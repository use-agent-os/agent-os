"""Bridging between Base and Robinhood Chain through AgentOS DEX.

The bridge is the same HTTP API the swaps use (``aggregator.py``, served at
``https://agg.useagentos.dev``): ``GET /v1/bridge/quote`` asks Relay and
Across at once and answers with the better offer, and ``GET
/v1/bridge/status`` says how far a transfer has got. A firm quote (one with
a ``taker``) carries a single unsigned transaction: a call to
``AgentOSBridge.bridge(BridgeParams)``. That contract pulls the order's
amount from the wallet, keeps the AgentOS fee, and deposits the rest into
the bridge's own contract on the wallet's behalf. The service holds no key
and never broadcasts: funds move only when the vault signs that call.

What is checked here before anything is signed mirrors the aggregator's
discipline:

* the call is sent ``to`` a bridge contract this desk has pinned
  (:data:`TRUSTED_BRIDGES`, plus any the operator added with
  ``trading.aggregator_trusted_spenders``) — never an address merely read
  from the response — on the origin chain, with exactly the order's value
  when native and none otherwise;
* the calldata is ``bridge(BridgeParams)`` and is decoded, not trusted: the
  token and amount are the order's, the fee is what the quote says and
  under :data:`BRIDGE_MAX_FEE_BPS`, the destination chain is the route's,
  the recipient is the wallet, the bridge named is the quote's, and the
  deadline has not passed;
* the approval, when one is needed, is never read from the response at
  all: the engine encodes ``approve(bridge, amount)`` for exactly the
  order's amount.

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

from agentos.trading.aggregator import AGGREGATOR_BASE, NATIVE_SENTINEL
from agentos.trading.chains import CHAINS, NATIVE_ADDRESS, ChainSpec, is_native, normalize_address
from agentos.trading.evm import USER_AGENT
from agentos.trading.providers import ProviderError

BRIDGE_QUOTE_PATH = "/v1/bridge/quote"
BRIDGE_STATUS_PATH = "/v1/bridge/status"
#: The bridges AgentOS DEX asks. A quote names the one it is built from, and
#: that name is the order's ``provider``.
SOURCE_RELAY = "relay"
SOURCE_ACROSS = "across"
BRIDGE_SOURCES: tuple[str, ...] = (SOURCE_RELAY, SOURCE_ACROSS)
BRIDGE_SOURCE_LABELS: dict[str, str] = {SOURCE_RELAY: "Relay", SOURCE_ACROSS: "Across"}
#: What an order made before the bridge moved behind AgentOS DEX ran through.
BRIDGE_PROVIDER = SOURCE_RELAY
BRIDGE_PROVIDER_LABEL = BRIDGE_SOURCE_LABELS[BRIDGE_PROVIDER]
#: AgentOSBridge (use-agent-os/agent-os-dex-contracts): the contract every
#: bridge transaction is sent to and the only spender an approval names. It
#: takes the AgentOS fee on-chain and deposits the rest into Relay or Across.
#: The same address on every chain (seen live on Base and Robinhood Chain,
#: 2026-11-22).
AGENTOS_BRIDGE = "0xe7b13ae215b1aa7b5f42ac0cafaffdd54441bc20"
#: The built-in bridge contracts; ``trading.aggregator_trusted_spenders`` can
#: add to them, never remove.
TRUSTED_BRIDGES: frozenset[str] = frozenset({AGENTOS_BRIDGE})
BRIDGE_CONTRACT_LABEL = "AgentOS Bridge"
#: The mark a plan carries when it was quoted through AgentOS DEX. One without
#: it predates the move and deposited straight to the Relay solver.
BRIDGE_VIA = "agentos-dex"
#: ``bridge((bytes32,address,uint256,uint256,bytes32,uint256,address,uint256))``.
BRIDGE_SELECTOR = "0xea91bd0c"
#: The gateway refuses to be configured above 1%; so does this desk.
BRIDGE_MAX_FEE_BPS = 100
#: A bridge commits to a floor a little under what it expects to deliver
#: (2–3% on Relay). One further away than this is not a quote worth signing.
BRIDGE_MAX_COMMIT_GAP_PCT = 5.0
#: The deadline in the calldata is ten minutes out; one beyond this is refused.
BRIDGE_MAX_DEADLINE_S = 3600.0
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
#: ``trading.bridge.status`` re-reads the fill at most this often per order.
BRIDGE_STATUS_MIN_INTERVAL_S = 2.0
#: A ``failure`` is often not the last word (Relay may still retry or refund
#: it): the order only fails once the bridge has said so for this long.
BRIDGE_FAILURE_GRACE_S = 120.0
#: Fill statuses that end a transfer.
FILL_FINAL = frozenset({"success", "failure", "refund"})
FILL_STATUSES = frozenset({"waiting", "pending", "success", "failure", "refund", "unknown"})
#: AgentOS DEX's five words for a transfer, as the engine's fill statuses.
_DEX_FILL_STATUS: dict[str, str] = {
    "pending": "pending",
    "filled": "success",
    "refunded": "refund",
    "failed": "failure",
    "unknown": "unknown",
}


class BridgeError(ProviderError):
    """A bridge failure or a refused route, with the engine's error code."""


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
#: chain or asset (the chain must also exist in ``chains.CHAINS`` and on AgentOS DEX).
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


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = int(str(value))
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _bytes32(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if len(text) != 66 or not text.startswith("0x"):
        return None
    try:
        int(text, 16)
    except ValueError:
        return None
    return text


def token_param(address: str) -> str:
    """Our zero-address native sentinel becomes AgentOS DEX's own (``0xeeee…``)."""
    return NATIVE_SENTINEL.lower() if is_native(address) else normalize_address(address)


def _same_token(reported: Any, expected: str) -> bool:
    return str(reported or "").strip().lower() == token_param(expected)


def source_label(source: Any) -> str:
    """``relay`` → ``Relay``; an unknown source is shown as it came."""
    key = str(source or "").strip().lower()
    return BRIDGE_SOURCE_LABELS.get(key) or (key.capitalize() if key else BRIDGE_PROVIDER_LABEL)


def _source_id(source: str) -> str:
    """The ``bytes32`` AgentOSBridge switches on: the ASCII label, left-aligned."""
    return source.encode("ascii").hex().ljust(64, "0")


def _wrong(message: str) -> BridgeError:
    """The gateway answered for some other order than the one that was asked for."""
    return BridgeError("trading.bridge.provider", message)


def _refuse(message: str) -> BridgeError:
    return BridgeError("trading.bridge.unsupported_step", message)


def _route_rows(rows: Any) -> list[dict[str, Any]]:
    """The gateway's comparison of both bridges, kept to the fields it documents."""
    out: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not row.get("source"):
            continue
        item: dict[str, Any] = {
            "source": str(row["source"]),
            "label": source_label(row["source"]),
            "available": bool(row.get("available")),
            "selected": bool(row.get("selected")),
        }
        if item["available"]:
            buy = _int(row.get("buyAmount"))
            floor = _int(row.get("minBuyAmount"))
            item.update(
                {
                    "expectedOutRaw": str(buy) if buy is not None else None,
                    "minBuyRaw": str(floor) if floor is not None else None,
                    "timeEstimateSeconds": row.get("estimatedTimeSeconds"),
                    "refundGuaranteed": bool(row.get("refundGuaranteed")),
                }
            )
        else:
            item["reason"] = str(row.get("reason") or "") or None
        out.append(item)
    return out


@dataclass
class BridgeQuote:
    """One ``/v1/bridge/quote`` body, read into what the engine needs.

    Indicative when it was asked without a wallet: the amounts are real, but
    there is no order, nothing to track and nothing to sign. A firm one
    carries ``transaction``, which :func:`bridge_transaction` turns into the
    call the vault signs, refusing anything off-pattern.
    """

    source: str
    route: BridgeRoute
    wallet: str | None
    amount_raw: int
    fee_bps: int
    fee_raw: int
    net_raw: int
    expected_out_raw: int
    #: The floor the bridge itself commits to (the gateway's ``minBuyAmount``).
    min_buy_raw: int
    time_estimate_s: float | None
    refund_guaranteed: bool
    expires_at: int | None
    order_id: str | None
    tracking_id: str | None
    allowance_target: str | None
    transaction: dict[str, Any] | None
    routes: list[dict[str, Any]]
    fetched_at: float

    @property
    def firm(self) -> bool:
        return self.transaction is not None

    @property
    def request_id(self) -> str | None:
        """What the engine has always called the id a fill is tracked by."""
        return self.tracking_id

    @property
    def bridge(self) -> str | None:
        """The contract the transaction is sent to; ``None`` on an indicative quote."""
        to = (self.transaction or {}).get("to")
        return str(to).lower() if to else None

    @property
    def rate(self) -> float | None:
        """Destination units delivered per origin unit bridged, fees included."""
        if self.amount_raw <= 0:
            return None
        scale = 10.0 ** (self.route.token_in.decimals - self.route.token_out.decimals)
        return round(float(self.expected_out_raw) * scale / self.amount_raw, 8)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requestId": self.tracking_id,
            "orderId": self.order_id,
            "source": self.source,
            "sourceLabel": source_label(self.source),
            "bridge": self.bridge,
            "expectedOutRaw": str(self.expected_out_raw),
            "minBuyRaw": str(self.min_buy_raw),
            "rate": self.rate,
            "fee": {"bps": self.fee_bps, "amountRaw": str(self.fee_raw)},
            "netAmountRaw": str(self.net_raw),
            "timeEstimateSeconds": self.time_estimate_s,
            "refundGuaranteed": self.refund_guaranteed,
            "expiresAt": self.expires_at,
            "routes": self.routes,
            "fetchedAt": self.fetched_at,
        }


def parse_quote(
    body: dict[str, Any], *, route: BridgeRoute, wallet: str | None, amount_raw: int
) -> BridgeQuote:
    """Read a bridge quote and check it describes the order that was asked for."""
    if _int(body.get("originChainId")) != route.origin.chain_id:
        raise _wrong(f"the quote leaves chain {body.get('originChainId')}, not {route.origin.name}")
    if _int(body.get("destinationChainId")) != route.destination.chain_id:
        raise _wrong(
            f"the quote arrives on chain {body.get('destinationChainId')}, "
            f"not {route.destination.name}"
        )
    sell = _dict(body.get("sellToken"))
    buy = _dict(body.get("buyToken"))
    if not _same_token(sell.get("address"), route.token_in.address):
        raise _wrong(f"the quote sells {sell.get('address')}, not {route.token_in.symbol}")
    if not _same_token(buy.get("address"), route.token_out.address):
        raise _wrong(
            f"the quote delivers {buy.get('address')} on {route.destination.name}, "
            f"not {route.token_out.symbol} ({route.token_out.address})"
        )
    quoted_in = _int(body.get("sellAmount"))
    if quoted_in != amount_raw:
        raise _wrong(f"the quote is for {quoted_in} base units, the order is {amount_raw}")
    source = str(body.get("source") or "").strip().lower()
    if source not in BRIDGE_SOURCES:
        raise _wrong(f'the quote names an unknown bridge "{source}"')
    fee = _dict(body.get("fee"))
    fee_bps = _int(fee.get("bps"))
    fee_raw = _int(fee.get("amount"))
    net_raw = _int(body.get("netAmount"))
    if fee_bps is None or fee_raw is None or net_raw is None:
        raise _wrong("the quote does not say what the bridge fee is")
    if fee_bps > BRIDGE_MAX_FEE_BPS:
        raise _wrong(f"the quote takes a {fee_bps} bps fee, above the {BRIDGE_MAX_FEE_BPS} bps cap")
    if fee_raw != amount_raw * fee_bps // 10_000 or net_raw != amount_raw - fee_raw:
        raise _wrong(
            f"the quote's fee ({fee_raw}) and net amount ({net_raw}) "
            f"are not {fee_bps} bps of {amount_raw}"
        )
    expected = _int(body.get("buyAmount"))
    floor = _int(body.get("minBuyAmount"))
    if not expected or floor is None:
        raise _wrong("the quote names no destination amount")
    if floor < expected * (1 - BRIDGE_MAX_COMMIT_GAP_PCT / 100.0):
        raise _wrong(
            f"the bridge commits to {floor} of the {expected} it expects to deliver, "
            f"more than {BRIDGE_MAX_COMMIT_GAP_PCT:g}% short"
        )
    refund = _dict(body.get("refund"))
    transaction = body.get("transaction")
    tracking_id = _bytes32(body.get("trackingId"))
    order_id = _bytes32(body.get("orderId"))
    if wallet is not None:
        if not isinstance(transaction, dict):
            raise _wrong("the quote carries no transaction to sign")
        if tracking_id is None or order_id is None:
            raise _wrong("the quote carries no order id to track the transfer by")
        if str(refund.get("address") or "").lower() != wallet.lower():
            raise _wrong(f"a failed transfer would refund {refund.get('address')}, not the wallet")
    target = body.get("allowanceTarget")
    eta = body.get("estimatedTimeSeconds")
    return BridgeQuote(
        source=source,
        route=route,
        wallet=wallet.lower() if wallet else None,
        amount_raw=amount_raw,
        fee_bps=fee_bps,
        fee_raw=fee_raw,
        net_raw=net_raw,
        expected_out_raw=expected,
        min_buy_raw=min(floor, expected),
        time_estimate_s=float(eta) if isinstance(eta, int | float) else None,
        refund_guaranteed=bool(refund.get("guaranteed")),
        expires_at=_int(body.get("expiresAt")),
        order_id=order_id,
        tracking_id=tracking_id,
        allowance_target=str(target).lower() if target else None,
        transaction=dict(transaction) if isinstance(transaction, dict) and wallet else None,
        routes=_route_rows(body.get("routes")),
        fetched_at=time.time(),
    )


def bridge_transaction(
    quote: BridgeQuote,
    *,
    route: BridgeRoute,
    wallet: str,
    amount_raw: int,
    trusted: frozenset[str] = TRUSTED_BRIDGES,
    now: float | None = None,
) -> dict[str, Any]:
    """The one call to sign, or a refusal.

    The transaction must go to a pinned bridge contract on the origin chain,
    carry the order's value when native and none otherwise, and its calldata
    must decode to ``bridge(BridgeParams)`` for exactly this order: this
    token and amount, the quote's fee and bridge, the route's destination
    chain, this wallet as the recipient, a deadline still ahead. The approval
    is not part of the answer: the engine encodes its own.
    """
    tx = quote.transaction
    if tx is None:
        raise _refuse("the bridge quote carries no transaction (it was indicative)")
    chain_id = _int(tx.get("chainId"))
    if chain_id is not None and chain_id != route.origin.chain_id:
        raise _refuse(f"the bridge call runs on chain {chain_id}, not {route.origin.chain_id}")
    try:
        to = normalize_address(str(tx.get("to") or ""))
    except ValueError as exc:
        raise _refuse("the bridge call names no contract") from exc
    if to not in {a.lower() for a in trusted}:
        raise BridgeError(
            "trading.tx_failed",
            f"bridge target {to} is not an AgentOS bridge contract this desk trusts",
        )
    native = is_native(route.token_in.address)
    value = _int(tx.get("value")) or 0
    if native:
        if value != amount_raw:
            raise BridgeError(
                "trading.tx_failed", f"bridge value {value} is not the order's {amount_raw}"
            )
        if quote.allowance_target is not None:
            raise _refuse("the gateway asks for an approval on a native bridge")
    else:
        if value != 0:
            raise BridgeError("trading.tx_failed", "an ERC-20 bridge carries native value")
        if quote.allowance_target != to:
            raise BridgeError(
                "trading.tx_failed",
                f"approval spender {quote.allowance_target} is not the bridge contract",
            )
    data = str(tx.get("data") or "").lower()
    if not data.startswith(BRIDGE_SELECTOR) or len(data) != 10 + 8 * 64:
        raise _refuse("the bridge calldata is not bridge(BridgeParams)")
    try:
        words = [data[10 + i * 64 : 10 + (i + 1) * 64] for i in range(8)]
        numbers = [int(word, 16) for word in words]
    except ValueError as exc:
        raise _refuse("the bridge calldata is not hex") from exc
    source_id, token, _amount, _net, order_id, _dest, recipient, _deadline = words
    amount_in, net, dest_chain, deadline = numbers[2], numbers[3], numbers[5], numbers[7]
    if source_id != _source_id(quote.source):
        raise BridgeError("trading.tx_failed", f"the calldata does not deposit into {quote.source}")
    if "0x" + token[24:] != token_param(route.token_in.address) or int(token[:24], 16):
        raise BridgeError("trading.tx_failed", "the calldata bridges another token")
    if amount_in != amount_raw:
        raise BridgeError(
            "trading.tx_failed", f"the calldata pulls {amount_in}, the order is {amount_raw}"
        )
    if net != quote.net_raw:
        raise BridgeError(
            "trading.tx_failed", f"the calldata bridges {net}, the quote said {quote.net_raw}"
        )
    if "0x" + order_id != quote.order_id or not int(order_id, 16):
        raise BridgeError("trading.tx_failed", "the calldata carries another order id")
    if dest_chain != route.destination.chain_id:
        raise BridgeError(
            "trading.tx_failed",
            f"the calldata delivers on chain {dest_chain}, not {route.destination.chain_id}",
        )
    if "0x" + recipient[24:] != wallet.lower() or int(recipient[:24], 16):
        raise BridgeError("trading.tx_failed", "the calldata pays another address than the wallet")
    moment = time.time() if now is None else now
    if deadline <= moment:
        raise BridgeError("trading.bridge.provider", "the bridge quote has expired", retryable=True)
    if deadline > moment + BRIDGE_MAX_DEADLINE_S:
        raise BridgeError("trading.tx_failed", "the calldata's deadline is too far ahead")
    return {
        "from": wallet,
        "to": to,
        "data": data,
        "value": str(value),
        "chainId": route.origin.chain_id,
    }


def fill_status_from(body: Any) -> dict[str, Any]:
    """A ``/v1/bridge/status`` body as the engine's fill record."""
    payload = body if isinstance(body, dict) else {}
    status = _DEX_FILL_STATUS.get(str(payload.get("status") or "").strip().lower(), "unknown")
    origin = payload.get("originTxHash")
    destination = payload.get("destinationTxHash")
    return {
        "status": status,
        "originTxHash": str(origin).lower() if origin else None,
        "destinationTxHash": str(destination).lower() if destination else None,
    }


#: The gateway's error envelope maps onto the engine's own codes. The bool is
#: whether retrying the same request could plausibly work.
_ERROR_CODES: dict[str, tuple[str, bool]] = {
    "INVALID_REQUEST": ("trading.invalid", False),
    "UNSUPPORTED_CHAIN": ("trading.bridge.unsupported_route", False),
    "UNSUPPORTED_BRIDGE_PAIR": ("trading.bridge.unsupported_route", False),
    "NO_BRIDGE_ROUTE": ("trading.bridge.no_route", False),
    "BRIDGE_DISABLED": ("trading.bridge.provider", False),
    "SOURCE_UNAVAILABLE": ("trading.bridge.provider", False),
    "MISCONFIGURED": ("trading.bridge.provider", False),
    "RATE_LIMITED": ("trading.bridge.provider", True),
    "UPSTREAM_REJECTED": ("trading.bridge.provider", False),
    "UPSTREAM_RATE_LIMITED": ("trading.bridge.provider", True),
    "UPSTREAM_ERROR": ("trading.bridge.provider", True),
    "INTERNAL_ERROR": ("trading.bridge.provider", True),
}


class DexBridgeClient:
    """The two AgentOS DEX bridge endpoints, over a shared ``httpx`` client."""

    def __init__(
        self,
        *,
        base_url: str = AGGREGATOR_BASE,
        http: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
        max_retries: int = 2,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self.base_url = (base_url or AGGREGATOR_BASE).rstrip("/")
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
        return {"accept": "application/json", "user-agent": USER_AGENT}

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            try:
                response = await self._http.get(
                    url, params=params, headers=self._headers(), timeout=self._timeout
                )
            except httpx.HTTPError as exc:
                if attempt < self._max_retries:
                    attempt += 1
                    await self._sleep(0.5 * attempt)
                    continue
                raise BridgeError(
                    "trading.bridge.provider", f"AgentOS DEX unreachable: {exc}", retryable=True
                ) from exc
            try:
                body: Any = response.json()
            except ValueError:
                body = None
            if response.status_code >= 400 or (isinstance(body, dict) and "error" in body):
                error = self._error(body, response.status_code)
                if error.retryable and attempt < self._max_retries:
                    attempt += 1
                    wait = (error.details or {}).get("retryAfterSeconds")
                    delay = float(wait) if isinstance(wait, int | float) else 0.5 * attempt
                    await self._sleep(min(delay, 5.0))
                    continue
                raise error
            if not isinstance(body, dict):
                raise BridgeError("trading.bridge.provider", "AgentOS DEX returned no JSON")
            return body

    @staticmethod
    def _error(body: Any, status: int) -> BridgeError:
        payload = _dict(_dict(body).get("error"))
        upstream = str(payload.get("code") or "").strip().upper()
        message = str(payload.get("message") or "").strip()
        code, retryable = _ERROR_CODES.get(
            upstream, ("trading.bridge.provider", status >= 500 or status == 429)
        )
        details = dict(_dict(payload.get("details")))
        details["aggregatorCode"] = upstream or f"HTTP_{status}"
        retry_after = payload.get("retryAfterSeconds")
        if retry_after is not None:
            details["retryAfterSeconds"] = retry_after
        if code == "trading.bridge.unsupported_route":
            details["supportedRoutes"] = list_routes()
        if code == "trading.bridge.no_route":
            rows = _route_rows(details.get("routes"))
            details["routes"] = rows
            why = ", ".join(f"{r['label']}: {r.get('reason') or 'no offer'}" for r in rows)
            # A bridge that was merely unreachable may answer the next time.
            retryable = any(r.get("reason") == "UPSTREAM_ERROR" for r in rows)
            message = (
                "no bridge has a route for this transfer at this amount"
                + (f" ({why})" if why else "")
                + "; a larger amount often works"
            )
        return BridgeError(
            code,
            message or f"AgentOS DEX bridge request failed ({status})",
            retryable=retryable,
            details=details,
        )

    async def quote(
        self,
        *,
        route: BridgeRoute,
        amount_raw: int,
        wallet: str | None = None,
        source: str | None = None,
    ) -> BridgeQuote:
        """A firm quote when ``wallet`` is given (it signs), else an indicative one.

        ``source`` pins the bridge; left out, the gateway picks whichever
        delivers more.
        """
        params: dict[str, Any] = {
            "originChain": str(route.origin.chain_id),
            "destinationChain": str(route.destination.chain_id),
            "sellToken": token_param(route.token_in.address),
            "buyToken": token_param(route.token_out.address),
            "amount": str(int(amount_raw)),
        }
        if wallet:
            params["taker"] = wallet.lower()
        if source:
            params["source"] = source
        body = await self._get(BRIDGE_QUOTE_PATH, params)
        return parse_quote(body, route=route, wallet=wallet, amount_raw=amount_raw)

    async def status(self, tracking_id: str, *, source: str = BRIDGE_PROVIDER) -> dict[str, Any]:
        if not tracking_id:
            raise BridgeError("trading.invalid", "requestId is required")
        body = await self._get(
            BRIDGE_STATUS_PATH, {"source": source or BRIDGE_PROVIDER, "trackingId": tracking_id}
        )
        return fill_status_from(body)
