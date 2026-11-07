"""Minimal async JSON-RPC client for EVM chains.

Deliberately small: the trading subsystem needs balances, ERC-20 metadata,
Transfer logs, gas data and raw-transaction broadcast. ABI encoding for the
handful of ERC-20 selectors is done by hand so no ABI library is pulled in.
Every request goes out with a ``User-Agent`` (the Robinhood public RPC
answers 403 without one) and batched JSON-RPC is used wherever a page of
reads would otherwise be N round-trips.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from agentos import __version__
from agentos.trading.chains import redact_rpc_url

log = structlog.get_logger(__name__)

USER_AGENT = f"agentos-trading/{__version__}"

#: How long ``sealed_receipt`` waits for the block after a receipt's own
#: (Base and Robinhood Chain make one every 2 s; the cap covers a slow node).
SEAL_TIMEOUT_S = 15.0
SEAL_INTERVAL_S = 1.0

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# keccak256("Approval(address,address,uint256)")
APPROVAL_TOPIC = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"

SEL_BALANCE_OF = "0x70a08231"
SEL_DECIMALS = "0x313ce567"
SEL_SYMBOL = "0x95d89b41"
SEL_NAME = "0x06fdde03"
SEL_ALLOWANCE = "0xdd62ed3e"
SEL_APPROVE = "0x095ea7b3"
SEL_TRANSFER = "0xa9059cbb"
SEL_TRANSFER_FROM = "0x23b872dd"
SEL_AGGREGATE3 = "0x82ad56cb"

UINT256_MAX = (1 << 256) - 1
# Anything at or above this is an "unlimited" allowance in practice: wallets
# and routers set 2**256-1, some tokens clamp to 2**255 or 2**96-1 (UNI).
UNLIMITED_ALLOWANCE_FLOOR = 1 << 95


class EvmRpcError(RuntimeError):
    """A JSON-RPC level error (the node answered with ``error``)."""

    def __init__(self, message: str, *, code: int | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class EvmTransportError(RuntimeError):
    """HTTP/transport failure talking to the node."""


# ── ABI helpers ────────────────────────────────────────────────────────────


def pad_address(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def pad_uint(value: int) -> str:
    if value < 0 or value > UINT256_MAX:
        raise ValueError("uint256 out of range")
    return format(value, "x").rjust(64, "0")


def encode_call(selector: str, *words: str) -> str:
    return selector + "".join(words)


def encode_aggregate3(calls: Sequence[tuple[str, str]]) -> str:
    """Calldata for ``aggregate3((address,bool,bytes)[])``, every call allowed to fail."""
    offsets: list[str] = []
    tails: list[str] = []
    offset = 32 * len(calls)
    for target, data in calls:
        body = data.removeprefix("0x")
        padded = body + "0" * (-len(body) % 64)
        tail = pad_address(target) + pad_uint(1) + pad_uint(96) + pad_uint(len(body) // 2) + padded
        offsets.append(pad_uint(offset))
        tails.append(tail)
        offset += len(tail) // 2
    return SEL_AGGREGATE3 + pad_uint(32) + pad_uint(len(calls)) + "".join(offsets + tails)


def decode_aggregate3(hex_data: str) -> list[tuple[bool, bytes]]:
    """``(success, returnData)`` per call; ``ValueError`` on anything malformed."""
    raw = bytes.fromhex(hex_data.removeprefix("0x"))

    def word(at: int) -> int:
        if at + 32 > len(raw):
            raise ValueError("aggregate3 answer is truncated")
        return int.from_bytes(raw[at : at + 32], "big")

    start = word(0) + 32
    count = word(start - 32)
    if count > (len(raw) - start) // 32:
        raise ValueError("aggregate3 answer is truncated")
    out: list[tuple[bool, bytes]] = []
    for index in range(count):
        item = start + word(start + 32 * index)
        data_at = item + word(item + 32)
        size = word(data_at)
        if data_at + 32 + size > len(raw):
            raise ValueError("aggregate3 answer is truncated")
        out.append((word(item) != 0, raw[data_at + 32 : data_at + 32 + size]))
    return out


def encode_transfer(recipient: str, amount: int) -> str:
    """``transfer(address,uint256)`` calldata."""
    return encode_call(SEL_TRANSFER, pad_address(recipient), pad_uint(amount))


def encode_approve(spender: str, amount: int) -> str:
    """``approve(address,uint256)`` calldata."""
    return encode_call(SEL_APPROVE, pad_address(spender), pad_uint(amount))


def is_unlimited(allowance: int) -> bool:
    return allowance >= UNLIMITED_ALLOWANCE_FLOOR


_TX_QUANTITY_KEYS = frozenset(
    {"value", "gas", "gasLimit", "gasPrice", "maxFeePerGas", "maxPriorityFeePerGas", "nonce"}
)


def rpc_tx(tx: dict[str, Any]) -> dict[str, Any]:
    """A transaction object as JSON-RPC wants it: every QUANTITY as a hex string.

    Lenient public nodes accept JSON numbers; strict gateways (dRPC, Alchemy)
    reject them with HTTP 400 "mismatched type", which is how a swap that
    quoted fine can still fail at the simulation step.
    """
    out: dict[str, Any] = {}
    for key, val in tx.items():
        if key in _TX_QUANTITY_KEYS and isinstance(val, int) and not isinstance(val, bool):
            out[key] = hex(val)
        elif key in _TX_QUANTITY_KEYS and isinstance(val, str) and val.isdigit():
            out[key] = hex(int(val))
        else:
            out[key] = val
    return out


def decode_uint(hex_data: str | None) -> int:
    if not hex_data or hex_data == "0x":
        return 0
    return int(hex_data, 16)


def decode_string(hex_data: str | None) -> str:
    """Decode an ABI ``string`` return, tolerating ``bytes32``-style symbols."""
    if not hex_data or hex_data == "0x":
        return ""
    raw = bytes.fromhex(hex_data.removeprefix("0x"))
    if len(raw) == 32:
        # Old tokens (MKR-style) return a bytes32.
        return raw.rstrip(b"\x00").decode("utf-8", errors="replace")
    if len(raw) < 64:
        return ""
    offset = int.from_bytes(raw[:32], "big")
    if offset + 32 > len(raw):
        return ""
    length = int.from_bytes(raw[offset : offset + 32], "big")
    start = offset + 32
    return raw[start : start + length].decode("utf-8", errors="replace")


def topic_address(topic: str) -> str:
    return "0x" + topic.removeprefix("0x")[-40:].lower()


@dataclass(frozen=True)
class TransferLog:
    tx_hash: str
    log_index: int
    block_number: int
    token: str
    sender: str
    recipient: str
    amount: int

    @property
    def key(self) -> tuple[str, int]:
        return (self.tx_hash, self.log_index)


def parse_transfer_log(log: dict[str, Any]) -> TransferLog | None:
    topics = log.get("topics") or []
    if len(topics) != 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
        # ERC-721 transfers carry 4 topics; anything else is not an ERC-20 Transfer.
        return None
    try:
        return TransferLog(
            tx_hash=str(log["transactionHash"]).lower(),
            log_index=int(str(log.get("logIndex", "0x0")), 16),
            block_number=int(str(log.get("blockNumber", "0x0")), 16),
            token=str(log["address"]).lower(),
            sender=topic_address(str(topics[1])),
            recipient=topic_address(str(topics[2])),
            amount=decode_uint(str(log.get("data") or "0x0")),
        )
    except (KeyError, ValueError, TypeError):
        return None


@dataclass(frozen=True)
class ApprovalLog:
    tx_hash: str
    log_index: int
    block_number: int
    token: str
    owner: str
    spender: str
    amount: int

    @property
    def key(self) -> tuple[str, int]:
        return (self.tx_hash, self.log_index)


def parse_approval_log(log: dict[str, Any]) -> ApprovalLog | None:
    """An ERC-20 ``Approval(owner, spender, value)`` log; None for anything else."""
    topics = log.get("topics") or []
    if len(topics) != 3 or str(topics[0]).lower() != APPROVAL_TOPIC:
        return None
    try:
        return ApprovalLog(
            tx_hash=str(log["transactionHash"]).lower(),
            log_index=int(str(log.get("logIndex", "0x0")), 16),
            block_number=int(str(log.get("blockNumber", "0x0")), 16),
            token=str(log["address"]).lower(),
            owner=topic_address(str(topics[1])),
            spender=topic_address(str(topics[2])),
            amount=decode_uint(str(log.get("data") or "0x0")),
        )
    except (KeyError, ValueError, TypeError):
        return None


# ── Client ─────────────────────────────────────────────────────────────────


# Tokens per Multicall3 ``eth_call``; a bigger read is split.
MULTICALL_CHUNK = 100
# The public node answers a read the sync can do without for a moment, so it gets
# less patience than the RPC itself, and a rest after failing: a pass reads once per
# wallet/chain, and an outage must not cost each of them a timeout and a retry.
READ_LANE_TIMEOUT_S = 5.0
READ_LANE_COOLDOWN_S = 60.0


class EvmClient:
    """One node, one URL. Safe to share across tasks."""

    def __init__(
        self,
        url: str,
        *,
        http: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        max_log_span: int = 2000,
        max_priority_fee_wei: int = 2 * 10**9,
        max_fee_per_gas_wei: int = 50 * 10**9,
        multicall3: str | None = None,
        read_url: str | None = None,
    ) -> None:
        self.url = url
        # Multicall3's address, and the node bulk balance reads go to first. Both
        # only ever change how ``erc20_balances`` reads, never anything else.
        self.multicall3 = multicall3
        self.read_url = read_url
        self._read_lane_down_until = 0.0
        # Provider keys live in the path (dRPC, Alchemy): errors show only the host.
        self.display_url = redact_rpc_url(url)
        self._own_http = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout)
        self._timeout = timeout
        self.max_log_span = max(1, int(max_log_span))
        # Ceilings on what ``fee_data`` may answer (see ``ChainSpec``): the
        # node's fee history is data, not a number this desk signs blindly.
        self.max_priority_fee_wei = max(1, int(max_priority_fee_wei))
        self.max_fee_per_gas_wei = max(self.max_priority_fee_wei, int(max_fee_per_gas_wei))
        self._next_id = 1

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": USER_AGENT,
        }

    async def _post(
        self, payload: Any, *, url: str | None = None, timeout: float | None = None
    ) -> Any:
        target = url or self.url
        display_url = self.display_url if url is None else redact_rpc_url(url)
        try:
            response = await self._http.post(
                target, json=payload, headers=self._headers(), timeout=timeout or self._timeout
            )
        except httpx.HTTPError as exc:
            raise EvmTransportError(f"{display_url}: {exc}") from exc
        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json()
                err = body.get("error") if isinstance(body, dict) else None
                if isinstance(err, dict) and err.get("message"):
                    detail = f" ({str(err['message'])[:160]})"
            except ValueError:
                pass
            raise EvmTransportError(f"{display_url}: HTTP {response.status_code}{detail}")
        try:
            return response.json()
        except ValueError as exc:
            raise EvmTransportError(f"{display_url}: invalid JSON response") from exc

    @staticmethod
    def _unwrap(item: Any) -> Any:
        if not isinstance(item, dict):
            raise EvmTransportError("malformed JSON-RPC response")
        error = item.get("error")
        if error:
            if isinstance(error, dict):
                raise EvmRpcError(
                    str(error.get("message") or "rpc error"),
                    code=error.get("code"),
                    data=error.get("data"),
                )
            raise EvmRpcError(str(error))
        return item.get("result")

    async def call(self, method: str, params: Sequence[Any] | None = None) -> Any:
        req_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": list(params or [])}
        return self._unwrap(await self._post(payload))

    async def batch(self, calls: Sequence[tuple[str, Sequence[Any]]]) -> list[Any]:
        """Issue many calls in one round-trip; results keep the input order.

        A node that does not support batching answers with a single object;
        that is treated as a transport error and the caller falls back to
        sequential calls.
        """
        if not calls:
            return []
        payload = []
        ids: list[int] = []
        for method, params in calls:
            req_id = self._next_id
            self._next_id += 1
            ids.append(req_id)
            payload.append(
                {"jsonrpc": "2.0", "id": req_id, "method": method, "params": list(params)}
            )
        try:
            raw = await self._post(payload)
        except EvmTransportError:
            raise
        if not isinstance(raw, list):
            # Sequential fallback for nodes without batch support.
            return [await self.call(m, p) for m, p in calls]
        by_id: dict[int, Any] = {}
        for item in raw:
            if isinstance(item, dict) and "id" in item:
                try:
                    by_id[int(item["id"])] = item
                except (TypeError, ValueError):
                    continue
        results: list[Any] = []
        for req_id in ids:
            item = by_id.get(req_id)
            if item is None:
                raise EvmTransportError("batch response missing an id")
            results.append(self._unwrap(item))
        return results

    # ── reads ──────────────────────────────────────────────────────────

    async def chain_id(self) -> int:
        return decode_uint(await self.call("eth_chainId"))

    async def block_number(self) -> int:
        return decode_uint(await self.call("eth_blockNumber"))

    async def get_block(self, number: int | str) -> dict[str, Any] | None:
        tag = number if isinstance(number, str) else hex(number)
        result = await self.call("eth_getBlockByNumber", [tag, False])
        return result if isinstance(result, dict) else None

    async def block_timestamp(self, number: int) -> int | None:
        block = await self.get_block(number)
        if not block:
            return None
        return decode_uint(str(block.get("timestamp") or "0x0"))

    async def get_balance(self, address: str, block: str = "latest") -> int:
        return decode_uint(await self.call("eth_getBalance", [address, block]))

    async def eth_call(self, to: str, data: str, block: str = "latest") -> str:
        result = await self.call("eth_call", [{"to": to, "data": data}, block])
        return str(result or "0x")

    async def erc20_balance_of(self, token: str, owner: str) -> int:
        return decode_uint(
            await self.eth_call(token, encode_call(SEL_BALANCE_OF, pad_address(owner)))
        )

    async def erc20_balances(self, owner: str, tokens: Iterable[str]) -> dict[str, int | None]:
        """``balanceOf`` for every token, in as few requests as the chain allows.

        Through Multicall3 when the chain has it, on ``read_url`` when one is
        set; a token that gets no clean answer that way is read again on its
        own from the RPC itself, in one batch.

        A token whose read failed maps to ``None``, never to ``0``: a balance
        the node could not answer is unknown, and a caller that wrote it down
        as zero would erase a real holding from the ledger.
        """
        token_list = [t.lower() for t in tokens]
        if not token_list:
            return {}
        out: dict[str, int | None] = {}
        if self.multicall3:
            out.update(await self._multicall_balances(owner, token_list))
        pending = [token for token in token_list if token not in out]
        if not pending:
            return out
        calls: list[tuple[str, Sequence[Any]]] = [
            (
                "eth_call",
                [{"to": token, "data": encode_call(SEL_BALANCE_OF, pad_address(owner))}, "latest"],
            )
            for token in pending
        ]
        results = await self._batch_lenient(calls)
        for token, result in zip(pending, results, strict=True):
            try:
                out[token] = decode_uint(result) if isinstance(result, str) else None
            except ValueError:
                out[token] = None
        return out

    async def _multicall_balances(self, owner: str, tokens: Sequence[str]) -> dict[str, int]:
        """The balances Multicall3 answered cleanly; anything else is left out."""
        data = encode_call(SEL_BALANCE_OF, pad_address(owner))
        out: dict[str, int] = {}
        for start in range(0, len(tokens), MULTICALL_CHUNK):
            chunk = tokens[start : start + MULTICALL_CHUNK]
            results = await self._aggregate3(encode_aggregate3([(t, data) for t in chunk]))
            if results is None or len(results) != len(chunk):
                continue
            for token, (success, answer) in zip(chunk, results, strict=True):
                # A call to no contract "succeeds" with no data: not an answer.
                if success and len(answer) == 32:
                    out[token] = int.from_bytes(answer, "big")
        return out

    async def _aggregate3(self, calldata: str) -> list[tuple[bool, bytes]] | None:
        """Ask the read lane, then the RPC itself; ``None`` when neither answers."""
        params = [{"to": self.multicall3, "data": calldata}, "latest"]
        payload = {"jsonrpc": "2.0", "method": "eth_call", "params": params}
        if self.read_url and time.monotonic() >= self._read_lane_down_until:
            self._next_id += 1
            try:
                raw = await self._post(
                    {**payload, "id": self._next_id},
                    url=self.read_url,
                    timeout=READ_LANE_TIMEOUT_S,
                )
                return decode_aggregate3(self._unwrap(raw))
            except (EvmRpcError, EvmTransportError, ValueError, TypeError, AttributeError) as exc:
                self._read_lane_down_until = time.monotonic() + READ_LANE_COOLDOWN_S
                log.debug("trading.read_lane_failed", error=str(exc)[:200])
        self._next_id += 1
        try:
            raw = await self._post({**payload, "id": self._next_id})
            return decode_aggregate3(self._unwrap(raw))
        except (EvmRpcError, EvmTransportError, ValueError, TypeError, AttributeError) as exc:
            log.debug("trading.multicall_failed", error=str(exc)[:200])
            return None

    async def _batch_lenient(self, calls: Sequence[tuple[str, Sequence[Any]]]) -> list[Any]:
        """Batch, but a per-item RPC error becomes ``None`` instead of raising."""
        if not calls:
            return []
        payload = []
        ids: list[int] = []
        for method, params in calls:
            req_id = self._next_id
            self._next_id += 1
            ids.append(req_id)
            payload.append(
                {"jsonrpc": "2.0", "id": req_id, "method": method, "params": list(params)}
            )
        raw = await self._post(payload)
        if not isinstance(raw, list):
            results: list[Any] = []
            for method, params in calls:
                try:
                    results.append(await self.call(method, params))
                except EvmRpcError:
                    results.append(None)
            return results
        by_id: dict[int, Any] = {}
        for item in raw:
            if isinstance(item, dict) and "id" in item:
                try:
                    by_id[int(item["id"])] = item
                except (TypeError, ValueError):
                    continue
        out: list[Any] = []
        for req_id in ids:
            item = by_id.get(req_id)
            if item is None or not isinstance(item, dict) or item.get("error"):
                out.append(None)
            else:
                out.append(item.get("result"))
        return out

    async def erc20_metadata(self, token: str) -> tuple[str, str, int]:
        """``(symbol, name, decimals)``; unknown fields degrade to ``("", "", 18)``."""
        calls: list[tuple[str, Sequence[Any]]] = [
            ("eth_call", [{"to": token, "data": SEL_SYMBOL}, "latest"]),
            ("eth_call", [{"to": token, "data": SEL_NAME}, "latest"]),
            ("eth_call", [{"to": token, "data": SEL_DECIMALS}, "latest"]),
        ]
        symbol_raw, name_raw, decimals_raw = await self._batch_lenient(calls)
        symbol = decode_string(symbol_raw) if isinstance(symbol_raw, str) else ""
        name = decode_string(name_raw) if isinstance(name_raw, str) else ""
        decimals = (
            decode_uint(decimals_raw)
            if isinstance(decimals_raw, str) and decimals_raw not in ("", "0x")
            else 18
        )
        if decimals > 77:
            decimals = 18
        return symbol.strip("\x00 "), name.strip("\x00 "), decimals

    async def erc20_allowance(self, token: str, owner: str, spender: str) -> int:
        data = encode_call(SEL_ALLOWANCE, pad_address(owner), pad_address(spender))
        return decode_uint(await self.eth_call(token, data))

    async def erc20_allowances(
        self, owner: str, pairs: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], int | None]:
        """``allowance(owner, spender)`` for every ``(token, spender)`` pair in one batch.

        A pair whose read failed maps to ``None``: an allowance the node
        could not answer is unknown, and "unknown" must not read as revoked.
        """
        keys = [(t.lower(), s.lower()) for t, s in pairs]
        if not keys:
            return {}
        calls: list[tuple[str, Sequence[Any]]] = [
            (
                "eth_call",
                [
                    {
                        "to": token,
                        "data": encode_call(
                            SEL_ALLOWANCE, pad_address(owner), pad_address(spender)
                        ),
                    },
                    "latest",
                ],
            )
            for token, spender in keys
        ]
        out: dict[tuple[str, str], int | None] = {}
        results = await self._batch_lenient(calls)
        for key, result in zip(keys, results, strict=True):
            try:
                out[key] = decode_uint(result) if isinstance(result, str) else None
            except ValueError:
                out[key] = None
        return out

    async def get_code(self, address: str) -> str:
        return str(await self.call("eth_getCode", [address, "latest"]) or "0x")

    # ── logs ───────────────────────────────────────────────────────────

    async def get_logs(
        self,
        *,
        from_block: int,
        to_block: int,
        topics: Sequence[str | None | list[str]],
        address: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "topics": list(topics),
        }
        if address:
            params["address"] = address
        result = await self.call("eth_getLogs", [params])
        return [item for item in (result or []) if isinstance(item, dict)]

    async def transfer_logs(
        self,
        wallet: str,
        *,
        from_block: int,
        to_block: int,
        max_span: int | None = None,
        pause_s: float = 0.0,
    ) -> list[TransferLog]:
        """Every ERC-20 Transfer to or from ``wallet`` in the block range.

        Chunked to the node's tolerance; a chunk the node refuses (too many
        results, range too large) is halved and retried down to a single
        block before the error is surfaced. A load-balanced gateway (dRPC)
        refuses an oversized range with an HTTP 500, not a JSON-RPC error,
        so a transport error halves the span too — down to
        ``self.max_log_span``, which every node accepts; below that it is a
        real outage and is raised (same rule as :meth:`approval_logs`).
        """
        if to_block < from_block:
            return []
        padded = "0x" + pad_address(wallet)
        span = max(1, int(max_span or self.max_log_span))
        floor = max(1, min(span, self.max_log_span))
        seen: dict[tuple[str, int], TransferLog] = {}
        start = from_block
        while start <= to_block:
            end = min(to_block, start + span - 1)
            try:
                incoming = await self.get_logs(
                    from_block=start, to_block=end, topics=[TRANSFER_TOPIC, None, padded]
                )
                outgoing = await self.get_logs(
                    from_block=start, to_block=end, topics=[TRANSFER_TOPIC, padded]
                )
            except EvmRpcError:
                if span == 1:
                    raise
                span = max(1, span // 2)
                continue
            except EvmTransportError:
                if span <= floor:
                    raise
                span = max(floor, span // 2)
                continue
            for log in [*incoming, *outgoing]:
                parsed = parse_transfer_log(log)
                if parsed is not None:
                    seen[parsed.key] = parsed
            start = end + 1
            if pause_s > 0 and start <= to_block:
                await asyncio.sleep(pause_s)
        return sorted(seen.values(), key=lambda t: (t.block_number, t.log_index))

    async def approval_logs(
        self,
        owner: str,
        *,
        from_block: int,
        to_block: int,
        max_span: int | None = None,
        pause_s: float = 0.0,
    ) -> list[ApprovalLog]:
        """Every ERC-20 Approval granted *by* ``owner`` in the block range.

        Same chunking and halving as :meth:`transfer_logs`, with one more
        case: a load-balanced gateway (dRPC) answers an oversized range with
        an HTTP 500 and a sentence, not a JSON-RPC error, so a transport
        error also halves the span — down to ``self.max_log_span``, which
        every node accepts; below that the error is a real outage and is
        raised. Only the grant side is scanned: what this wallet let others
        spend is the question an allowance review asks.
        """
        if to_block < from_block:
            return []
        padded = "0x" + pad_address(owner)
        span = max(1, int(max_span or self.max_log_span))
        floor = max(1, min(span, self.max_log_span))
        seen: dict[tuple[str, int], ApprovalLog] = {}
        start = from_block
        while start <= to_block:
            end = min(to_block, start + span - 1)
            try:
                logs = await self.get_logs(
                    from_block=start, to_block=end, topics=[APPROVAL_TOPIC, padded]
                )
            except EvmRpcError:
                if span == 1:
                    raise
                span = max(1, span // 2)
                continue
            except EvmTransportError:
                if span <= floor:
                    raise
                span = max(floor, span // 2)
                continue
            for log in logs:
                parsed = parse_approval_log(log)
                if parsed is not None:
                    seen[parsed.key] = parsed
            start = end + 1
            if pause_s > 0 and start <= to_block:
                await asyncio.sleep(pause_s)
        return sorted(seen.values(), key=lambda a: (a.block_number, a.log_index))

    # ── transactions ───────────────────────────────────────────────────

    async def get_transaction(self, tx_hash: str) -> dict[str, Any] | None:
        result = await self.call("eth_getTransactionByHash", [tx_hash])
        return result if isinstance(result, dict) else None

    async def nonce(self, address: str, block: str = "pending") -> int:
        return decode_uint(await self.call("eth_getTransactionCount", [address, block]))

    async def estimate_gas(self, tx: dict[str, Any]) -> int:
        return decode_uint(await self.call("eth_estimateGas", [rpc_tx(tx)]))

    async def simulate(self, tx: dict[str, Any]) -> str:
        """``eth_call`` the transaction as the sender; raises on revert."""
        call_tx = {k: v for k, v in tx.items() if k in {"from", "to", "data", "value", "gas"}}
        return str(await self.call("eth_call", [rpc_tx(call_tx), "latest"]) or "0x")

    async def fee_data(self) -> tuple[int, int]:
        """``(maxFeePerGas, maxPriorityFeePerGas)`` from ``eth_feeHistory``.

        The tip is the *median* 50th-percentile reward over the last ten
        blocks: one block with a single absurd tip (a MEV bundle, a fat
        finger) must not become the price of every transaction this desk
        signs. Both numbers are then capped at the chain's ceilings — the
        cap wins over the node, never the other way round.

        Falls back to ``eth_gasPrice`` when the node does not serve fee
        history; the priority tip is then a small fraction of the base fee.
        """
        try:
            history = await self.call("eth_feeHistory", [10, "latest", [50]])
        except EvmRpcError:
            history = None
        if isinstance(history, dict) and history.get("baseFeePerGas"):
            base_fees = [decode_uint(str(x)) for x in history["baseFeePerGas"]]
            rewards = history.get("reward") or []
            tips = sorted(decode_uint(str(r[0])) for r in rewards if r)
            base = base_fees[-1] if base_fees else 0
            tip = tips[len(tips) // 2] if tips else max(base // 20, 1)
            return self._capped(base * 2, tip)
        gas_price = decode_uint(await self.call("eth_gasPrice"))
        return self._capped(gas_price, max(gas_price // 20, 1))

    def _capped(self, fee_before_tip: int, tip: int) -> tuple[int, int]:
        tip = min(max(int(tip), 1), self.max_priority_fee_wei)
        max_fee = min(int(fee_before_tip) + tip, self.max_fee_per_gas_wei)
        # EIP-1559 needs maxFeePerGas >= maxPriorityFeePerGas.
        return max_fee, min(tip, max_fee)

    async def send_raw_transaction(self, raw_hex: str) -> str:
        result = await self.call("eth_sendRawTransaction", [raw_hex])
        return str(result).lower()

    async def get_transaction_receipt(self, tx_hash: str) -> dict[str, Any] | None:
        result = await self.call("eth_getTransactionReceipt", [tx_hash])
        return result if isinstance(result, dict) else None

    async def wait_for_receipt(
        self,
        tx_hash: str,
        *,
        timeout_s: float = 120.0,
        interval_s: float = 1.5,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout_s
        while True:
            receipt = await self.get_transaction_receipt(tx_hash)
            if receipt is not None and receipt.get("blockNumber"):
                return receipt
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(interval_s)

    async def wait_for_sealed_receipt(
        self,
        tx_hash: str,
        *,
        timeout_s: float = 120.0,
        interval_s: float = 1.5,
    ) -> dict[str, Any] | None:
        """``wait_for_receipt``, then ``sealed_receipt``: what settlement books from."""
        first = await self.wait_for_receipt(tx_hash, timeout_s=timeout_s, interval_s=interval_s)
        if first is None:
            return None
        return await sealed_receipt(self, tx_hash, first)


def is_provisional_capable(receipt: dict[str, Any]) -> bool:
    """Whether a receipt comes from an OP-stack chain (it carries ``l1Fee``).

    Only there can the first receipt a node serves be a preconfirmation
    whose fee fields change once the block is sealed.
    """
    return "l1Fee" in receipt


async def sealed_receipt(
    client: EvmClient,
    tx_hash: str,
    first: dict[str, Any],
    *,
    timeout_s: float | None = None,
    interval_s: float | None = None,
) -> dict[str, Any]:
    """The receipt of ``tx_hash`` as the sealed block has it.

    Base serves flashblock preconfirmations: the first receipt a node returns
    for a transaction can carry a provisional ``l1Fee`` that differs, by a
    factor of two either way, from the one charged once the block is sealed —
    and every gas, ``spent`` and ``received`` figure booked from it is off by
    the difference. So on an OP-stack chain wait until the head is past the
    receipt's block and read the receipt again. A chain without ``l1Fee``
    returns ``first`` untouched and costs nothing; when the head never moves
    or the re-read fails, ``first`` is kept and ``trading.receipt_provisional``
    logged — a slightly wrong fee beats a settlement that never happens.
    """
    if not is_provisional_capable(first):
        return first
    block = decode_uint(str(first.get("blockNumber") or "0x0"))
    if not block:
        return first
    deadline = time.monotonic() + (SEAL_TIMEOUT_S if timeout_s is None else timeout_s)
    interval = SEAL_INTERVAL_S if interval_s is None else interval_s
    reason = "head did not advance"
    while True:
        try:
            head = await client.block_number()
        except (EvmRpcError, EvmTransportError) as exc:
            head, reason = 0, f"eth_blockNumber failed: {exc}"
        if head > block:
            try:
                again = await client.get_transaction_receipt(tx_hash)
            except (EvmRpcError, EvmTransportError) as exc:
                again, reason = None, f"receipt re-read failed: {exc}"
            else:
                reason = "receipt re-read came back empty"
            if again is not None and again.get("blockNumber"):
                return again
        if time.monotonic() >= deadline:
            log.warning("trading.receipt_provisional", tx=tx_hash, block=block, reason=reason)
            return first
        await asyncio.sleep(interval)


def receipt_succeeded(receipt: dict[str, Any] | None) -> bool:
    if not receipt:
        return False
    return decode_uint(str(receipt.get("status") or "0x0")) == 1


def receipt_gas_wei(receipt: dict[str, Any]) -> int:
    """What the transaction cost the sender in wei.

    On OP-stack chains (Base, Robinhood Chain) the receipt also carries
    ``l1Fee``, the data-posting fee charged on top of ``gasUsed × price``;
    it is often several times the L2 fee, and leaving it out books it as
    "spent" against the order instead of as gas.
    """
    gas_used = decode_uint(str(receipt.get("gasUsed") or "0x0"))
    price = decode_uint(str(receipt.get("effectiveGasPrice") or "0x0"))
    l1_fee = decode_uint(str(receipt.get("l1Fee") or "0x0"))
    return gas_used * price + l1_fee


def receipt_transfers(receipt: dict[str, Any]) -> list[TransferLog]:
    logs = receipt.get("logs") or []
    out: list[TransferLog] = []
    for log in logs:
        if not isinstance(log, dict):
            continue
        entry = dict(log)
        entry.setdefault("transactionHash", receipt.get("transactionHash"))
        entry.setdefault("blockNumber", receipt.get("blockNumber"))
        parsed = parse_transfer_log(entry)
        if parsed is not None:
            out.append(parsed)
    return out


def receipt_approvals(receipt: dict[str, Any]) -> list[ApprovalLog]:
    logs = receipt.get("logs") or []
    out: list[ApprovalLog] = []
    for log in logs:
        if not isinstance(log, dict):
            continue
        entry = dict(log)
        entry.setdefault("transactionHash", receipt.get("transactionHash"))
        entry.setdefault("blockNumber", receipt.get("blockNumber"))
        parsed = parse_approval_log(entry)
        if parsed is not None:
            out.append(parsed)
    return out
