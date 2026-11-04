"""Bridging between Base and Robinhood Chain through Relay.

Relay (https://docs.relay.link) is a public, keyless REST API: ``POST
/quote/v2`` prices a cross-chain transfer and answers with the unsigned
origin-chain transactions that fund it (an ERC-20 ``approve`` when one is
needed, then one ``deposit`` to a Relay solver carrying the request id in
its calldata); ``GET /intents/status/v3`` says whether the solver has filled
the destination side. Relay holds no key and never broadcasts: funds move
only when the vault signs the deposit below.

What is checked here before anything is signed mirrors the aggregator's
discipline (``aggregator.py``):

* the deposit is sent ``to`` a solver this desk has pinned
  (:data:`RELAY_SOLVERS`) — the set is read from this file, not from the
  response — and the approval, when there is one, names that same solver as
  its spender and is on the token being bridged for no more than the order;
* a native deposit carries exactly the order's value and an ERC-20 deposit
  carries none; every step is a plain transaction on the origin chain
  (a ``signature`` step is refused, not signed);
* the deposit has calldata: it is what ties the transfer to the request, and
  a bare transfer to the solver would be lost.

Routes are a per-chain registry (:data:`CHAIN_BRIDGE_TOKENS`): a token
bridges to whichever token on the other chain shares its ``asset``, so
ETH↔ETH and the USD stable (USDC on Base, USDG on Robinhood Chain) pair up
without a route list to maintain.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from agentos.trading.chains import CHAINS, NATIVE_ADDRESS, ChainSpec, is_native, normalize_address
from agentos.trading.evm import SEL_APPROVE, USER_AGENT
from agentos.trading.providers import ProviderError

RELAY_BASE_URL = "https://api.relay.link"
RELAY_QUOTE_PATH = "/quote/v2"
RELAY_STATUS_PATH = "/intents/status/v3"
#: Relay always bridges an exact input amount; the output is what the solver pays.
RELAY_TRADE_TYPE = "EXACT_INPUT"
BRIDGE_PROVIDER = "relay"
BRIDGE_PROVIDER_LABEL = "Relay"
#: The Relay solver every deposit is sent to (seen live on Base and Robinhood
#: Chain, both directions, 2026-11-04). The only ``to`` a deposit may name and
#: the only spender an approval may carry; any other address is refused.
RELAY_SOLVERS: frozenset[str] = frozenset({"0x4cd00e387622c35bddb9b4c962c136462338bc31"})
#: Robinhood Chain's canonical USD stable (there is no native USDC there).
USDG_ROBINHOOD = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
#: How much worse than the approved quote the execution re-quote may be.
BRIDGE_TOLERANCE_PCT = 1.0
#: A quote older than this is re-fetched before signing.
BRIDGE_QUOTE_TTL_S = 30.0
#: Fill polling: backoff per attempt, then the last value forever.
BRIDGE_FILL_POLL_S: tuple[float, ...] = (2.0, 3.0, 5.0, 8.0, 13.0, 20.0, 30.0)
#: An in-process fill tracker gives up after this; ``tick`` keeps polling.
BRIDGE_FILL_TASK_S = 30 * 60.0
#: No fill after this long since the order was created → ``failed``.
BRIDGE_FILL_GIVE_UP_S = 6 * 3600.0
#: ``trading.bridge.status`` re-reads Relay at most this often per order.
BRIDGE_STATUS_MIN_INTERVAL_S = 2.0
#: Relay statuses that end an intent.
FILL_FINAL = frozenset({"success", "failure", "refund"})
FILL_STATUSES = frozenset({"waiting", "pending", "success", "failure", "refund", "unknown"})


class BridgeError(ProviderError):
    """A Relay failure or a refused route, with the engine's error code."""


@dataclass(frozen=True)
class BridgeToken:
    asset: str
    address: str
    symbol: str
    decimals: int

    def to_dict(self) -> dict[str, Any]:
        return {"address": self.address, "symbol": self.symbol, "decimals": self.decimals}


#: Per-chain bridgeable tokens. A token bridges to the token on the other
#: chain with the same ``asset``; this is the only table to edit for a new
#: chain or asset (the chain must also exist in ``chains.CHAINS`` and on Relay).
CHAIN_BRIDGE_TOKENS: dict[int, tuple[BridgeToken, ...]] = {
    8453: (
        BridgeToken("ETH", NATIVE_ADDRESS, "ETH", 18),
        BridgeToken("USD", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "USDC", 6),
    ),
    4663: (
        BridgeToken("ETH", NATIVE_ADDRESS, "ETH", 18),
        BridgeToken("USD", USDG_ROBINHOOD, "USDG", 6),
    ),
}


@dataclass(frozen=True)
class BridgeRoute:
    origin: ChainSpec
    destination: ChainSpec
    token_in: BridgeToken
    token_out: BridgeToken

    @property
    def asset(self) -> str:
        return self.token_in.asset

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "originChainId": self.origin.chain_id,
            "destinationChainId": self.destination.chain_id,
            "tokenIn": self.token_in.to_dict(),
            "tokenOut": self.token_out.to_dict(),
        }


def list_routes() -> list[dict[str, Any]]:
    """Every directed route the registry implies, for error details and docs."""
    routes: list[dict[str, Any]] = []
    for from_id, from_tokens in CHAIN_BRIDGE_TOKENS.items():
        for to_id, to_tokens in CHAIN_BRIDGE_TOKENS.items():
            if from_id == to_id:
                continue
            for origin in from_tokens:
                dest = next((t for t in to_tokens if t.asset == origin.asset), None)
                if dest is None:
                    continue
                routes.append(
                    {
                        "from": CHAINS[from_id].key,
                        "to": CHAINS[to_id].key,
                        "tokenIn": origin.symbol,
                        "tokenOut": dest.symbol,
                    }
                )
    return routes


def _route_error(message: str) -> BridgeError:
    return BridgeError(
        "trading.bridge.unsupported_route",
        message,
        details={"supportedRoutes": list_routes()},
    )


def _token_key(token: str) -> str:
    """A symbol as upper-case text, an address lower-cased, native as the zero address."""
    text = str(token or "").strip()
    if not text:
        return ""
    if text.startswith("0x") and len(text) == 42:
        return NATIVE_ADDRESS if is_native(text) else normalize_address(text)
    return text.upper()


def resolve_route(origin: ChainSpec, destination: ChainSpec, token: str) -> BridgeRoute:
    """The route for ``token`` from ``origin`` to ``destination``, or a refusal.

    ``token`` is a registry symbol (``ETH``, ``USDC``, ``USDG``) or an
    address; the destination token's own symbol is accepted too (``USDC``
    from Robinhood Chain means its USD stable, USDG), so a caller can name
    either side of the pair.
    """
    from_tokens = CHAIN_BRIDGE_TOKENS.get(origin.chain_id)
    to_tokens = CHAIN_BRIDGE_TOKENS.get(destination.chain_id)
    if from_tokens is None or to_tokens is None or origin.chain_id == destination.chain_id:
        raise _route_error(
            f"no bridge from {origin.name} to {destination.name}; "
            "bridges run between Base and Robinhood Chain only"
        )
    key = _token_key(token)
    found = next((t for t in from_tokens if key in (t.address, t.symbol)), None)
    if found is None:
        # The caller may have named the token by what it is called on the
        # other side ("bridge my USDC back to Base" from Robinhood Chain).
        twin = next((t for t in to_tokens if key in (t.address, t.symbol)), None)
        if twin is not None:
            found = next((t for t in from_tokens if t.asset == twin.asset), None)
    if found is None:
        names = ", ".join(t.symbol for t in from_tokens)
        raise _route_error(f"{token} cannot be bridged from {origin.name}; only {names} can")
    dest = next((t for t in to_tokens if t.asset == found.asset), None)
    if dest is None:  # pragma: no cover - the registry pairs every asset today
        raise _route_error(f"{found.symbol} has no counterpart on {destination.name}")
    return BridgeRoute(origin=origin, destination=destination, token_in=found, token_out=dest)


def _f(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(str(value))
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _extract_request_id(quote: dict[str, Any]) -> str | None:
    for step in quote.get("steps") or []:
        if isinstance(step, dict):
            if step.get("requestId"):
                return str(step["requestId"])
            for item in step.get("items") or []:
                data = item.get("data") if isinstance(item, dict) else None
                if isinstance(data, dict) and data.get("requestId"):
                    return str(data["requestId"])
    rid = quote.get("requestId")
    return str(rid) if rid else None


#: Relay's non-overlapping top-level fee buckets: ``relayer`` already equals
#: ``relayerGas + relayerService``, and ``subsidized`` is a discount.
_FEE_KEYS = ("gas", "relayer", "app")


def _fees(fees: Any) -> tuple[float | None, dict[str, float | None]]:
    if not isinstance(fees, dict):
        return None, {}
    parts: dict[str, float | None] = {}
    total = 0.0
    found = False
    for key in _FEE_KEYS:
        bucket = fees.get(key)
        usd = _f(bucket.get("amountUsd")) if isinstance(bucket, dict) else None
        parts[f"{key}Usd"] = usd
        if usd is not None:
            total += usd
            found = True
    return (round(total, 6) if found else None), parts


@dataclass
class RelayQuote:
    """One ``/quote/v2`` body, read into what the engine needs.

    ``steps`` are the raw Relay steps; :func:`validate_steps` turns them
    into the transactions the vault signs, refusing anything off-pattern.
    """

    request_id: str | None
    route: BridgeRoute
    wallet: str
    amount_raw: int
    expected_out_raw: int | None
    rate: float | None
    fees_usd: float | None
    fees: dict[str, float | None]
    time_estimate_s: float | None
    amount_in_usd: float | None
    amount_out_usd: float | None
    steps: list[dict[str, Any]]
    fetched_at: float

    @property
    def solver(self) -> str | None:
        """Where the deposit goes: the first incomplete ``deposit`` item's ``to``."""
        for step in self.steps:
            if str(step.get("id") or "") != "deposit":
                continue
            for item in step.get("items") or []:
                data = item.get("data") if isinstance(item, dict) else None
                if isinstance(data, dict) and data.get("to"):
                    return str(data["to"]).lower()
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "solver": self.solver,
            "expectedOutRaw": str(self.expected_out_raw) if self.expected_out_raw else None,
            "rate": self.rate,
            "feesUsd": self.fees_usd,
            "fees": self.fees,
            "timeEstimateSeconds": self.time_estimate_s,
            "amountInUsd": self.amount_in_usd,
            "amountOutUsd": self.amount_out_usd,
            "fetchedAt": self.fetched_at,
            "steps": self.steps,
        }


def normalize_quote(
    body: dict[str, Any], *, route: BridgeRoute, wallet: str, amount_raw: int
) -> RelayQuote:
    """Read a Relay quote and check it describes the order that was asked for."""
    details = _dict(body.get("details"))
    currency_in = _dict(details.get("currencyIn"))
    currency_out = _dict(details.get("currencyOut"))
    quoted_in = _int(currency_in.get("amount"))
    if quoted_in is not None and quoted_in != amount_raw:
        raise BridgeError(
            "trading.bridge.provider",
            f"Relay quoted {quoted_in} base units, the order is {amount_raw}",
        )
    out_currency = _dict(currency_out.get("currency"))
    out_chain = _int(out_currency.get("chainId"))
    out_address = str(out_currency.get("address") or "").lower()
    if out_chain is not None and out_chain != route.destination.chain_id:
        raise BridgeError(
            "trading.bridge.provider",
            f"Relay quoted a fill on chain {out_chain}, not {route.destination.chain_id}",
        )
    if out_address and out_address != route.token_out.address:
        raise BridgeError(
            "trading.bridge.provider",
            f"Relay quoted {out_address} on {route.destination.name}, "
            f"not {route.token_out.symbol} ({route.token_out.address})",
        )
    recipient = str(details.get("recipient") or "").lower()
    if recipient and recipient != wallet.lower():
        raise BridgeError("trading.bridge.provider", f"Relay would pay {recipient}, not the wallet")
    fees_usd, parts = _fees(body.get("fees"))
    steps = [s for s in (body.get("steps") or []) if isinstance(s, dict)]
    return RelayQuote(
        request_id=_extract_request_id(body),
        route=route,
        wallet=wallet.lower(),
        amount_raw=amount_raw,
        expected_out_raw=_int(currency_out.get("amount")),
        rate=_f(details.get("rate")),
        fees_usd=fees_usd,
        fees=parts,
        time_estimate_s=_f(details.get("timeEstimate")),
        amount_in_usd=_f(currency_in.get("amountUsd")),
        amount_out_usd=_f(currency_out.get("amountUsd")),
        steps=steps,
        fetched_at=time.time(),
    )


def _refuse(message: str) -> BridgeError:
    return BridgeError("trading.bridge.unsupported_step", message)


def _tx_from(item: dict[str, Any], *, route: BridgeRoute, wallet: str) -> dict[str, Any]:
    data = item.get("data")
    if not isinstance(data, dict) or not data.get("to"):
        raise _refuse("a Relay step carries no transaction")
    chain_id = _int(data.get("chainId"))
    if chain_id is not None and chain_id != route.origin.chain_id:
        raise _refuse(f"a Relay step runs on chain {chain_id}, not {route.origin.chain_id}")
    sender = str(data.get("from") or "").lower()
    if sender and sender != wallet.lower():
        raise _refuse(f"a Relay step is signed by {sender}, not the wallet")
    value = _int(data.get("value")) or 0
    tx: dict[str, Any] = {
        "from": wallet,
        "to": normalize_address(str(data["to"])),
        "data": str(data.get("data") or "0x"),
        "value": str(value),
        "chainId": route.origin.chain_id,
    }
    gas = _int(data.get("gas"))
    if gas:
        tx["gasLimit"] = gas
    return tx


def validate_steps(
    quote: RelayQuote, *, route: BridgeRoute, wallet: str, amount_raw: int
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The transactions to sign, ``(approve or None, deposit)``, or a refusal.

    Only the approve → deposit pattern is signed. The deposit must go to a
    pinned solver with calldata, carry the order's value when native and
    none otherwise; the approval must be on the bridged token, for the
    order's amount at most, to that same solver (the service re-encodes it
    for exactly the amount, so Relay's bytes never reach the signer).
    """
    approve: dict[str, Any] | None = None
    deposit: dict[str, Any] | None = None
    for step in quote.steps:
        kind = str(step.get("kind") or "transaction")
        step_id = str(step.get("id") or "")
        if kind != "transaction":
            raise _refuse(f'Relay asks for a "{kind}" step ({step_id or "unnamed"})')
        if step_id not in ("approve", "deposit"):
            raise _refuse(f'Relay asks for an unknown step "{step_id or "unnamed"}"')
        for item in step.get("items") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "") == "complete":
                continue
            tx = _tx_from(item, route=route, wallet=wallet)
            if step_id == "approve":
                if approve is not None:
                    raise _refuse("Relay asks for two approvals")
                approve = tx
            else:
                if deposit is not None:
                    raise _refuse("Relay asks for two deposits")
                deposit = tx
    if deposit is None:
        raise _refuse("Relay returned no deposit transaction")
    if deposit["to"] not in RELAY_SOLVERS:
        raise BridgeError(
            "trading.tx_failed",
            f"deposit target {deposit['to']} is not a Relay solver this desk trusts",
        )
    if len(deposit["data"]) < 10:
        raise _refuse("the deposit carries no calldata (nothing would tie it to the request)")
    native = is_native(route.token_in.address)
    value = int(deposit["value"])
    if native:
        if value != amount_raw:
            raise BridgeError(
                "trading.tx_failed",
                f"deposit value {value} is not the order's {amount_raw}",
            )
        if approve is not None:
            raise _refuse("Relay asks for an approval on a native bridge")
    else:
        if value != 0:
            raise BridgeError("trading.tx_failed", "an ERC-20 deposit carries native value")
        if approve is not None:
            if approve["to"] != route.token_in.address:
                raise BridgeError("trading.tx_failed", "approval is not on the token being bridged")
            data = approve["data"]
            if not data.startswith(SEL_APPROVE) or len(data) != 2 + 8 + 64 + 64:
                raise BridgeError(
                    "trading.tx_failed", "approval calldata is not approve(address,uint256)"
                )
            spender = "0x" + data[10 + 24 : 10 + 64]
            if spender.lower() != deposit["to"]:
                raise BridgeError(
                    "trading.tx_failed",
                    f"approval spender {spender} is not the deposit target",
                )
            if int(data[10 + 64 :], 16) > amount_raw:
                raise BridgeError("trading.tx_failed", "approval amount exceeds the order's amount")
    return approve, deposit


def fill_status_from(body: Any) -> dict[str, Any]:
    """Relay's ``/intents/status/v3`` body as the engine's fill record."""
    payload = body if isinstance(body, dict) else {}
    status = str(payload.get("status") or "").strip().lower()
    if status not in FILL_STATUSES:
        status = "unknown"
    in_hashes = payload.get("inTxHashes") or []
    out_hashes = payload.get("txHashes") or []
    return {
        "status": status,
        "originTxHash": str(in_hashes[0]).lower() if in_hashes else None,
        "destinationTxHash": str(out_hashes[0]).lower() if out_hashes else None,
        "details": payload.get("details"),
    }


class RelayClient:
    """The two Relay endpoints a bridge needs, over a shared ``httpx`` client."""

    def __init__(
        self,
        *,
        base_url: str = RELAY_BASE_URL,
        http: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
        max_retries: int = 2,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self.base_url = (base_url or RELAY_BASE_URL).rstrip("/")
        self._own_http = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout)
        self._timeout = timeout
        self._max_retries = max(0, int(max_retries))
        self._sleep = sleep or asyncio.sleep

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()

    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "user-agent": USER_AGENT,
        }

    async def _request(
        self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            try:
                response = await self._http.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    headers=self._headers(),
                    timeout=self._timeout,
                )
            except httpx.HTTPError as exc:
                if attempt < self._max_retries:
                    attempt += 1
                    await self._sleep(0.5 * attempt)
                    continue
                raise BridgeError(
                    "trading.bridge.provider", f"Relay unreachable: {exc}", retryable=True
                ) from exc
            try:
                body: Any = response.json()
            except ValueError:
                body = None
            if response.status_code >= 400:
                error = self._error(body, response.status_code)
                if error.retryable and attempt < self._max_retries:
                    attempt += 1
                    await self._sleep(0.5 * attempt)
                    continue
                raise error
            if not isinstance(body, dict):
                raise BridgeError("trading.bridge.provider", "Relay returned no JSON")
            return body

    @staticmethod
    def _error(body: Any, status: int) -> BridgeError:
        payload = body if isinstance(body, dict) else {}
        message = str(payload.get("message") or payload.get("error") or "").strip()
        upstream = str(payload.get("errorCode") or "").strip().upper()
        details: dict[str, Any] = {"relayCode": upstream or f"HTTP_{status}"}
        text = f"{upstream} {message}".lower()
        if status < 500 and any(
            word in text for word in ("route", "unsupported", "not supported", "no_quotes")
        ):
            details["supportedRoutes"] = list_routes()
            return BridgeError(
                "trading.bridge.unsupported_route",
                message or "Relay has no route for this bridge",
                details=details,
            )
        return BridgeError(
            "trading.bridge.provider",
            message or f"Relay request failed ({status})",
            retryable=status >= 500 or status == 429,
            details=details,
        )

    async def quote(self, *, route: BridgeRoute, wallet: str, amount_raw: int) -> RelayQuote:
        body = await self._request(
            "POST",
            RELAY_QUOTE_PATH,
            json={
                "user": wallet,
                "recipient": wallet,
                "originChainId": route.origin.chain_id,
                "destinationChainId": route.destination.chain_id,
                "originCurrency": route.token_in.address,
                "destinationCurrency": route.token_out.address,
                "amount": str(int(amount_raw)),
                "tradeType": RELAY_TRADE_TYPE,
            },
        )
        return normalize_quote(body, route=route, wallet=wallet, amount_raw=amount_raw)

    async def status(self, request_id: str) -> dict[str, Any]:
        if not request_id:
            raise BridgeError("trading.invalid", "requestId is required")
        body = await self._request("GET", RELAY_STATUS_PATH, params={"requestId": request_id})
        return fill_status_from(body)
