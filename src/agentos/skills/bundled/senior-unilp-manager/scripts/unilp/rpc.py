"""JSON-RPC transport — replaces viem's public client, batching and multicall.

viem hid three things behind ``createPublicClient`` that have to become explicit here:

* **HTTP batching.** ``http({batch:{wait:10}})`` coalesced concurrent requests. Python
  is synchronous, so batching is an explicit :meth:`RpcClient.batch` call.
* **Multicall.** ``client.multicall`` wrapped Multicall3 ``aggregate3``. Reimplemented
  in :meth:`RpcClient.multicall`, returning the same ``[{status, result}]`` shape the
  ported call sites already consume.
* **Concurrency.** ``Promise.all`` became either a batch or a plain loop. Log sweeps
  stay sequential on purpose — the Node source notes that firing them concurrently
  only trips the provider's rate limiter.

Two failure modes are handled deliberately rather than left to chance:

* A chunk that is too large for the node (gas cap, response cap) is **bisected** and
  retried down to a single call, so an over-sized request degrades into slower but
  correct results instead of marking healthy calls as failed.
* ``success == true`` with empty ``returnData`` means there is no code at the target.
  That is reported as a failure, never decoded as a zero — launchpad discovery
  depends on telling the two apart.
"""

from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.request
import zlib
from typing import Any

from .abi_codec import decode, encode, encode_function_data
from .chains import resolve_rpc_url

_AGG3_IN = [{"type": "tuple[]", "components": [
    {"name": "target", "type": "address"},
    {"name": "allowFailure", "type": "bool"},
    {"name": "callData", "type": "bytes"},
]}]
_AGG3_OUT = [{"type": "tuple[]", "components": [
    {"name": "success", "type": "bool"},
    {"name": "returnData", "type": "bytes"},
]}]
_AGGREGATE3_ABI = [{
    "type": "function", "name": "aggregate3", "stateMutability": "payable",
    "inputs": _AGG3_IN, "outputs": _AGG3_OUT,
}]

# Chunking for multicall. viem chunked on 1024 bytes of calldata and let the HTTP
# transport re-batch; larger chunks and fewer round trips win over a remote RPC.
MAX_CALLS_PER_CHUNK = 100
MAX_CALLDATA_BYTES = 24_000

# eth_sendRawTransaction is never retried: a "failed" send may already be in the
# mempool, and resending risks a second transaction rather than a duplicate no-op.
_NEVER_RETRY = {"eth_sendRawTransaction"}
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RESPONSE_BYTES = 256 * 1024 * 1024

# Sent on every outbound HTTP request, RPC and price API alike. Not cosmetic: urllib's
# default "Python-urllib/3.x" is blocked outright by some WAFs (GeckoTerminal sits
# behind Cloudflare and answers it with a 403 "error 1010"), which surfaces as prices
# quietly reading n/a rather than as an error.
USER_AGENT = "senior-unilp-manager/1.0"


# Standard JSON-RPC code for a reverted ``eth_call`` (EIP-1474 / geth). Some
# nodes report a revert under another code, which the message check below
# catches; a node fault never names one.
_REVERT_ERROR_CODE = 3


def _names_a_revert(message: Any) -> bool:
    """True when a JSON-RPC error message reports a contract revert."""
    return "revert" in str(message).lower()


def _carries_a_revert_blob(data: Any) -> bool:
    """True when the error carries a hex ``data`` payload — the contract's own output.

    A node that refuses a call (a rate limit, a transient internal error) has no
    contract output to attach, so a non-empty hex blob means the call reached the
    contract and it answered, whatever the message says. Some nodes report a
    revert as ``VM Exception`` instead of naming it.
    """
    return isinstance(data, str) and data.startswith("0x") and len(data) > 2


class RpcError(RuntimeError):
    """A JSON-RPC error response. ``data`` carries the revert blob when present.

    ``error`` is an object in the JSON-RPC spec, but nodes and proxies do answer
    with a bare string (``"error": "rate limit exceeded"``) or null. Reading it
    as a dict unconditionally turned every one of those into an AttributeError
    that killed the whole command instead of the RpcError callers handle.

    ``answered`` separates the two kinds of error, because only one of them is
    evidence about the contract: True when the node reached the contract and the
    contract's own response is what failed the call (a revert), False when the
    node itself failed -- a rate limit, a transient internal error. Callers that
    turn a failed call into a verdict must not report the second as the first:
    a node fault never names a revert. A revert blob in ``data`` is the
    contract's own output, so a non-empty hex payload counts as an answer
    whatever the message says.
    """

    def __init__(self, method: str, error: Any) -> None:
        fields = error if isinstance(error, dict) else {}
        super().__init__(f"{method}: {fields.get('message', error)}")
        self.code = fields.get("code")
        self.data = fields.get("data")
        self.raw = error
        self.answered = (
            self.code == _REVERT_ERROR_CODE
            or _names_a_revert(fields.get("message", error))
            or _carries_a_revert_blob(self.data)
        )


def _jsonrpc_error_in(exc: urllib.error.HTTPError) -> Any | None:
    """The JSON-RPC ``error`` member of an HTTP error body, or None.

    Reads at most 64 KB — an error page is small, and a proxy can answer a 5xx
    with anything at all — and treats a body that is not a JSON-RPC envelope as
    "no structured error", which keeps the plain ``HTTP <code>`` path intact.
    """
    try:
        raw = exc.read(65536)
    except Exception:  # noqa: BLE001 — a closed or absent body is "no body"
        return None
    if not raw:
        return None
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    if isinstance(body, list) and body:
        body = body[0]
    if isinstance(body, dict) and body.get("error"):
        return body["error"]
    return None


_TRANSIENT_MARKERS = ("temporar", "retry", "timeout", "timed out", "overload", "busy",
                      "rate limit", "too many")


def _is_transient(error: Any) -> bool:
    """Whether a JSON-RPC error names a passing condition rather than a rule.

    drpc's ``code 19 "Temporary internal error. Please retry"`` is worth retrying;
    its ``code 22 "range over 100000 blocks is not supported"`` never changes.
    """
    message = error.get("message", "") if isinstance(error, dict) else str(error)
    lowered = str(message).lower()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)


class RpcClient:
    """A minimal, synchronous JSON-RPC client over ``urllib``."""

    def __init__(self, chain: dict, rpc_url: str | None = None, timeout: int = 60,
                 debug: bool = False, allow_batch: bool = True) -> None:
        self.chain = chain
        # Routed through resolve_rpc_url even when overridden, so the
        # http(s) guard sees --rpc too; it returns the override unchanged.
        self.url = resolve_rpc_url(chain, rpc_url)
        self.timeout = timeout
        self.debug = debug
        self._allow_batch = allow_batch
        self._next_id = 0
        self.request_count = 0

    # -- transport ---------------------------------------------------------

    def _post(self, payload: Any, label: str, retries: int = 3) -> Any:
        body = json.dumps(payload).encode("utf-8")
        attempt = 0
        while True:
            attempt += 1
            request = urllib.request.Request(
                self.url, data=body, method="POST",
                headers={
                    "content-type": "application/json",
                    "accept": "application/json",
                    # Log sweeps return megabytes; gzip is a real saving and is stdlib.
                    "accept-encoding": "gzip, deflate",
                    "user-agent": USER_AGENT,
                },
            )
            try:
                self.request_count += 1
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read(_MAX_RESPONSE_BYTES + 1)
                    if len(raw) > _MAX_RESPONSE_BYTES:
                        raise RuntimeError(
                            f"{label}: response exceeds {_MAX_RESPONSE_BYTES // 1048576} MB — "
                            "narrow the block range or lower --max-pools"
                        )
                    encoding = (response.headers.get("content-encoding") or "").lower()
                if encoding == "gzip":
                    raw = gzip.decompress(raw)
                elif encoding == "deflate":
                    raw = zlib.decompress(raw)
                return json.loads(raw)
            except urllib.error.HTTPError as exc:
                # Proxies such as drpc put a real JSON-RPC error in a 5xx body
                # ("eth_getLogs range over 100000 blocks is not supported ...").
                # Surface it: "HTTP 500" alone sends the caller hunting for a network
                # fault that is not there. A deterministic error is also not worth
                # three retries with backoff — only a transient one is.
                error = _jsonrpc_error_in(exc)
                retryable = (
                    exc.code in _RETRY_STATUS and label not in _NEVER_RETRY
                    and (error is None or _is_transient(error))
                )
                if not retryable or attempt > retries:
                    if error is not None:
                        raise RpcError(label, error) from exc
                    raise RuntimeError(f"{label}: HTTP {exc.code}") from exc
                self._sleep_before_retry(exc.headers.get("retry-after"), attempt)
            except (urllib.error.URLError, TimeoutError) as exc:
                if label in _NEVER_RETRY or attempt > retries:
                    raise RuntimeError(f"{label}: {exc}") from exc
                self._sleep_before_retry(None, attempt)

    def _sleep_before_retry(self, retry_after: str | None, attempt: int) -> None:
        delay = 0.5 * (2 ** (attempt - 1))
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        if self.debug:
            print(f"  [rpc] retry in {delay:.1f}s (attempt {attempt})")
        time.sleep(min(delay, 30.0))

    def request(self, method: str, params: list | None = None) -> Any:
        self._next_id += 1
        body = self._post(
            {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or []},
            method,
        )
        if isinstance(body, dict) and body.get("error"):
            raise RpcError(method, body["error"])
        return body.get("result") if isinstance(body, dict) else None

    def batch(self, calls: list[dict], chunk_size: int = 20) -> list[Any]:
        """Send many calls in JSON-RPC array form. One round trip per chunk.

        Results come back positionally. A per-call error becomes ``{"error": …}``
        rather than raising, so one bad pool cannot abort a 40-pool sweep — callers
        distinguish the two with ``isinstance(result, dict) and "error" in result``.

        Responses are mapped strictly by ``id`` and the count is asserted: providers
        do reorder, and a silently truncated batch would attribute results to the
        wrong pool.
        """
        out: list[Any] = [None] * len(calls)
        if not calls:
            return out

        for start in range(0, len(calls), chunk_size):
            window = calls[start:start + chunk_size]
            if not self._allow_batch:
                for offset, call in enumerate(window):
                    try:
                        out[start + offset] = self.request(call["method"], call.get("params"))
                    except RpcError as exc:
                        out[start + offset] = {"error": exc.raw}
                continue

            payload = [
                {"jsonrpc": "2.0", "id": start + offset,
                 "method": call["method"], "params": call.get("params") or []}
                for offset, call in enumerate(window)
            ]
            body = self._post(payload, "batch")
            if not isinstance(body, list):
                # Provider does not support batching — fall back for the rest of
                # this process rather than failing the command.
                self._allow_batch = False
                if self.debug:
                    print("  [rpc] provider rejected a batch; falling back to singles")
                for offset, call in enumerate(window):
                    try:
                        out[start + offset] = self.request(call["method"], call.get("params"))
                    except RpcError as exc:
                        out[start + offset] = {"error": exc.raw}
                continue

            seen = set()
            for entry in body:
                index = int(entry["id"])
                seen.add(index)
                error = entry.get("error")
                out[index] = {"error": error} if error else entry.get("result")
            missing = set(range(start, start + len(window))) - seen
            if missing:
                raise RuntimeError(
                    f"batch response dropped {len(missing)} of {len(window)} calls "
                    "— results cannot be trusted"
                )
        return out

    # -- convenience wrappers ---------------------------------------------

    def call(self, to: str, data: str, block: str = "latest",
             from_address: str | None = None) -> str:
        params: dict[str, str] = {"to": to, "data": data}
        if from_address:
            params["from"] = from_address
        return self.request("eth_call", [params, block])

    def read(self, address: str, abi: list, function_name: str,
             args: list | None = None, block: str = "latest") -> Any:
        from .abi_codec import decode_function_result

        data = encode_function_data(abi, function_name, args or [])
        return decode_function_result(abi, function_name, self.call(address, data, block))

    def block_number(self) -> int:
        return int(self.request("eth_blockNumber"), 16)

    def chain_id(self) -> int:
        return int(self.request("eth_chainId"), 16)

    def get_block(self, block: str = "latest") -> dict:
        return self.request("eth_getBlockByNumber", [block, False])

    def get_logs(self, params: dict) -> list:
        return self.request("eth_getLogs", [params])

    def transaction_count(self, address: str, block: str = "pending") -> int:
        return int(self.request("eth_getTransactionCount", [address, block]), 16)

    def estimate_gas(self, tx: dict) -> int:
        return int(self.request("eth_estimateGas", [tx]), 16)

    def send_raw_transaction(self, raw: str) -> str:
        return self.request("eth_sendRawTransaction", [raw])

    def get_receipt(self, tx_hash: str) -> dict | None:
        return self.request("eth_getTransactionReceipt", [tx_hash])

    # -- multicall ---------------------------------------------------------

    def multicall(self, calls: list[dict], allow_failure: bool = True,
                  block: str = "latest") -> list[dict]:
        """Multicall3 ``aggregate3``.

        Each call is ``{"address", "abi", "functionName", "args"}``. Returns one
        ``{"status": "success"|"failure", "result": …}`` per input, in order —
        matching what the ported call sites expect from ``client.multicall``.
        """
        if not calls:
            return []
        prepared = []
        for call in calls:
            prepared.append({
                "call": call,
                "data": encode_function_data(
                    call["abi"], call["functionName"], call.get("args") or []
                ),
            })

        results: list[dict] = []
        for chunk in self._chunk(prepared):
            results.extend(self._multicall_chunk(chunk, allow_failure, block))
        return results

    @staticmethod
    def _chunk(prepared: list[dict]) -> list[list[dict]]:
        chunks: list[list[dict]] = []
        current: list[dict] = []
        size = 0
        for entry in prepared:
            entry_size = len(entry["data"]) // 2
            if current and (
                len(current) >= MAX_CALLS_PER_CHUNK or size + entry_size > MAX_CALLDATA_BYTES
            ):
                chunks.append(current)
                current, size = [], 0
            current.append(entry)
            size += entry_size
        if current:
            chunks.append(current)
        return chunks

    def _multicall_chunk(self, chunk: list[dict], allow_failure: bool,
                         block: str) -> list[dict]:
        payload = encode_function_data(_AGGREGATE3_ABI, "aggregate3", [[
            {"target": entry["call"]["address"], "allowFailure": True,
             "callData": entry["data"]}
            for entry in chunk
        ]])
        try:
            raw = self.call(self.chain["multicall3"], payload, block)
        except (RpcError, RuntimeError):
            # Too big for this node, or a transient failure. Bisect and retry; a
            # single call that still fails is reported as a failure, not dropped.
            if len(chunk) == 1:
                if allow_failure:
                    return [{"status": "failure", "result": None,
                             "error": "call failed on its own"}]
                raise
            midpoint = len(chunk) // 2
            if self.debug:
                print(f"  [rpc] multicall chunk of {len(chunk)} failed; bisecting")
            return (self._multicall_chunk(chunk[:midpoint], allow_failure, block)
                    + self._multicall_chunk(chunk[midpoint:], allow_failure, block))

        decoded = decode(_AGG3_OUT, raw)[0]
        out: list[dict] = []
        for entry, item in zip(chunk, decoded):
            call = entry["call"]
            return_data = item["returnData"]
            if not item["success"] or return_data in ("0x", ""):
                # Empty returnData on a "successful" call means no code at the
                # target. Decoding it as zeros would invent a value.
                reason = ("reverted" if not item["success"]
                          else "empty return — no contract at that address")
                if not allow_failure:
                    raise RuntimeError(
                        f"multicall {call['functionName']} on {call['address']}: {reason}"
                    )
                out.append({"status": "failure", "result": None, "error": reason})
                continue
            from .abi_codec import decode_function_result
            try:
                value = decode_function_result(call["abi"], call["functionName"], return_data)
            except Exception as exc:  # noqa: BLE001 — a decode failure is a call failure
                if not allow_failure:
                    raise
                out.append({"status": "failure", "result": None, "error": str(exc)})
                continue
            out.append({"status": "success", "result": value})
        return out


def unwrap(results: list[dict], label: str = "multicall") -> list:
    """Take the values out of a multicall result list, raising on any failure."""
    values = []
    for index, entry in enumerate(results):
        if entry["status"] != "success":
            raise RuntimeError(f"{label}[{index}] failed: {entry.get('error')}")
        values.append(entry["result"])
    return values


def encode_aggregate3(calls: list[dict]) -> str:
    """Exposed for the self-test, which pins the request encoding."""
    return encode(_AGG3_IN, [calls])
