"""TradingService: the one object behind ``wallet.*`` and ``trading.*``.

It owns the vault, the ledger, the price service, one EVM client per chain
and the Uniswap client, and runs the swap pipeline:

    resolve tokens → guardrails → (approval tx) → quote → swap calldata →
    sign → broadcast → receipt → ledger (entries, lots, realized, spend)

Agent-initiated swaps above the threshold park as ``awaiting_approval``
orders; the desktop approves or rejects them, and a background task expires
the ones nobody answers. Every state change is broadcast over the gateway
WebSocket so the UI never polls for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

import httpx
import structlog

from agentos import __version__
from agentos.trading import guardrails
from agentos.trading.aggregator import AGGREGATOR_BASE, AggregatorClient, AggregatorProvider
from agentos.trading.chains import (
    CHAINS,
    NATIVE_ADDRESS,
    ChainSpec,
    UnsupportedChainError,
    checksum_address,
    is_native,
    normalize_address,
    redact_rpc_url,
    rpc_url_for,
)
from agentos.trading.decode import (
    decode_calldata,
    describe_call,
    receipt_movements,
    spender_label,
    tx_summary,
)
from agentos.trading.discovery import BlockscoutDiscovery
from agentos.trading.evm import (
    EvmClient,
    EvmRpcError,
    EvmTransportError,
    decode_uint,
    encode_approve,
    encode_transfer,
    is_unlimited,
    pad_uint,
    receipt_gas_wei,
    receipt_succeeded,
    receipt_transfers,
)
from agentos.trading.ledger import (
    LP_ORDER_KINDS,
    ORDER_FINAL_STATUSES,
    Ledger,
    local_day,
    new_batch_id,
    new_order_id,
)
from agentos.trading.pnl import (
    format_amount,
    format_ratio,
    holding_from_lots,
    per_raw,
    to_human,
    to_raw,
)
from agentos.trading.prices import PriceInfo, PriceService, TokenMeta, native_token
from agentos.trading.providers import (
    DEFAULT_PROVIDER_ID,
    PROVIDER_IDS,
    ProbeResult,
    ProviderError,
    ProviderQuote,
    SwapProvider,
    UniswapProvider,
    provider_label,
)
from agentos.trading.spam import MIN_LIQUIDITY_USD, TokenCurator
from agentos.trading.sync import WalletSyncer
from agentos.trading.uniswap import (
    DecisionOrigin,
    UniswapAuthError,
    UniswapClient,
    UniswapError,
    validate_transaction,
)
from agentos.trading.vault import Vault, VaultError, WalletNotFoundError, WalletRecord

log = structlog.get_logger(__name__)

Initiator = Literal["manual", "agent"]
Broadcast = Callable[[str, dict[str, Any]], Awaitable[None]]

RECEIPT_TIMEOUT_S = 180.0
#: A fee-paying LP increase may return up to its planned native fees plus
#: 1/this of them (fees accrue between planning and the transaction).
LP_FEE_GROWTH_TOLERANCE_DIV = 10
# A submitted transaction with no receipt after this long is treated as
# dropped. Until then ``tick`` keeps polling for it.
SUBMITTED_GIVE_UP_S = 6 * 3600.0
# One receipt poll per housekeeping pass for recovered orders.
RECOVER_POLL_S = 5.0
# A load-balanced RPC endpoint does not answer from one node. Measured on
# Base on 2026-09-19: a receipt came back from a node that had the block
# while the very next eth_getBalance and eth_call were served by one that
# did not — an allowance that is provably on chain simulated as "transfer
# amount exceeds allowance", and a settled swap booked as having received
# only its gas back. These bound how long the engine waits for the endpoint
# to agree with itself before it believes a read (``_native_after``,
# ``_await_allowance``).
CHAIN_CATCHUP_ATTEMPTS = 6
CHAIN_CATCHUP_POLL_S = 1.0
# Pause between wallets in a batch so a multi-wallet swap stays under the
# Trading API's per-endpoint rate limit.
BATCH_PAUSE_S = 0.35
# Selling 100% of the gas coin keeps this much back so the swap itself (and
# the next one) can still pay for gas.
NATIVE_GAS_RESERVE_WEI = 10**15
# ``wallet.balances`` with ``refresh`` forces a chain read at most this often
# per wallet; between forced reads the sync loop keeps the ledger current.
MANUAL_SYNC_MIN_S = 10.0
# Hidden (junk) tokens leave the sync's fast lane; their balances and
# openings are still brought up to date this often.
HIDDEN_RESCAN_S = 3600.0
# WETH9 ``withdraw(uint256)``.
SEL_WETH_WITHDRAW = "0x2e1a7d4d"
# A multisend is one decision and one wallet lock; beyond this many legs it
# is a script, not an order.
MAX_SEND_RECIPIENTS = 200
# ``network`` re-reads the chains at most this often; the strip polls faster.
NETWORK_TTL_S = 10.0
# A head block older than this means the endpoint is behind or the chain is
# stalled; either way a read from it is not to be trusted.
STALE_HEAD_S = 60.0
# A transaction the node *refused* at broadcast is almost certainly not on
# chain — but "almost": a load-balanced endpoint can reject on one node
# after another accepted. Such an order is watched this long, not six hours.
REJECTED_GIVE_UP_S = 600.0
# Gas may cost at most the larger of this and this share of the order.
GAS_CEILING_USD = 2.0
GAS_CEILING_SHARE = 0.05
# What a caller's idempotency key may look like.
CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
BROADCAST_REJECTED = "broadcast rejected:"
# Ledger log index for a native (ETH) leg of an LP write: it has no Transfer
# log of its own, and must not collide with an ERC-20 leg's index.
LP_NATIVE_LOG_INDEX = 1_000_000
LP_PROVIDER = "uniswap_v4"
LP_PROVIDER_LABEL = "Uniswap V4"
# ``(chain, event loop) -> lp.ChainEnv``: how LP writes reach the chain (tests swap it).
LpEnvFactory = Callable[[ChainSpec, asyncio.AbstractEventLoop], Any]


def _thin(points: list[dict[str, float]], limit: int) -> list[dict[str, float]]:
    """Even stride down to ``limit`` points, last one always kept.

    A day of 30-second snapshots is 2,880 points for a line a few hundred
    pixels wide. Striding is enough — the shape survives, and it keeps the
    payload and the chart's own work proportional to what is visible.
    """
    if limit <= 0 or len(points) <= limit:
        return points
    step = len(points) / limit
    out = [points[int(i * step)] for i in range(limit)]
    if out[-1] is not points[-1]:
        out[-1] = points[-1]
    return out


def _origin(initiator: str) -> DecisionOrigin:
    return "autonomous" if initiator == "agent" else "human_mediated"


@dataclass(frozen=True)
class ChartRange:
    """One tab on the chart.

    ``local_first`` is the cheap path: the sync loop already writes a price
    snapshot for every held token every ``sync_interval_seconds``, so the
    short windows can be drawn from sqlite without touching the network at
    all. GeckoTerminal is the fallback there, and the primary for the long
    windows the local table cannot reach back to.
    """

    timeframe: str
    limit: int
    seconds: float
    local_first: bool
    #: Most points worth drawing; denser snapshot runs are bucketed down.
    max_points: int = 180


CHART_RANGES: dict[str, ChartRange] = {
    "1h": ChartRange("minute", 60, 3_600, local_first=True, max_points=120),
    "6h": ChartRange("5m", 72, 6 * 3_600, local_first=True),
    "1d": ChartRange("15m", 96, 86_400, local_first=True),
    "1w": ChartRange("hour", 168, 7 * 86_400, local_first=False),
    "all": ChartRange("day", 365, 5 * 365 * 86_400, local_first=False, max_points=365),
}
DEFAULT_CHART_RANGE = "1d"


class TradingError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _err(exc: Exception) -> TradingError:
    """Normalise every subsystem error into a coded TradingError."""
    if isinstance(exc, TradingError):
        return exc
    if isinstance(exc, VaultError):
        return TradingError(getattr(exc, "code", "wallet.error"), str(exc))
    if isinstance(exc, UniswapAuthError):
        return TradingError("trading.no_api_key", str(exc))
    if isinstance(exc, UniswapError):
        return TradingError(exc.code, str(exc), details={"errorCode": exc.error_code})
    if isinstance(exc, ProviderError):
        return TradingError(exc.code, str(exc), details=exc.details)
    if isinstance(exc, UnsupportedChainError):
        return TradingError("trading.unsupported_chain", str(exc))
    if isinstance(exc, EvmRpcError | EvmTransportError):
        return TradingError("trading.rpc", str(exc))
    if isinstance(exc, ValueError):
        return TradingError("trading.invalid", str(exc))
    return TradingError("trading.error", str(exc) or exc.__class__.__name__)


async def _default_broadcast(event: str, payload: dict[str, Any]) -> None:
    """No transport wired (tests, CLI-only use): events are logged, not sent."""
    log.debug("trading.event", event=event)


def _sign_tx(tx: dict[str, Any], key: bytes) -> str:
    from eth_account import Account

    signed = Account.sign_transaction(tx, key)
    return "0x" + bytes(signed.raw_transaction).hex()


def _local_tx_hash(raw: str) -> str:
    """The hash a signed transaction *will* have: keccak of its raw bytes.

    Known before the node is asked, so the ledger can carry it before the
    broadcast rather than after — the window between the two is where a
    crash or a "rejected" that was actually accepted used to lose money.
    """
    from eth_utils import keccak

    return "0x" + keccak(bytes.fromhex(raw.removeprefix("0x"))).hex()


def _client_order_id(value: str | None) -> str | None:
    """Validate a caller's idempotency key; ``None`` when none was given."""
    text = (value or "").strip()
    if not text:
        return None
    if not CLIENT_ORDER_ID_RE.match(text):
        raise TradingError(
            "trading.invalid",
            "clientOrderId must be 1-64 characters of letters, digits, '.', '_', ':' or '-'",
        )
    return text


def _client_quote_json(
    expected_out_raw: int | None, min_out_raw: int | None, quote_id: str | None
) -> str | None:
    """The quote a client confirmed, as stored in ``orders.quote_json``; ``None`` when none."""
    if expected_out_raw is None and min_out_raw is None and quote_id is None:
        return None
    for name, value in (("expectedOutRaw", expected_out_raw), ("minOutRaw", min_out_raw)):
        if value is not None and int(value) < 0:
            raise TradingError("trading.invalid", f"{name} must not be negative")
    return json.dumps(
        {
            "quoteId": quote_id,
            "expectedOutRaw": str(expected_out_raw) if expected_out_raw is not None else None,
            "minOutRaw": str(min_out_raw) if min_out_raw is not None else None,
        }
    )


def _client_expected_out(row: dict[str, Any]) -> int | None:
    """``expectedOutRaw`` from the order's client quote, or ``None`` when the client sent none."""
    raw = row.get("quote_json")
    if not raw:
        return None
    try:
        payload = json.loads(str(raw))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("expectedOutRaw")
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sign_permit(permit: dict[str, Any], key: bytes) -> str:
    from eth_account import Account

    types = {k: v for k, v in dict(permit.get("types") or {}).items() if k != "EIP712Domain"}
    signed = Account.sign_typed_data(
        key,
        domain_data=dict(permit.get("domain") or {}),
        message_types=types,
        message_data=dict(permit.get("values") or {}),
    )
    return "0x" + bytes(signed.signature).hex()


class TradingService:
    def __init__(
        self,
        config: Any,
        *,
        vault: Vault | None = None,
        ledger: Ledger | None = None,
        prices: PriceService | None = None,
        http: httpx.AsyncClient | None = None,
        evm_factory: Callable[[ChainSpec], EvmClient] | None = None,
        uniswap_factory: Callable[[str], UniswapClient] | None = None,
        aggregator_factory: Callable[[str], AggregatorClient] | None = None,
        broadcast: Broadcast | None = None,
        now: Callable[[], float] = time.time,
        sign_tx: Callable[[dict[str, Any], bytes], str] = _sign_tx,
        sign_permit: Callable[[dict[str, Any], bytes], str] = _sign_permit,
        background: bool = True,
        lp_env_factory: LpEnvFactory | None = None,
    ) -> None:
        self._gateway_config = config
        self.config = getattr(config, "trading", config)
        self.vault = vault or Vault()
        self.ledger = ledger or Ledger()
        self._http = http or httpx.AsyncClient(timeout=15.0)
        self.prices = prices or PriceService(
            http=self._http, ttl_s=float(getattr(self.config, "price_ttl_seconds", 20))
        )
        self._evm_factory = evm_factory
        self._uniswap_factory = uniswap_factory
        self._aggregator_factory = aggregator_factory
        self._aggregator: AggregatorClient | None = None
        self._aggregator_base = ""
        self._broadcast = broadcast or _default_broadcast
        self._now = now
        self._sign_tx = sign_tx
        self._sign_permit = sign_permit
        self._background = background
        self.lp_env_factory = lp_env_factory
        self._evm: dict[int, EvmClient] = {}
        self._uniswap: UniswapClient | None = None
        self._uniswap_key = ""
        self._task: asyncio.Task[None] | None = None
        self._confirm_tasks: set[asyncio.Task[Any]] = set()
        self._order_events: dict[str, asyncio.Event] = {}
        # One lock per wallet around sign-and-send: two orders for the same
        # wallet in flight at once would read the same pending nonce.
        self._wallet_locks: dict[str, asyncio.Lock] = {}
        self._settling: set[str] = set()
        self._sync_lock = asyncio.Lock()
        self.syncing = False
        self.last_sync_at: float | None = None
        self.discovery = BlockscoutDiscovery(http=self._http, now=now)
        self.curator = TokenCurator(self.ledger, self.prices, now=now)
        self._hidden_sync_at = 0.0
        self.syncer = WalletSyncer(
            self.ledger,
            self.prices,
            evm_for=self.evm,
            token_meta=self.token_meta,
            watch_tokens=self.watch_tokens,
            discover_tokens=self.discovery.holdings,
            discovery_truncated=self.discovery.truncated,
            now=now,
        )
        # When each wallet last had a chain read forced through wallet.balances,
        # so a client polling with refresh=true cannot turn into a load test.
        self._manual_sync_at: dict[str, float] = {}
        self._network_cache: dict[str, Any] | None = None
        self._network_cache_at = 0.0
        # One Approval-log scan per wallet/chain at a time; a review call
        # returns what is cached and reports that the scan is still running.
        self._allowance_scans: dict[tuple[int, str], asyncio.Task[None]] = {}

    # ── infrastructure ─────────────────────────────────────────────────

    def chains(self) -> list[ChainSpec]:
        return list(CHAINS.values())

    def evm(self, chain: ChainSpec) -> EvmClient:
        client = self._evm.get(chain.chain_id)
        if client is None:
            if self._evm_factory is not None:
                client = self._evm_factory(chain)
            else:
                url = rpc_url_for(chain, dict(getattr(self.config, "rpc_urls", {}) or {}))
                client = EvmClient(
                    url,
                    http=self._http,
                    max_log_span=chain.max_log_span,
                    max_priority_fee_wei=chain.max_priority_fee_wei,
                    max_fee_per_gas_wei=chain.max_fee_per_gas_wei,
                )
            self._evm[chain.chain_id] = client
        return client

    def api_key(self) -> str:
        resolver = getattr(self.config, "resolved_uniswap_api_key", None)
        if callable(resolver):
            return str(resolver() or "")
        return str(getattr(self.config, "uniswap_api_key", "") or "")

    def uniswap(self, api_key: str | None = None) -> UniswapClient:
        key = api_key if api_key is not None else self.api_key()
        if not key:
            raise TradingError("trading.no_api_key", "No Uniswap API key configured")
        if api_key is not None and api_key != self.api_key():
            if self._uniswap_factory is not None:
                return self._uniswap_factory(key)
            return UniswapClient(key, http=self._http)
        if self._uniswap is None or self._uniswap_key != key:
            self._uniswap = (
                self._uniswap_factory(key)
                if self._uniswap_factory is not None
                else UniswapClient(key, http=self._http)
            )
            self._uniswap_key = key
        return self._uniswap

    def aggregator(self) -> AggregatorClient:
        base = str(getattr(self.config, "aggregator_base_url", "") or AGGREGATOR_BASE).strip()
        if self._aggregator is None or self._aggregator_base != base:
            self._aggregator = (
                self._aggregator_factory(base)
                if self._aggregator_factory is not None
                else AggregatorClient(base_url=base, http=self._http)
            )
            self._aggregator_base = base
        return self._aggregator

    def provider_id(self) -> str:
        value = (
            str(getattr(self.config, "provider", DEFAULT_PROVIDER_ID) or DEFAULT_PROVIDER_ID)
            .strip()
            .lower()
        )
        return value if value in PROVIDER_IDS else DEFAULT_PROVIDER_ID

    def provider(
        self, provider_id: str | None = None, *, api_key: str | None = None
    ) -> SwapProvider:
        """The swap provider to use for this call (read from config every time)."""
        chosen = (provider_id or "").strip().lower() or self.provider_id()
        if chosen == "aggregator":
            return AggregatorProvider(self.aggregator())
        if chosen == "uniswap":
            return UniswapProvider(self.uniswap(api_key))
        raise TradingError("trading.invalid", f"unknown swap provider {chosen!r}")

    def ensure_unlocked(self) -> bool:
        if self.vault.unlocked:
            return True
        return self.vault.try_auto_unlock()

    def ensure_started(self) -> None:
        """Start the sync/expiry loop once an event loop is running."""
        if not self._background or self._task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._run_loop(), name="trading-sync")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        for task in list(self._confirm_tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._confirm_tasks.clear()
        for scan in list(self._allowance_scans.values()):
            scan.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await scan
        self._allowance_scans.clear()

    async def aclose(self) -> None:
        await self.stop()
        for client in self._evm.values():
            await client.aclose()
        if self._uniswap is not None:
            await self._uniswap.aclose()
        await self.prices.aclose()
        await self._http.aclose()
        self.ledger.close()

    async def _run_loop(self) -> None:
        interval = float(getattr(self.config, "sync_interval_seconds", 30))
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("trading.loop_error", error=str(exc))
            await asyncio.sleep(interval)

    async def tick(self) -> None:
        """One pass of housekeeping: expire approvals, settle strays, sync wallets."""
        await self.expire_orders()
        if self.vault.initialized:
            await self.recover_submitted()
            await self.sync_all()

    async def recover_submitted(self) -> None:
        """Finish ``submitted`` orders nobody is watching.

        A gateway restart drops every in-memory confirm task; a confirm task
        that raised leaves its order ``submitted`` forever. Either way the
        transaction is on-chain and the ledger owes it an answer.
        """
        watched = {t.get_name() for t in self._confirm_tasks}
        # A row that carries a tx_hash was signed and recorded before the
        # broadcast; whatever status the crash left it in, the transaction
        # may be on chain, so it is a ``submitted`` order from here on.
        for row in self.ledger.list_orders(status="quoted,approved,awaiting_approval", limit=100):
            if row.get("tx_hash"):
                self.ledger.update_order(
                    str(row["order_id"]), expect_status=str(row["status"]), status="submitted"
                )
        for row in self.ledger.list_orders(status="submitted", limit=100):
            order_id = str(row["order_id"])
            tx_hash = row.get("tx_hash")
            if not tx_hash or f"trading-confirm-{order_id}" in watched:
                continue
            try:
                self.vault.get(str(row["wallet"]))
            except Exception:
                continue
            try:
                await self._confirm(order_id, str(tx_hash), None, timeout_s=RECOVER_POLL_S)
            except Exception as exc:  # one stray must not stop the pass
                log.warning("trading.recover_failed", order=order_id, error=str(exc))

    async def _emit(self, event: str, payload: dict[str, Any]) -> None:
        try:
            await self._broadcast(event, {"schema_version": 1, **payload})
        except Exception:  # pragma: no cover
            log.debug("trading.emit_failed", event=event)

    # ── status / probe ─────────────────────────────────────────────────

    async def status(self, *, check_rpc: bool = False) -> dict[str, Any]:
        chains = []
        for chain in self.chains():
            healthy: bool | None = None
            if check_rpc:
                try:
                    healthy = (await self.evm(chain).chain_id()) == chain.chain_id
                except Exception:
                    healthy = False
            row = chain.to_dict()
            # Provider keys live in the URL path (dRPC, Alchemy, Infura): never send
            # them to a client.
            row["rpcUrl"] = redact_rpc_url(
                rpc_url_for(chain, dict(getattr(self.config, "rpc_urls", {}) or {}))
            )
            row["healthy"] = healthy
            chains.append(row)
        vault = self.vault.status()
        providers = []
        for pid in PROVIDER_IDS:
            needs_key = pid == "uniswap"
            prow: dict[str, Any] = {
                "id": pid,
                "label": provider_label(pid),
                "needsKey": needs_key,
                "keyConfigured": bool(self.api_key()) if needs_key else True,
                "healthy": None,
                "active": pid == self.provider_id(),
            }
            if check_rpc:
                try:
                    result = await self.provider(pid).probe(chain=self.chains()[0])
                except TradingError:
                    result = ProbeResult(ok=False, latency_ms=None, error="not configured")
                prow["healthy"] = result.ok
            providers.append(prow)
        return {
            "enabled": bool(getattr(self.config, "enabled", True)),
            "version": __version__,
            "provider": self.provider_id(),
            "providers": providers,
            "apiKeyConfigured": bool(self.api_key()),
            "chains": chains,
            "limits": self._limits_dict(),
            "unlockMode": vault["unlockMode"],
            "unlocked": vault["unlocked"],
            "initialized": vault["initialized"],
            "walletCount": vault["walletCount"],
            "syncing": self.syncing,
            "rebuild": self.syncer.rebuild_progress,
            "lastSyncAt": int(self.last_sync_at * 1000) if self.last_sync_at else None,
            "ledgerRepair": self._ledger_repair(),
        }

    def _ledger_repair(self) -> str | None:
        """What the ledger says it needs (``"full sync required"``), or ``None``."""
        repair = getattr(self.ledger, "repair_pending", None)
        if repair is None:
            return None
        result = repair()
        return str(result) if result else None

    def _limits_dict(self) -> dict[str, Any]:
        return {
            "approvalThresholdUsd": float(getattr(self.config, "approval_threshold_usd", 100.0)),
            "dailyCapUsd": float(getattr(self.config, "daily_cap_usd", 1000.0)),
            "approvalTtlSeconds": int(getattr(self.config, "approval_ttl_seconds", 900)),
            "agentMaxPriceImpactPct": self._agent_max_price_impact(),
            "agentMaxSlippagePct": self._agent_max_slippage(),
        }

    def _agent_max_price_impact(self) -> float:
        return float(
            getattr(
                self.config,
                "agent_max_price_impact_pct",
                guardrails.DEFAULT_AGENT_MAX_PRICE_IMPACT_PCT,
            )
        )

    def _agent_max_slippage(self) -> float:
        default = guardrails.DEFAULT_AGENT_MAX_SLIPPAGE_PCT
        return float(getattr(self.config, "agent_max_slippage_pct", default))

    def _spent_today(self, record: WalletRecord, *, exclude_order_id: str | None = None) -> float:
        """Confirmed agent spend today plus what agent orders still in flight hold."""
        return self.ledger.spent_today(record.key) + self.ledger.open_agent_value_usd(
            record.key, exclude_order_id=exclude_order_id, now=self._now()
        )

    def _verdict(
        self,
        *,
        initiator: str,
        value: float | None,
        record: WalletRecord,
        quote: ProviderQuote,
        order_id: str | None = None,
    ) -> guardrails.GuardVerdict:
        return guardrails.evaluate(
            initiator=initiator,
            value_usd=value,
            threshold_usd=self.config.approval_threshold_usd,
            daily_cap_usd=self.config.daily_cap_usd,
            spent_today_usd=self._spent_today(record, exclude_order_id=order_id),
            price_impact_pct=quote.price_impact_pct,
            max_price_impact_pct=self._agent_max_price_impact(),
        )

    def _wallet_lock(self, key: str) -> asyncio.Lock:
        lock = self._wallet_locks.get(key)
        if lock is None:
            lock = self._wallet_locks[key] = asyncio.Lock()
        return lock

    def _check_agent_slippage(self, initiator: str, slippage: float | None) -> None:
        ceiling = self._agent_max_slippage()
        if initiator == "agent" and slippage is not None and float(slippage) > ceiling:
            raise TradingError(
                "trading.slippage_too_high",
                f"agent slippage {float(slippage):.2f}% is above the {ceiling:.2f}% ceiling",
            )

    async def probe(
        self, api_key: str | None = None, *, provider_id: str | None = None
    ) -> dict[str, Any]:
        chosen = (provider_id or "").strip().lower() or self.provider_id()
        if chosen not in PROVIDER_IDS:
            raise TradingError("trading.invalid", f"unknown swap provider {chosen!r}")
        if chosen == "uniswap":
            key = (api_key or "").strip() or self.api_key()
            if not key:
                return {
                    "provider": chosen,
                    "ok": False,
                    "latencyMs": None,
                    "error": "No Uniswap API key configured",
                }
            result = await self.provider("uniswap", api_key=key).probe(chain=self.chains()[0])
        else:
            result = await self.provider(chosen).probe(chain=self.chains()[0])
        return {"provider": chosen, **result.to_dict()}

    # ── tokens ─────────────────────────────────────────────────────────

    async def token_meta(self, chain: ChainSpec, address: str) -> TokenMeta:
        """Metadata for a token, remembering it — and judging it, if not judged yet.

        Every token the engine meets passes through here (sweep, balances,
        quotes), so this is where a junk airdrop gets hidden: see ``spam.py``.
        """
        meta = await self._token_meta(chain, address)
        if not meta.native:
            await self.curator.review(chain, meta.address)
        return meta

    async def _token_meta(self, chain: ChainSpec, address: str) -> TokenMeta:
        if is_native(address):
            meta = native_token(chain)
            self._remember_token(meta)
            return meta
        key = normalize_address(address)
        row = self.ledger.get_token(chain.chain_id, key)
        if row and (row["symbol"] or row["verified"]):
            return TokenMeta(
                chain_id=chain.chain_id,
                address=key,
                symbol=str(row["symbol"]),
                name=str(row["name"]),
                decimals=int(row["decimals"]),
                logo_url=row["logo_url"],
                verified=bool(row["verified"]),
                stock_token=str(row["name"]).endswith("• Robinhood Token"),
            )
        known = await self.prices.known_token(chain, key)
        if known is not None:
            self._remember_token(known)
            return known
        symbol, name, decimals = "", "", 18
        try:
            symbol, name, decimals = await self.evm(chain).erc20_metadata(key)
        except (EvmRpcError, EvmTransportError):
            pass
        if not symbol:
            # Nobody names it. Either it is a contract that does not implement
            # symbol(), or there is nothing here — most often an address pasted
            # from another chain. Only the second is an error, and it has to be
            # one: returning a token with a *guessed* 18 decimals would let a
            # swap be built against something that does not exist.
            try:
                code = await self.evm(chain).get_code(key)
            except (EvmRpcError, EvmTransportError):
                # A node we cannot reach is not evidence of absence.
                code = ""
            if code in ("0x", "0x0"):
                raise TradingError(
                    "trading.unknown_token",
                    f"No contract at {key} on {chain.name}",
                    details={"address": key, "chainId": chain.chain_id},
                )
        meta = TokenMeta(chain.chain_id, key, symbol, name, decimals)
        self._remember_token(meta)
        return meta

    def _remember_token(self, meta: TokenMeta) -> None:
        self.ledger.upsert_token(
            meta.chain_id,
            meta.address,
            symbol=meta.symbol,
            name=meta.name,
            decimals=meta.decimals,
            logo_url=meta.logo_url,
            is_native=meta.native,
            verified=meta.verified,
        )

    async def resolve_token(self, chain: ChainSpec, value: str) -> TokenMeta:
        """An address, ``ETH``, or a symbol (unique, verified) → metadata."""
        text = (value or "").strip()
        if not text:
            raise TradingError("trading.invalid", "token is required")
        if is_native(text) or text.lower() == chain.native_symbol.lower():
            return await self.token_meta(chain, NATIVE_ADDRESS)
        if text.startswith("0x"):
            return self._touch(await self.token_meta(chain, text))
        hits = await self.prices.find_by_symbol(chain, text)
        if not hits:
            raise TradingError("trading.invalid", f"Unknown token symbol {text!r} on {chain.name}")
        stock = [h for h in hits if h.stock_token]
        chosen = stock[0] if len(stock) == 1 else (hits[0] if len(hits) == 1 else None)
        if chosen is None:
            raise TradingError(
                "trading.invalid",
                f"Symbol {text!r} is ambiguous on {chain.name}; pass the address",
                details={"candidates": [h.to_dict() for h in hits[:5]]},
            )
        return self._touch(await self.token_meta(chain, chosen.address))

    def _touch(self, meta: TokenMeta) -> TokenMeta:
        """Someone named this token on purpose: it is shown, and stays shown."""
        if not meta.native:
            self.ledger.touch_token(meta.chain_id, meta.address)
        return meta

    async def set_token_hidden(
        self, chain: ChainSpec, address: str, hidden: bool
    ) -> dict[str, Any]:
        """The user's own hide/show. Final: the classifier never reverses it."""
        meta = await self._token_meta(chain, address)
        if meta.native:
            raise TradingError("trading.invalid", f"{chain.native_symbol} cannot be hidden")
        self.ledger.set_token_hidden(
            chain.chain_id, meta.address, hidden, by="user", classified_at=self._now()
        )
        await self._emit("trading.changed", {"reason": "token", "chainId": chain.chain_id})
        return self._token_row(chain.chain_id, meta.address)

    def _token_row(self, chain_id: int, address: str) -> dict[str, Any]:
        """A token dict with its visibility, for the hide/show RPC."""
        info = self._token_dict(chain_id, address) or {}
        row = self.ledger.get_token(chain_id, address)
        return {
            **info,
            "hidden": bool(row and row["hidden"]),
            "hiddenBy": row.get("hidden_by") if row else None,
        }

    async def search_tokens(self, chain: ChainSpec, query: str) -> list[dict[str, Any]]:
        rows = await self._fill_from_chain(chain, await self.prices.search(chain, query))
        for row in rows:
            self.ledger.upsert_token(
                chain.chain_id,
                str(row["address"]),
                symbol=str(row.get("symbol") or ""),
                name=str(row.get("name") or ""),
                decimals=int(row.get("decimals") or 18),
                logo_url=row.get("logoUrl"),
                is_native=bool(row.get("native")),
                verified=bool(row.get("verified")),
            )
        return rows

    async def _fill_from_chain(
        self, chain: ChainSpec, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Ask the chain about what the indexes did not know, and drop what is
        not there.

        A bare address that no list carries and no pool quotes used to come back
        as a row with an empty symbol, an empty name and a *guessed* 18 decimals.
        Two different things hid behind that blank row, and they deserve
        opposite answers:

        * A real token nobody has indexed yet. The contract knows its symbol,
          name and decimals, so read them — a guessed 18 would have made any
          trade in it wrong by orders of magnitude.
        * Nothing at all: an address from another chain, or a typo. There is no
          code at it here, so it is not offered. An empty result is the honest
          answer; a row you cannot trade is not.

        Only rows the indexes left blank pay for the calls, and a node that will
        not answer leaves the row exactly as it was rather than discarding it.
        """
        out: list[dict[str, Any]] = []
        for row in rows:
            if row.get("native"):
                out.append(row)
                continue
            address = str(row["address"])
            has_symbol = bool(str(row.get("symbol") or "").strip())
            # A verified list carries real decimals. A search hit outside it
            # (DexScreener's pair search) guessed 18, which — silently — makes
            # every USD figure for a 6-decimal token a trillion times off, and
            # the daily cap with it. Read the contract once; the ledger keeps
            # the answer so later hits skip the call.
            known = self.ledger.get_token(chain.chain_id, normalize_address(address))
            if has_symbol and (row.get("verified") or (known and known["symbol"])):
                if known and not row.get("verified"):
                    row["decimals"] = int(known["decimals"])
                out.append(row)
                continue
            evm = self.evm(chain)
            try:
                symbol, name, decimals = await evm.erc20_metadata(address)
            except Exception:  # a search must not fail because a node did
                if has_symbol:
                    out.append(row)
                continue
            if symbol:
                row["symbol"] = symbol
            if name:
                row["name"] = name
            row["decimals"] = decimals
            if symbol:
                out.append(row)
                continue
            try:
                code = await evm.get_code(address)
            except Exception:
                out.append(row)
                continue
            # A contract that simply does not implement symbol() is still real.
            if code and code not in ("0x", "0x0"):
                out.append(row)
        return out

    def _token_dict(self, chain_id: int, address: str | None) -> dict[str, Any] | None:
        if address is None:
            return None
        chain = CHAINS.get(chain_id)
        if is_native(address) and chain is not None:
            return native_token(chain).to_dict()
        row = self.ledger.get_token(chain_id, address)
        if row is None:
            return TokenMeta(chain_id, address.lower(), "", "", 18).to_dict()
        return TokenMeta(
            chain_id=chain_id,
            address=str(row["address"]),
            symbol=str(row["symbol"]),
            name=str(row["name"]),
            decimals=int(row["decimals"]),
            logo_url=row["logo_url"],
            native=bool(row["is_native"]),
            verified=bool(row["verified"]),
            stock_token=str(row["name"]).endswith("• Robinhood Token"),
        ).to_dict()

    def _decimals(self, chain_id: int, address: str) -> int:
        if is_native(address):
            return 18
        row = self.ledger.get_token(chain_id, address)
        return int(row["decimals"]) if row else 18

    async def watch_tokens(self, chain: ChainSpec) -> list[str]:
        """Tokens every wallet is scanned for: the chain's USDC/WETH plus any token
        the ledger has met (searched, traded, or transferred)."""
        tokens: set[str] = set()
        weth = await self.weth_for(chain)
        if weth:
            tokens.add(weth)
        usdc = await self.usdc_for(chain)
        if usdc:
            tokens.add(usdc)
        for row in self.ledger.tokens(chain.chain_id):
            if not row["is_native"]:
                tokens.add(str(row["address"]))
        return sorted(tokens)

    async def usdc_for(self, chain: ChainSpec) -> str | None:
        if chain.usdc:
            return chain.usdc.lower()
        hits = await self.prices.find_by_symbol(chain, "USDC")
        return hits[0].address if hits else None

    async def weth_for(self, chain: ChainSpec) -> str | None:
        """The wrapped native token on ``chain`` (lowercase), if known."""
        if chain.weth:
            return chain.weth.lower()
        hits = await self.prices.find_by_symbol(chain, "WETH")
        return hits[0].address if hits else None

    async def unwrap(
        self, chain: ChainSpec, wallet: str | None, amount: str | None = None
    ) -> dict[str, Any]:
        """Send ``WETH.withdraw(amount)`` from ``wallet``; whole balance when omitted.

        An operator's action (the RPC gates it); it signs under the wallet's
        lock like every other send, so it never races an order for the nonce.
        """
        if not getattr(self.config, "enabled", True):
            raise TradingError("trading.disabled", "Trading is disabled in config")
        if not self.ensure_unlocked():
            raise TradingError("wallet.locked", "Wallet vault is locked")
        record = self.vault.resolve(wallet)
        weth = await self.weth_for(chain)
        if weth is None:
            raise TradingError("trading.invalid", f"WETH is not known on {chain.name}")
        meta = await self.token_meta(chain, weth)
        evm = self.evm(chain)
        async with self._wallet_lock(record.key):
            balance = await evm.erc20_balance_of(weth, record.address)
            raw = to_raw(amount, meta.decimals) if amount else balance
            if raw <= 0:
                raise TradingError("trading.invalid", "nothing to unwrap")
            if raw > balance:
                raise TradingError(
                    "trading.insufficient_balance",
                    f"{record.label} holds {format_amount(balance, meta.decimals)} WETH",
                )
            key = self.vault.private_key(record.address)
            tx = {
                "to": checksum_address(weth),
                "from": record.address,
                "data": SEL_WETH_WITHDRAW + pad_uint(raw),
                "value": "0",
                "chainId": chain.chain_id,
            }
            pre_native = await evm.get_balance(record.address)
            tx_hash = await self._send(chain, record, key, tx)
        receipt = await evm.wait_for_sealed_receipt(tx_hash, timeout_s=RECEIPT_TIMEOUT_S)
        if receipt is None:
            raise TradingError(
                "trading.tx_pending",
                f"unwrap {tx_hash} was not mined within the wait window; "
                "the balance will reconcile on the next sync",
            )
        if not receipt_succeeded(receipt):
            raise TradingError("trading.tx_failed", f"unwrap transaction failed ({tx_hash})")
        gas_wei = receipt_gas_wei(receipt)
        eth_price = await self.prices.price(chain, NATIVE_ADDRESS)
        gas_usd = float(to_human(gas_wei, 18)) * eth_price if eth_price is not None else None
        entry_id = self.ledger.insert_entry(
            ts=self._now(),
            chain_id=chain.chain_id,
            wallet=record.key,
            kind="unwrap",
            tx_hash=tx_hash,
            log_index=0,
            token_in=weth,
            amount_in_raw=raw,
            token_out=NATIVE_ADDRESS,
            amount_out_raw=raw,
            gas_usd=gas_usd,
            initiator="manual",
        )
        # Move the lots across 1:1 so cost basis follows the ETH.
        lots = self.ledger.open_lots(chain.chain_id, record.key, weth)
        moved = 0
        for lot in lots:
            if moved >= raw:
                break
            take = min(lot.amount_raw, raw - moved)
            self.ledger.add_lot(
                chain.chain_id,
                record.key,
                NATIVE_ADDRESS,
                amount_raw=take,
                cost_usd_per_raw=lot.cost_usd_per_raw,
                acquired_at=lot.acquired_at,
                entry_id=entry_id,
            )
            lot.amount_raw -= take
            moved += take
        self.ledger.save_lots(lots)
        self.ledger.set_balance(chain.chain_id, record.key, weth, balance - raw)
        # Pinned to the receipt's block and checked against the pre-send
        # snapshot: an unpinned "latest" read behind a load balancer can
        # still be the balance from before the unwrap.
        self.ledger.set_balance(
            chain.chain_id,
            record.key,
            NATIVE_ADDRESS,
            await self._native_after(evm, record.address, receipt, pre=pre_native or None),
        )
        await self._emit("trading.changed", {"reason": "sync", "wallet": record.address})
        return {
            "txHash": tx_hash,
            "explorerUrl": chain.tx_url(tx_hash),
            "amount": format_amount(raw, 18),
        }

    # ── wallets ────────────────────────────────────────────────────────

    def wallet_dicts(self) -> list[dict[str, Any]]:
        if not self.vault.initialized:
            return []
        primary = self.vault.primary_address()
        rows = []
        for record in self.vault.list():
            row = record.to_dict(primary=(record.address == primary))
            row["chains"] = [c.chain_id for c in self.chains()]
            rows.append(row)
        return rows

    def _mirror_wallet(self, record: WalletRecord) -> None:
        primary = self.vault.primary_address()
        self.ledger.upsert_wallet(
            record.key,
            label=record.label,
            is_primary=(record.address == primary),
            created_at=record.created_at,
            created_block=record.created_block,
        )

    async def create_wallet(self, label: str) -> dict[str, Any]:
        self.ensure_unlocked()
        record = self.vault.create(label)
        await self._stamp_created_block(record)
        self._mirror_wallet(record)
        await self._emit("trading.changed", {"reason": "wallet", "wallet": record.address})
        self._sync_new_wallet(record)
        return record.to_dict(primary=(self.vault.primary_address() == record.address))

    def _sync_new_wallet(self, record: WalletRecord) -> None:
        """A wallet that just appeared should not wait a whole tick to show a balance."""
        if not self._background:
            return
        try:
            self.request_sync(wallet=record.address)
        except RuntimeError:  # no running loop (tests, CLI helpers)
            pass

    async def _stamp_created_block(self, record: WalletRecord) -> None:
        for chain in self.chains():
            try:
                block = await self.evm(chain).block_number()
            except Exception:
                continue
            self.vault.set_created_block(record.address, chain.chain_id, block)
            record.created_block[str(chain.chain_id)] = block

    async def import_wallet(
        self,
        label: str,
        *,
        private_key: str | None = None,
        keystore_json: str | None = None,
        keystore_password: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_unlocked()
        if private_key:
            record = self.vault.import_private_key(label, private_key)
        elif keystore_json:
            record = self.vault.import_keystore(label, keystore_json, keystore_password or "")
        else:
            raise TradingError("trading.invalid", "privateKey or keystoreJson is required")
        self._mirror_wallet(record)
        await self._emit("trading.changed", {"reason": "wallet", "wallet": record.address})
        self._sync_new_wallet(record)
        return record.to_dict(primary=(self.vault.primary_address() == record.address))

    async def remove_wallet(self, address: str, password: str) -> None:
        record = self.vault.get(address)
        self.vault.remove(record.address, password)
        self.ledger.remove_wallet(record.key)
        await self._emit("trading.changed", {"reason": "wallet", "wallet": record.address})

    def _wallets_for(self, selector: Any) -> list[WalletRecord]:
        if selector in (None, "", "primary"):
            return [self.vault.resolve(None)]
        if selector == "all":
            return self.vault.list()
        if isinstance(selector, str):
            return [self.vault.resolve(selector)]
        if isinstance(selector, list):
            out: list[WalletRecord] = []
            seen: set[str] = set()
            for item in selector:
                record = self.vault.resolve(str(item))
                if record.key not in seen:
                    seen.add(record.key)
                    out.append(record)
            if not out:
                raise WalletNotFoundError("no wallets selected")
            return out
        raise TradingError("trading.invalid", "wallets must be an address list, 'all' or omitted")

    # ── balances / portfolio ───────────────────────────────────────────

    async def balances(
        self,
        wallet: str | None = None,
        chain_id: int | None = None,
        *,
        refresh: bool = False,
        include_hidden: bool = False,
    ) -> list[dict[str, Any]]:
        """Token balances from the ledger, priced now.

        The ledger is the only thing this reads: the chain is read by the sync
        loop, after every swap, and on ``refresh=True`` — which runs the same
        sync (logs, balances, openings) rather than a side path, and at most
        once per wallet per ``MANUAL_SYNC_MIN_S`` so a polling client cannot
        turn into a load test. A client that needs to know how fresh a chain's
        rows are reads :meth:`chain_reads`.

        Hidden (junk) tokens are left out unless ``include_hidden``; then
        they come back flagged ``hidden`` and are priced like the rest. Use
        :meth:`hidden_balance_count` for the number left out.
        """
        records = self.vault.list() if wallet is None else [self.vault.resolve(wallet)]
        chains = [c for c in self.chains() if chain_id is None or c.chain_id == chain_id]
        if refresh:
            await self._refresh_wallets(records, chains)
        rows: list[dict[str, Any]] = []
        for chain in chains:
            wanted: dict[str, dict[str, Any]] = {}
            for record in records:
                for row in self.ledger.balances(record.key, chain.chain_id):
                    raw = int(row["raw"])
                    if raw <= 0 and row["token"] != NATIVE_ADDRESS:
                        continue
                    key = f"{row['wallet']}:{row['token']}"
                    wanted[key] = {
                        "wallet": checksum_address(row["wallet"]),
                        "token": row["token"],
                        "raw": raw,
                        "updatedAt": int(float(row["updated_at"]) * 1000),
                    }
            if not wanted:
                continue
            # Judge before filtering: a first sight of a token is judged here.
            for address in sorted({w["token"] for w in wanted.values()}):
                await self.token_meta(chain, address)
            hidden = self.ledger.hidden_tokens(chain.chain_id)
            if not include_hidden:
                wanted = {k: w for k, w in wanted.items() if w["token"] not in hidden}
                if not wanted:
                    continue
            addresses = sorted({w["token"] for w in wanted.values()})
            prices = await self.prices.prices(chain, addresses)
            for item in wanted.values():
                token_info = self._token_dict(chain.chain_id, item["token"]) or {}
                decimals = int(token_info.get("decimals", 18))
                price_info = self._sellable_price(token_info, prices.get(item["token"]))
                amount = float(to_human(item["raw"], decimals))
                price = price_info.price_usd if price_info else None
                rows.append(
                    {
                        "chainId": chain.chain_id,
                        "wallet": item["wallet"],
                        "token": token_info,
                        "raw": str(item["raw"]),
                        "amount": format_amount(item["raw"], decimals),
                        "priceUsd": price,
                        "valueUsd": amount * price if price is not None else None,
                        "change24hPct": price_info.change_24h_pct if price_info else None,
                        "updatedAt": item["updatedAt"],
                        "hidden": item["token"] in hidden,
                    }
                )
        rows.sort(key=lambda r: -(r["valueUsd"] or 0.0))
        return rows

    def hidden_balance_count(self, wallet: str | None = None, chain_id: int | None = None) -> int:
        """How many held (non-zero) tokens are hidden for these wallets/chains."""
        records = self.vault.list() if wallet is None else [self.vault.resolve(wallet)]
        chains = [c for c in self.chains() if chain_id is None or c.chain_id == chain_id]
        count = 0
        for chain in chains:
            hidden = self.ledger.hidden_tokens(chain.chain_id)
            if not hidden:
                continue
            for record in records:
                for row in self.ledger.balances(record.key, chain.chain_id):
                    if row["token"] in hidden and int(row["raw"]) > 0:
                        count += 1
        return count

    async def _refresh_wallets(self, records: list[WalletRecord], chains: list[ChainSpec]) -> bool:
        """Force a chain read for these wallets, throttled per wallet. True if one ran."""
        now = self._now()
        due = [
            r for r in records if now - self._manual_sync_at.get(r.key, 0.0) >= MANUAL_SYNC_MIN_S
        ]
        if not due:
            return False
        for record in due:
            self._manual_sync_at[record.key] = now
        await self.sync_all(wallets=due, chains=chains)
        return True

    def chain_reads(
        self, wallet: str | None = None, chain_id: int | None = None
    ) -> list[dict[str, Any]]:
        """How the last chain read of each wallet/chain went (see ``chain_reads``)."""
        records = self.vault.list() if wallet is None else [self.vault.resolve(wallet)]
        out: list[dict[str, Any]] = []
        for record in records:
            for row in self.ledger.chain_reads(record.key, chain_id):
                out.append(
                    {
                        "chainId": int(row["chain_id"]),
                        "wallet": checksum_address(str(row["wallet"])),
                        "status": str(row["status"]),
                        "reason": row.get("reason"),
                        "readAt": int(float(row["read_at"]) * 1000),
                    }
                )
        return out

    async def portfolio(
        self, wallet: str | None = None, *, include_hidden: bool = False
    ) -> dict[str, Any]:
        """Holdings with cost basis and PnL.

        Hidden (junk) tokens never count towards the totals. They are left out
        of ``holdings`` too unless ``include_hidden``, in which case they come
        back flagged ``hidden`` with a value; ``hiddenCount`` is how many were
        held either way.
        """
        records = self.vault.list() if wallet is None else [self.vault.resolve(wallet)]
        keys = {r.key for r in records}
        positions = [p for p in self.ledger.positions(None if wallet is None else records[0].key)]
        realized = self.ledger.realized_by_position(None if wallet is None else records[0].key)
        # Balance rows are the on-chain truth for the amount; lots supply cost.
        holdings_keys: set[tuple[int, str, str]] = set()
        for pos in positions:
            if pos.wallet in keys:
                holdings_keys.add((pos.chain_id, pos.wallet, pos.token))
        for row in self.ledger.balances(None if wallet is None else records[0].key):
            if row["wallet"] in keys and int(row["raw"]) > 0:
                holdings_keys.add((int(row["chain_id"]), str(row["wallet"]), str(row["token"])))
        by_chain: dict[int, set[str]] = {}
        for chain_id, _wallet, token in holdings_keys:
            by_chain.setdefault(chain_id, set()).add(token)
        price_map: dict[tuple[int, str], PriceInfo] = {}
        hidden_by_chain: dict[int, set[str]] = {}
        for chain_id, tokens in by_chain.items():
            chain = CHAINS[chain_id]
            for token in tokens:
                await self.token_meta(chain, token)
            hidden_by_chain[chain_id] = self.ledger.hidden_tokens(chain_id)
            priced = (
                sorted(tokens) if include_hidden else sorted(tokens - hidden_by_chain[chain_id])
            )
            for token, info in (await self.prices.prices(chain, priced)).items():
                price_map[(chain_id, token)] = info
        pos_by_key = {(p.chain_id, p.wallet, p.token): p for p in positions}
        holdings: list[dict[str, Any]] = []
        hidden_count = 0
        unpriced_count = 0
        per_wallet: dict[str, dict[str, float]] = {
            r.key: {"value": 0.0, "cost": 0.0, "realized": 0.0, "change": 0.0, "unpriced": 0}
            for r in records
        }
        counted: set[tuple[int, str, str]] = set()
        for chain_id, wallet_key, token in sorted(holdings_keys):
            is_hidden = token in hidden_by_chain.get(chain_id, set())
            if is_hidden:
                hidden_count += 1
                if not include_hidden:
                    continue
            decimals = self._decimals(chain_id, token)
            balance_raw = self.ledger.get_balance(chain_id, wallet_key, token)
            position = pos_by_key.get((chain_id, wallet_key, token))
            amount_raw = (
                balance_raw if balance_raw is not None else (position.amount_raw if position else 0)
            )
            if amount_raw <= 0 and (position is None or position.amount_raw <= 0):
                continue
            cost = position.cost_usd if position else 0.0
            if position and position.amount_raw > 0 and amount_raw < position.amount_raw:
                cost = cost * (amount_raw / position.amount_raw)
            token_info = self._token_dict(chain_id, token) or {}
            quoted = price_map.get((chain_id, token))
            price_info = self._sellable_price(token_info, quoted)
            # Priced by the source but not sellable: its cost basis is a
            # fiction too (the opening lot was booked at that same price),
            # so the totals count neither — not a value, not a loss.
            unsellable = quoted is not None and price_info is None
            price = price_info.price_usd if price_info else None
            pnl = holding_from_lots(
                [],
                decimals=decimals,
                price_usd=price,
                realized_usd=realized.get((chain_id, wallet_key, token), 0.0),
            )
            pnl.amount_raw = amount_raw
            pnl.cost_usd = cost
            value = pnl.value_usd
            change_pct = price_info.change_24h_pct if price_info else None
            change_usd = (
                value - value / (1 + change_pct / 100.0)
                if value is not None and change_pct is not None and change_pct > -100
                else None
            )
            holdings.append(
                {
                    "chainId": chain_id,
                    "wallet": checksum_address(wallet_key),
                    "token": token_info or None,
                    "amount": format_amount(amount_raw, decimals),
                    "raw": str(amount_raw),
                    "priceUsd": price,
                    "valueUsd": value,
                    "costUsd": cost if cost > 0 else None,
                    "avgCostUsd": pnl.avg_cost_usd,
                    "unrealizedUsd": pnl.unrealized_usd,
                    "unrealizedPct": pnl.unrealized_pct,
                    "realizedUsd": pnl.realized_usd,
                    "change24hPct": change_pct,
                    "change24hUsd": change_usd,
                    "allocationPct": 0.0,
                    "hidden": is_hidden,
                }
            )
            if is_hidden:
                # Shown on request, never counted: junk has no place in a total.
                continue
            bucket = per_wallet.setdefault(
                wallet_key,
                {"value": 0.0, "cost": 0.0, "realized": 0.0, "change": 0.0, "unpriced": 0},
            )
            counted.add((chain_id, wallet_key, token))
            if value is None:
                # No price, so no value: its cost must stay out of the totals
                # too, or the hero shows a loss the market never priced.
                unpriced_count += 1
                bucket["unpriced"] += 1
            else:
                bucket["value"] += value
                if not unsellable:
                    bucket["cost"] += cost
            bucket["realized"] += pnl.realized_usd
            bucket["change"] += change_usd or 0.0
        # A position sold down to nothing is no longer a holding, but what
        # it made or lost is still this wallet's realised PnL.
        for (chain_id, wallet_key, token), realized_usd in realized.items():
            if (chain_id, wallet_key, token) in counted or wallet_key not in keys:
                continue
            if chain_id not in hidden_by_chain:
                hidden_by_chain[chain_id] = self.ledger.hidden_tokens(chain_id)
            if token in hidden_by_chain[chain_id]:
                continue
            per_wallet[wallet_key]["realized"] += realized_usd
        total_value = sum(h["valueUsd"] or 0.0 for h in holdings if not h["hidden"])
        for h in holdings:
            h["allocationPct"] = (
                (h["valueUsd"] or 0.0) / total_value * 100.0
                if total_value > 0 and not h["hidden"]
                else 0.0
            )
        holdings.sort(key=lambda h: -(h["valueUsd"] or 0.0))
        gas_total = self.ledger.gas_total(None if wallet is None else records[0].key)
        wallets_out = []
        primary = self.vault.primary_address()
        for record in records:
            bucket = per_wallet[record.key]
            wallets_out.append(
                {
                    "wallet": {
                        **record.to_dict(primary=(record.address == primary)),
                        "chains": [c.chain_id for c in self.chains()],
                    },
                    "totals": self._totals(
                        bucket["value"],
                        bucket["cost"],
                        bucket["realized"],
                        self.ledger.gas_total(record.key),
                        bucket["change"],
                    ),
                    "unpricedCount": int(bucket["unpriced"]),
                }
            )
        totals = self._totals(
            total_value,
            sum(b["cost"] for b in per_wallet.values()),
            sum(b["realized"] for b in per_wallet.values()),
            gas_total,
            sum(b["change"] for b in per_wallet.values()),
        )
        return {
            "totals": totals,
            "holdings": holdings,
            "hiddenCount": hidden_count,
            "unpricedCount": unpriced_count,
            "wallets": wallets_out,
            "updatedAt": int(self._now() * 1000),
            "syncing": self.syncing,
            "rebuild": self.syncer.rebuild_progress,
        }

    @staticmethod
    def _sellable_price(token_info: dict[str, Any], price: PriceInfo | None) -> PriceInfo | None:
        """The price a holding may be valued at — or ``None`` when it could not be sold.

        A price source reports whatever a pool last traded at, even a pool
        with five cents in it. For a token nobody lists (``verified`` is
        false) that number is not a value: an airdropped lookalike with
        ``liquidityUsd`` of 0.05 was making a fifty-cent wallet show +$12 of
        unrealised gain. Below :data:`MIN_LIQUIDITY_USD` the holding is shown
        unpriced, which is what it is worth. The gas coin and every listed
        token keep their price whatever the pool says — a listed token's
        best pool is rarely the one DexScreener happens to answer with.
        """
        if price is None:
            return None
        if token_info.get("native") or token_info.get("verified"):
            return price
        liquidity = price.liquidity_usd
        if liquidity is not None and liquidity < MIN_LIQUIDITY_USD:
            return None
        return price

    @staticmethod
    def _totals(
        value: float, cost: float, realized: float, gas: float, change: float
    ) -> dict[str, Any]:
        previous = value - change
        return {
            "valueUsd": value,
            "costUsd": cost,
            # An airdrop has a cost of 0 and a value: that is a gain, not nothing.
            "unrealizedUsd": value - cost,
            "realizedUsd": realized,
            "gasUsd": gas,
            "change24hUsd": change,
            "change24hPct": (change / previous * 100.0) if previous > 0 else None,
        }

    # ── history / chart / limits ───────────────────────────────────────

    def _entry_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        chain_id = int(row["chain_id"])
        chain = CHAINS.get(chain_id)
        tx_hash = row.get("tx_hash")
        token_in = self._token_dict(chain_id, row.get("token_in"))
        token_out = self._token_dict(chain_id, row.get("token_out"))
        amount_in = row.get("amount_in_raw")
        amount_out = row.get("amount_out_raw")
        return {
            "id": int(row["id"]),
            "ts": int(float(row["ts"]) * 1000),
            "chainId": chain_id,
            "wallet": checksum_address(str(row["wallet"])),
            "kind": row["kind"],
            "txHash": tx_hash,
            "explorerUrl": chain.tx_url(tx_hash) if chain and tx_hash else None,
            "tokenIn": token_in,
            "amountIn": (
                format_amount(int(amount_in), int(token_in["decimals"]))
                if amount_in is not None and token_in
                else None
            ),
            "tokenOut": token_out,
            "amountOut": (
                format_amount(int(amount_out), int(token_out["decimals"]))
                if amount_out is not None and token_out
                else None
            ),
            "valueUsd": row.get("value_usd"),
            "gasUsd": row.get("gas_usd"),
            "priceInUsd": row.get("price_in_usd"),
            "priceOutUsd": row.get("price_out_usd"),
            "costBasisSource": row.get("cost_basis_source"),
            "initiator": row.get("initiator") or "external",
            "orderId": row.get("order_id"),
            "sessionKey": row.get("session_key"),
            "note": row.get("note"),
        }

    def history(
        self,
        *,
        wallet: str | None = None,
        chain_id: int | None = None,
        kind: str | None = None,
        limit: int = 100,
        before: float | None = None,
        include_hidden: bool = False,
    ) -> dict[str, Any]:
        """The ledger, newest first. Entries that only moved junk are left out by default."""
        key = self.vault.resolve(wallet).key if wallet else None
        rows = self.ledger.list_entries(
            wallet=key, chain_id=chain_id, kind=kind, limit=limit, before=before
        )
        next_before = float(rows[-1]["ts"]) if len(rows) >= max(1, min(limit, 1000)) else None
        if not include_hidden:
            hidden = self.ledger.hidden_tokens()
            rows = [r for r in rows if not self._entry_is_junk(r, hidden)]
        entries = [self._entry_dict(r) for r in rows]
        return {"entries": entries, "nextBefore": next_before}

    @staticmethod
    def _entry_is_junk(row: dict[str, Any], hidden: set[str]) -> bool:
        """An entry is junk when every ERC-20 it touched is hidden."""
        tokens = [
            str(t).lower()
            for t in (row.get("token_in"), row.get("token_out"))
            if t and not is_native(str(t))
        ]
        return bool(tokens) and all(t in hidden for t in tokens)

    async def chart(self, chain: ChainSpec, token: str, range_key: str) -> dict[str, Any]:
        """One close-price line for a token, drawn from the cheapest source.

        The line only needs a close per point, so candles are reduced on the
        way out. Short windows read the local snapshot table first and cost no
        request at all; they fall back to GeckoTerminal when the table is too
        young to cover them (a fresh install, or a token only just acquired).
        """
        spec = CHART_RANGES.get(range_key) or CHART_RANGES[DEFAULT_CHART_RANGE]
        address = NATIVE_ADDRESS if is_native(token) else normalize_address(token)
        since = self._now() - spec.seconds

        local: list[dict[str, float]] = [
            {"t": float(s["ts"]), "c": float(s["price_usd"])}
            for s in self.ledger.snapshots(chain.chain_id, address, since)
        ]
        source = ""
        points: list[dict[str, float]] = []
        # Two points is a segment, not a chart: anything thinner is treated as
        # no local history rather than drawn as a stub.
        if spec.local_first and len(local) > 2:
            source, points = "snapshots", local
        else:
            candles = await self.prices.ohlcv(
                chain, address, timeframe=spec.timeframe, limit=spec.limit
            )
            if candles:
                source = "geckoterminal"
                points = [{"t": float(c["t"]), "c": float(c["c"])} for c in candles]
            elif local:
                source, points = "snapshots", local

        stats = await self._chart_stats(chain, address, points)
        return {
            "source": source or "snapshots",
            "range": range_key,
            "points": _thin(points, spec.max_points),
            "stats": stats,
        }

    async def _chart_stats(
        self, chain: ChainSpec, address: str, points: list[dict[str, float]]
    ) -> dict[str, Any]:
        """The figures above the line. Free: the spot fetch behind them is the
        same cached DexScreener response the chart's pair lookup already used.
        """
        lookup = NATIVE_ADDRESS if is_native(address) else normalize_address(address)
        info = (await self.prices.prices(chain, [lookup])).get(lookup)
        first = points[0]["c"] if points else None
        last = points[-1]["c"] if points else None
        change = (last - first) / first * 100 if first and last else None
        return {
            "priceUsd": info.price_usd if info else None,
            "priceNative": info.price_native if info else None,
            "quoteSymbol": (info.price_native_symbol if info else None) or chain.native_symbol,
            "marketCapUsd": info.market_cap_usd if info else None,
            "market": info.market if info else None,
            "changePct": change,
        }

    def limits(self, wallet: str | None) -> dict[str, Any]:
        record = self.vault.resolve(wallet)
        return {
            **self._limits_dict(),
            "wallet": record.address,
            # What the guard will see: confirmed spend plus orders in flight.
            "spentTodayUsd": round(self._spent_today(record), 2),
            "day": local_day(self._now()),
        }

    def set_lot_cost(self, entry_id: int, cost_usd_per_token: float) -> dict[str, Any]:
        entry = self.ledger.get_entry(entry_id)
        if entry is None:
            raise TradingError("trading.invalid", f"no history entry {entry_id}")
        lots = self.ledger.lots_for_entry(entry_id)
        if not lots:
            raise TradingError("trading.invalid", "entry has no lot to reprice")
        token = str(lots[0]["token"])
        decimals = self._decimals(int(entry["chain_id"]), token)
        for lot in lots:
            self.ledger.update_lot_cost(int(lot["id"]), per_raw(cost_usd_per_token, decimals))
        amount_raw = entry.get("amount_out_raw")
        value = (
            float(to_human(int(amount_raw), decimals)) * float(cost_usd_per_token)
            if amount_raw is not None
            else entry.get("value_usd")
        )
        self.ledger.update_entry(
            entry_id,
            value_usd=value,
            price_out_usd=float(cost_usd_per_token),
            cost_basis_source="manual",
        )
        refreshed = self.ledger.get_entry(entry_id)
        assert refreshed is not None
        return self._entry_dict(refreshed)

    # ── sync ───────────────────────────────────────────────────────────

    async def sync_all(
        self,
        *,
        wallet: str | None = None,
        full: bool = False,
        wallets: list[WalletRecord] | None = None,
        chains: list[ChainSpec] | None = None,
    ) -> int:
        """Sync every wallet × chain (or the given subset). 0 when a sync is already running.

        Returns how many wallet/chain pairs changed; each one has already
        been announced as ``trading.changed`` so clients re-read the ledger
        the moment it moves, not on their next poll.
        """
        if self._sync_lock.locked():
            return 0
        async with self._sync_lock:
            self.syncing = True
            changed = 0
            try:
                if wallets is None:
                    wallets = self.vault.list() if wallet is None else [self.vault.resolve(wallet)]
                # Hidden tokens ride along once an hour; the fast lane skips them.
                include_hidden = self._now() - self._hidden_sync_at >= HIDDEN_RESCAN_S
                if include_hidden and wallet is None and not chains:
                    self._hidden_sync_at = self._now()
                for record in wallets:
                    for chain in chains or self.chains():
                        try:
                            if await self.syncer.sync(
                                record, chain, full=full, include_hidden=include_hidden
                            ):
                                changed += 1
                                await self._emit(
                                    "trading.changed", {"reason": "sync", "wallet": record.address}
                                )
                        except (EvmRpcError, EvmTransportError, httpx.HTTPError) as exc:
                            log.warning(
                                "trading.sync_failed",
                                chain=chain.key,
                                wallet=record.address,
                                error=str(exc),
                            )
                            self.syncer.record_failure(chain, record.key, str(exc))
                self.last_sync_at = self._now()
            finally:
                self.syncing = False
            return changed

    def request_sync(self, *, wallet: str | None = None, full: bool = False) -> None:
        loop = asyncio.get_running_loop()
        task = loop.create_task(self.sync_all(wallet=wallet, full=full), name="trading-sync-once")
        self._confirm_tasks.add(task)
        task.add_done_callback(self._confirm_tasks.discard)

    # ── quotes ─────────────────────────────────────────────────────────

    async def _value_usd(self, chain: ChainSpec, token: TokenMeta, amount_raw: int) -> float | None:
        price = await self.prices.price(chain, token.address)
        if price is None:
            return None
        return float(to_human(amount_raw, token.decimals)) * price

    async def _order_value_usd(
        self,
        chain: ChainSpec,
        meta_in: TokenMeta,
        amount_in_raw: int,
        meta_out: TokenMeta,
        amount_out_raw: int,
    ) -> float | None:
        """What a swap is worth for the guardrails: the *larger* of its two sides.

        The two legs of an honest swap price about the same; when they do
        not, one of the feeds is wrong — a stale or thin-pool price on the
        sell side would let a large order through the approval threshold
        and the daily cap as if it were small. Taking the larger side means
        a bad price can only make an order look bigger, never smaller.
        ``None`` only when neither side has a price at all.
        """
        in_usd = await self._value_usd(chain, meta_in, amount_in_raw)
        out_usd = await self._value_usd(chain, meta_out, amount_out_raw)
        sides = [v for v in (in_usd, out_usd) if v is not None]
        return max(sides) if sides else None

    async def _priced(
        self,
        provider: SwapProvider,
        *,
        chain: ChainSpec,
        swapper: str,
        meta_in: TokenMeta,
        meta_out: TokenMeta,
        amount_raw: int,
        slippage_pct: float | None,
        decision_origin: DecisionOrigin,
    ) -> ProviderQuote:
        """Quote through a provider, then fill in what that provider left blank.

        The aggregator publishes no price-impact percentage, and prices gas in
        the chain's own coin rather than dollars. Both are completed here from
        the engine's own price feed rather than inside the provider, so the
        agent guardrails and the ledger read the same numbers whichever
        provider routed the swap — in particular, ``agent_max_price_impact_pct``
        keeps biting when the route is thin.
        """
        quote = await provider.quote(
            chain=chain,
            swapper=swapper,
            token_in=NATIVE_ADDRESS if meta_in.native else meta_in.address,
            token_out=NATIVE_ADDRESS if meta_out.native else meta_out.address,
            amount_raw=amount_raw,
            slippage_pct=slippage_pct,
            decision_origin=decision_origin,
        )
        if quote.gas_usd is None and quote.gas_native is not None:
            native_price = await self.prices.price(chain, NATIVE_ADDRESS)
            if native_price:
                quote.gas_usd = float(quote.gas_native) * float(native_price)
        if quote.price_impact_pct is None:
            in_usd = await self._value_usd(chain, meta_in, quote.amount_in_raw)
            out_usd = await self._value_usd(chain, meta_out, quote.amount_out_raw)
            if in_usd and out_usd is not None and in_usd > 0:
                quote.price_impact_pct = max(0.0, (1 - out_usd / in_usd) * 100.0)
        return quote

    async def _raw_for_usd(self, chain: ChainSpec, token: TokenMeta, amount_usd: float) -> int:
        """How much of ``token`` is worth ``amount_usd`` right now, in raw units.

        "$5 of ETH" is how people size an order; the price is read once, here,
        so neither the user nor the agent has to divide by hand. A token
        without a USD price cannot be sized this way and says so.
        """
        if amount_usd <= 0:
            raise TradingError("trading.invalid", "amountUsd must be greater than zero")
        price = await self.prices.price(chain, token.address)
        if price is None or price <= 0:
            raise TradingError(
                "trading.unpriced",
                f"No USD price for {token.symbol or token.address} on {chain.name}; "
                "size the order in token units instead",
                details={"token": token.address, "chainId": chain.chain_id},
            )
        human = Decimal(str(amount_usd)) / Decimal(str(price))
        return to_raw(human, token.decimals)

    async def quote(
        self,
        *,
        chain: ChainSpec,
        wallet: str | None,
        token_in: str,
        token_out: str,
        amount_in: str | None = None,
        amount_usd: float | None = None,
        slippage_pct: float | None = None,
        initiator: Initiator = "manual",
    ) -> dict[str, Any]:
        """Price a swap. Size it in token units (``amount_in``) or in dollars (``amount_usd``)."""
        record = self.vault.resolve(wallet)
        meta_in = await self.resolve_token(chain, token_in)
        meta_out = await self.resolve_token(chain, token_out)
        if (amount_in is None) == (amount_usd is None):
            raise TradingError(
                "trading.invalid", "exactly one of amountIn or amountUsd is required"
            )
        if amount_in is not None:
            amount_raw = to_raw(amount_in, meta_in.decimals)
        else:
            amount_raw = await self._raw_for_usd(chain, meta_in, float(amount_usd or 0))
        if amount_raw <= 0:
            raise TradingError("trading.invalid", "amountIn must be greater than zero")
        slippage = slippage_pct if slippage_pct is not None else self.config.default_slippage_pct
        self._check_agent_slippage(initiator, slippage)
        try:
            quote = await self._priced(
                self.provider(),
                chain=chain,
                swapper=record.address,
                meta_in=meta_in,
                meta_out=meta_out,
                amount_raw=amount_raw,
                slippage_pct=slippage,
                decision_origin=_origin(initiator),
            )
        except ProviderError as exc:
            raise _err(exc) from exc
        value = await self._order_value_usd(
            chain, meta_in, amount_raw, meta_out, quote.amount_out_raw
        )
        verdict = self._verdict(initiator=initiator, value=value, record=record, quote=quote)
        return self._quote_dict(chain, record, meta_in, meta_out, quote, value, verdict)

    def _quote_dict(
        self,
        chain: ChainSpec,
        record: WalletRecord,
        meta_in: TokenMeta,
        meta_out: TokenMeta,
        quote: ProviderQuote,
        value: float | None,
        verdict: guardrails.GuardVerdict,
    ) -> dict[str, Any]:
        human_in = to_human(quote.amount_in_raw, meta_in.decimals)
        human_out = to_human(quote.amount_out_raw, meta_out.decimals)
        rate = format_ratio(human_out, human_in)
        return {
            "quoteId": quote.quote_id,
            "provider": quote.provider,
            "providerLabel": provider_label(quote.provider),
            "routing": quote.routing,
            "warnings": list(quote.warnings),
            "chainId": chain.chain_id,
            "wallet": record.address,
            "tokenIn": meta_in.to_dict(),
            "tokenOut": meta_out.to_dict(),
            "amountIn": format_amount(quote.amount_in_raw, meta_in.decimals),
            "amountInRaw": str(quote.amount_in_raw),
            "amountOut": format_amount(quote.amount_out_raw, meta_out.decimals),
            "amountOutRaw": str(quote.amount_out_raw),
            "minOut": format_amount(quote.min_out_raw, meta_out.decimals),
            "minOutRaw": str(quote.min_out_raw),
            "priceImpactPct": quote.price_impact_pct,
            "gasUsd": quote.gas_usd,
            "valueUsd": value,
            "rate": rate,
            "slippagePct": quote.slippage_pct,
            "expiresAt": int((quote.fetched_at + quote.fresh_for_s) * 1000),
            "guard": verdict.to_dict(),
        }

    # ── swaps ──────────────────────────────────────────────────────────

    def _order_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        chain_id = int(row["chain_id"])
        chain = CHAINS.get(chain_id)
        token_in = self._token_dict(chain_id, row["token_in"]) or {}
        token_out = self._token_dict(chain_id, row["token_out"]) or {}
        dec_out = int(token_out.get("decimals", 18))
        tx_hash = row.get("tx_hash")
        expected = row.get("expected_out_raw")
        min_out = row.get("min_out_raw")
        received = row.get("received_out_raw")
        recipient = row.get("recipient")
        return {
            "orderId": row["order_id"],
            "kind": row.get("kind") or "swap",
            "recipient": checksum_address(str(recipient)) if recipient else None,
            "recipientLabel": spender_label(str(recipient)) if recipient else None,
            "batchId": row.get("batch_id"),
            "createdAt": int(float(row["created_at"]) * 1000),
            "updatedAt": int(float(row["updated_at"]) * 1000),
            "chainId": chain_id,
            "wallet": checksum_address(str(row["wallet"])),
            "tokenIn": token_in,
            "tokenOut": token_out,
            "amountIn": row["amount_human"],
            "amountInRaw": row["amount_raw"],
            "expectedOut": format_amount(int(expected), dec_out) if expected else None,
            "minOut": format_amount(int(min_out), dec_out) if min_out else None,
            "receivedOut": format_amount(int(received), dec_out) if received else None,
            "valueUsd": row.get("value_usd"),
            "priceImpactPct": row.get("price_impact_pct"),
            "gasUsd": row.get("gas_usd"),
            "slippagePct": row.get("slippage_pct"),
            "status": row["status"],
            "reason": row.get("reason"),
            "initiator": row["initiator"],
            "sessionKey": row.get("session_key"),
            "note": row.get("note"),
            "txHash": tx_hash,
            "approvalTxHash": row.get("approval_tx_hash"),
            "explorerUrl": chain.tx_url(tx_hash) if chain and tx_hash else None,
            "expiresAt": int(float(row["expires_at"]) * 1000) if row.get("expires_at") else None,
            "deliveredToken": self._token_dict(chain_id, row.get("delivered_token")),
            "provider": row.get("provider") or DEFAULT_PROVIDER_ID,
            "providerLabel": provider_label(row.get("provider") or DEFAULT_PROVIDER_ID),
            "clientOrderId": row.get("client_order_id"),
            **(self._lp_order_fields(row) if row.get("kind") in LP_ORDER_KINDS else {}),
        }

    @staticmethod
    def _lp_plan(row: dict[str, Any]) -> dict[str, Any]:
        try:
            plan = json.loads(str(row.get("quote_json") or "{}"))
        except ValueError:
            return {}
        return plan if isinstance(plan, dict) else {}

    def _lp_order_fields(self, row: dict[str, Any]) -> dict[str, Any]:
        """What an LP write adds to the order JSON (``docs/lp-write.md``, order model).

        ``plan`` is the LpPlan the user approves; once confirmed, ``received`` /
        ``spent`` (``{base, quote}`` Amounts from the receipt) and, for a mint,
        the new ``tokenId``.
        """
        plan = self._lp_plan(row)
        settlement = plan.pop("settlement", None) or {}
        return {
            "plan": plan or None,
            "tokenId": plan.get("tokenId"),
            "received": settlement.get("received"),
            "spent": settlement.get("spent"),
            "expectedOut": None,
            "minOut": None,
            "receivedOut": None,
            "provider": LP_PROVIDER,
            "providerLabel": LP_PROVIDER_LABEL,
        }

    def get_order(self, order_id: str) -> dict[str, Any]:
        row = self.ledger.get_order(order_id)
        if row is None:
            raise TradingError("trading.invalid", f"no order {order_id}")
        return self._order_dict(row)

    def list_orders(
        self,
        *,
        status: str | None = None,
        wallet: str | None = None,
        limit: int = 50,
        kind: str | None = None,
    ) -> dict[str, Any]:
        key = self.vault.resolve(wallet).key if wallet else None
        rows = self.ledger.list_orders(status=status, wallet=key, limit=limit, kind=kind)
        return {
            "orders": [self._order_dict(r) for r in rows],
            "pendingApprovals": self.ledger.count_orders("awaiting_approval"),
        }

    def batch(self, batch_id: str) -> list[dict[str, Any]]:
        """Every leg of a multisend, oldest first."""
        rows = self.ledger.batch_orders(batch_id)
        if not rows:
            raise TradingError("trading.invalid", f"no batch {batch_id}")
        return [self._order_dict(r) for r in rows]

    # ── sends ──────────────────────────────────────────────────────────

    async def send(
        self,
        *,
        chain: ChainSpec,
        wallet: str | None,
        token: str,
        recipients: list[dict[str, Any]],
        initiator: Initiator,
        session_key: str | None,
        note: str | None,
        wait: bool = False,
        client_order_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Send one token to one or many addresses from one wallet.

        Every recipient becomes its own order; the orders of a multisend
        share a ``batch_id`` and are judged, approved, rejected and executed
        as one. The guardrail sees the batch's total: an agent cannot get a
        transfer through by chopping it up, and a person approving it sees
        one card, not twenty.

        ``client_order_id`` makes the call idempotent: a repeat with the
        same key returns the orders it created the first time, whatever
        state they are in, and creates nothing.
        """
        if not getattr(self.config, "enabled", True):
            raise TradingError("trading.disabled", "Trading is disabled in config")
        if initiator not in ("manual", "agent"):
            raise TradingError("trading.invalid", "initiator must be 'manual' or 'agent'")
        client_id = _client_order_id(client_order_id)
        if client_id is not None:
            existing = self.ledger.find_orders_by_client_id(client_id)
            if existing:
                return [self._order_dict(r) for r in existing]
        if not self.ensure_unlocked():
            raise TradingError("wallet.locked", "Wallet vault is locked")
        record = self.vault.resolve(wallet)
        meta = await self.resolve_token(chain, token)
        if not recipients:
            raise TradingError("trading.invalid", "at least one recipient is required")
        if len(recipients) > MAX_SEND_RECIPIENTS:
            raise TradingError(
                "trading.invalid", f"at most {MAX_SEND_RECIPIENTS} recipients per send"
            )
        legs: list[tuple[str, int]] = []
        seen: set[str] = set()
        for item in recipients:
            if not isinstance(item, dict):
                raise TradingError("trading.invalid", "each recipient must be an object")
            try:
                to = normalize_address(str(item.get("to") or ""))
            except ValueError as exc:
                raise TradingError("trading.invalid", str(exc)) from exc
            if to == NATIVE_ADDRESS:
                raise TradingError("trading.invalid", "recipient is the zero address")
            if to == record.key:
                raise TradingError("trading.invalid", "recipient is the sending wallet itself")
            if to in seen:
                raise TradingError(
                    "trading.invalid", f"{checksum_address(to)} appears twice in the recipients"
                )
            seen.add(to)
            amount = item.get("amount")
            amount_usd = item.get("amountUsd")
            if (amount is None) == (amount_usd is None):
                raise TradingError(
                    "trading.invalid", "each recipient needs exactly one of amount or amountUsd"
                )
            if amount is not None:
                raw = to_raw(str(amount), meta.decimals)
            else:
                raw = await self._raw_for_usd(chain, meta, float(amount_usd or 0))
            if raw <= 0:
                raise TradingError("trading.invalid", "amount must be greater than zero")
            legs.append((to, raw))
        total = sum(raw for _, raw in legs)
        evm = self.evm(chain)
        balance = await self._balance_raw(chain, record, meta)
        if balance < total:
            raise TradingError(
                "trading.insufficient_balance",
                f"{record.label} holds {format_amount(balance, meta.decimals)} "
                f"{meta.symbol or 'tokens'}, the send needs {format_amount(total, meta.decimals)}",
            )
        # A batch of native sends must also leave gas for every leg; each
        # leg re-checks at signing time, but the whole batch should not be
        # accepted only to fail from the third leg on.
        if meta.native and len(legs) > 1:
            try:
                max_fee, _tip = await evm.fee_data()
            except (EvmRpcError, EvmTransportError):
                max_fee = 0
            reserve = len(legs) * 21_000 * max_fee
            if balance < total + reserve:
                raise TradingError(
                    "trading.insufficient_balance",
                    f"not enough {chain.native_symbol} for {len(legs)} sends plus their gas "
                    f"(need ~{format_amount(total + reserve, 18)})",
                )
        price = await self.prices.price(chain, meta.address)
        values = [
            float(to_human(raw, meta.decimals)) * price if price is not None else None
            for _, raw in legs
        ]
        total_value = sum(v for v in values if v is not None) if price is not None else None
        # Spend is read before the rows exist, so none of them count against themselves.
        spent = self._spent_today(record)
        batch_id = new_batch_id() if len(legs) > 1 else None
        now = self._now()
        order_ids: list[str] = []
        for (to, raw), value in zip(legs, values, strict=True):
            order_id = new_order_id()
            try:
                self.ledger.insert_order(
                    {
                        "order_id": order_id,
                        "created_at": now,
                        "updated_at": now,
                        "chain_id": chain.chain_id,
                        "wallet": record.key,
                        "token_in": meta.address,
                        "token_out": meta.address,
                        "amount_raw": str(raw),
                        "amount_human": format_amount(raw, meta.decimals),
                        "value_usd": value,
                        "status": "quoted",
                        "initiator": initiator,
                        "session_key": session_key,
                        "note": note,
                        "kind": "send",
                        "recipient": to,
                        "batch_id": batch_id,
                        "client_order_id": client_id,
                    }
                )
            except sqlite3.IntegrityError:
                # Two identical calls raced past the lookup above; the other
                # one's rows are this call's answer.
                if client_id is not None:
                    twins = self.ledger.find_orders_by_client_id(client_id)
                    return [self._order_dict(r) for r in twins]
                raise
            order_ids.append(order_id)
        verdict = guardrails.evaluate_transfer(
            initiator=initiator,
            value_usd=total_value,
            daily_cap_usd=self.config.daily_cap_usd,
            spent_today_usd=spent,
        )
        await self._decide_batch(order_ids, verdict, batch_id=batch_id, wait=wait)
        await self._emit(
            "trading.changed", {"reason": "order", "orderId": order_ids[0], "batchId": batch_id}
        )
        return [self.get_order(order_id) for order_id in order_ids]

    async def _decide_batch(
        self,
        order_ids: list[str],
        verdict: guardrails.GuardVerdict,
        *,
        batch_id: str | None,
        wait: bool,
    ) -> None:
        """Park, refuse or run a set of freshly quoted orders on one verdict."""
        if verdict.decision == "blocked_daily_cap":
            for order_id in order_ids:
                self.ledger.update_order(order_id, status="rejected", reason=verdict.reason)
                await self._emit("trading.order.finished", {"order": self.get_order(order_id)})
            return
        if verdict.decision == "needs_approval":
            ttl = int(self.config.approval_ttl_seconds)
            expires = self._now() + ttl
            for order_id in order_ids:
                self.ledger.update_order(
                    order_id,
                    status="awaiting_approval",
                    reason=verdict.reason,
                    expires_at=expires,
                )
            await self._emit(
                "trading.approval.requested",
                {
                    "order": self.get_order(order_ids[0]),
                    "batchId": batch_id,
                    "orders": [self.get_order(o) for o in order_ids],
                },
            )
            return
        await self._run_legs(order_ids, wait=wait)

    async def _run_legs(self, order_ids: list[str], *, wait: bool) -> None:
        """Execute orders one after another; a leg that fails never stops the rest."""
        for order_id in order_ids:
            try:
                await self._execute(order_id, None, wait=wait)
            except Exception as exc:
                await self._fail_order(order_id, _err(exc))
            self._wake(order_id)

    async def _fail_order(self, order_id: str, error: TradingError) -> None:
        """Record an exception from an order's pipeline without lying about the chain.

        Before the transaction is broadcast the order is simply ``failed``.
        Once it carries a ``tx_hash`` the exception says nothing about the
        swap itself — a transient RPC error while *settling* a mined
        transaction is the common case — so the status is left where it is
        (``submitted``) and ``recover_submitted`` books it on a later pass.
        A status the settlement already made final is never touched.
        """
        row = self.ledger.get_order(order_id)
        if row is None:
            return
        status = str(row["status"])
        if status in ORDER_FINAL_STATUSES:
            return
        if row.get("tx_hash") or status == "submitted":
            # A refused broadcast keeps its reason: it decides how long the
            # order is watched (``_confirm``), and it is the real story.
            if not str(row.get("reason") or "").startswith(BROADCAST_REJECTED):
                self.ledger.update_order(
                    order_id,
                    expect_status=status,
                    reason=f"settling: {error.code}: {error}",
                )
            log.warning("trading.settle_deferred", order=order_id, error=str(error))
            return
        self.ledger.update_order(
            order_id, expect_status=status, status="failed", reason=f"{error.code}: {error}"
        )
        log.warning("trading.order_failed", order=order_id, error=str(error))
        await self._emit("trading.order.finished", {"order": self.get_order(order_id)})

    async def revoke(
        self,
        *,
        chain: ChainSpec,
        wallet: str | None,
        token: str,
        spender: str,
        initiator: Initiator,
        session_key: str | None,
        note: str | None,
        wait: bool = False,
    ) -> dict[str, Any]:
        """Set an ERC-20 allowance back to zero. An agent's revoke waits for a human."""
        if not getattr(self.config, "enabled", True):
            raise TradingError("trading.disabled", "Trading is disabled in config")
        if not self.ensure_unlocked():
            raise TradingError("wallet.locked", "Wallet vault is locked")
        record = self.vault.resolve(wallet)
        meta = await self.resolve_token(chain, token)
        if meta.native:
            raise TradingError("trading.invalid", "the gas coin has no allowances to revoke")
        try:
            spender_key = normalize_address(spender)
        except ValueError as exc:
            raise TradingError("trading.invalid", str(exc)) from exc
        if spender_key == NATIVE_ADDRESS:
            raise TradingError("trading.invalid", "spender is the zero address")
        evm = self.evm(chain)
        allowance = await evm.erc20_allowance(meta.address, record.address, spender_key)
        if allowance <= 0:
            raise TradingError(
                "trading.invalid",
                f"{checksum_address(spender_key)} has no allowance on "
                f"{meta.symbol or meta.address}",
            )
        order_id = new_order_id()
        now = self._now()
        self.ledger.insert_order(
            {
                "order_id": order_id,
                "created_at": now,
                "updated_at": now,
                "chain_id": chain.chain_id,
                "wallet": record.key,
                "token_in": meta.address,
                "token_out": meta.address,
                "amount_raw": str(allowance),
                "amount_human": "unlimited"
                if is_unlimited(allowance)
                else format_amount(allowance, meta.decimals),
                "value_usd": 0.0,
                "status": "quoted",
                "initiator": initiator,
                "session_key": session_key,
                "note": note,
                "kind": "revoke",
                "recipient": spender_key,
            }
        )
        verdict = guardrails.evaluate_revoke(initiator=initiator)
        await self._decide_batch([order_id], verdict, batch_id=None, wait=wait)
        await self._emit("trading.changed", {"reason": "order", "orderId": order_id})
        return self.get_order(order_id)

    # ── Uniswap V4 LP writes (docs/lp-write.md) ────────────────────────

    def _lp_env(self, chain: ChainSpec) -> Any:
        """A fresh ``lp.ChainEnv`` for one plan: the engine's RPC, prices and vault."""
        from agentos.trading import lp

        loop = asyncio.get_running_loop()
        if self.lp_env_factory is not None:
            return self.lp_env_factory(chain, loop)
        return lp._engine_env(self, chain, loop)

    async def _lp_run(self, chain: ChainSpec, work: Callable[[Any], Any]) -> Any:
        """Run blocking LP work against a started env in a worker thread; errors coded."""
        from agentos.trading import lp

        env = self._lp_env(chain)
        try:
            return await asyncio.to_thread(lambda: work(lp.start(env)))
        except Exception as exc:
            raise lp._as_trading_error(exc, chain) from exc

    def _lp_preflight(
        self, initiator: str, client_order_id: str | None
    ) -> tuple[str | None, dict[str, Any] | None]:
        """(validated idempotency key, the order it already names — if any)."""
        if not getattr(self.config, "enabled", True):
            raise TradingError("trading.disabled", "Trading is disabled in config")
        if initiator not in ("manual", "agent"):
            raise TradingError("trading.invalid", "initiator must be 'manual' or 'agent'")
        client_id = _client_order_id(client_order_id)
        if client_id is not None:
            existing = self.ledger.find_orders_by_client_id(client_id)
            if existing:
                return client_id, self._order_dict(existing[0])
        if not self.ensure_unlocked():
            raise TradingError("wallet.locked", "Wallet vault is locked")
        return client_id, None

    async def lp_collect(
        self,
        *,
        chain: ChainSpec,
        token_id: int,
        initiator: Initiator,
        session_key: str | None,
        note: str | None,
        client_order_id: str | None = None,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        """Park an order that collects a V4 position's fees (always awaits approval).

        Nothing uncollected is ``trading.lp.nothing_to_collect`` unless ``allow_empty``.
        """
        from agentos.trading import lp_write

        client_id, existing = self._lp_preflight(initiator, client_order_id)
        if existing is not None:
            return existing
        plan = await self._lp_run(
            chain,
            lambda env: lp_write.plan_collect(env, int(token_id), allow_empty=allow_empty),
        )
        return await self._lp_order(
            chain, plan, initiator=initiator, session_key=session_key, note=note,
            client_id=client_id,
        )  # fmt: skip

    async def lp_remove(
        self,
        *,
        chain: ChainSpec,
        token_id: int,
        pct: float = 100.0,
        slippage_pct: float | None = None,
        initiator: Initiator,
        session_key: str | None,
        note: str | None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Park an order that removes ``pct`` % of a position (100 burns it), fees included."""
        from agentos.trading import lp_write

        client_id, existing = self._lp_preflight(initiator, client_order_id)
        if existing is not None:
            return existing
        self._check_agent_slippage(initiator, slippage_pct)
        plan = await self._lp_run(
            chain,
            lambda env: lp_write.plan_remove(env, int(token_id), float(pct), slippage_pct),
        )
        return await self._lp_order(
            chain, plan, initiator=initiator, session_key=session_key, note=note,
            client_id=client_id,
        )  # fmt: skip

    async def lp_add(
        self,
        *,
        chain: ChainSpec,
        target: str | None,
        quote: str | None = None,
        fee: int | None = None,
        usd: float | None = None,
        amount_base: str | None = None,
        amount_quote: str | None = None,
        range_spec: str | None = None,
        to_position: int | None = None,
        wallet: str | None = None,
        slippage_pct: float | None = None,
        initiator: Initiator,
        session_key: str | None,
        note: str | None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Park an order that mints a position (or adds to ``to_position``) from a deposit.

        ``target`` may be a ``TOKEN/QUOTE`` pair; ``quote`` and ``fee`` (a V4 fee,
        ``lp.parse_fee``) narrow the pool among those holding the token.
        """
        from agentos.trading import lp, lp_write

        client_id, existing = self._lp_preflight(initiator, client_order_id)
        if existing is not None:
            return existing
        self._check_agent_slippage(initiator, slippage_pct)
        if target:
            target, quote = lp.split_pair(target, quote)
        address = await lp._resolve_address(self, chain, target) if target else None
        quote_address = await lp._resolve_address(self, chain, quote) if quote else None
        if to_position is None:
            owner: str | None = self.vault.resolve(wallet).address
        else:
            owner = self.vault.resolve(wallet).address if wallet else None
        plan = await self._lp_run(
            chain,
            lambda env: lp_write.plan_add(
                env,
                address,
                quote=quote_address,
                fee=fee,
                usd=usd,
                amount_base=amount_base,
                amount_quote=amount_quote,
                range_spec=range_spec,
                to_position=to_position,
                wallet=owner,
                slippage_pct=slippage_pct,
            ),
        )
        return await self._lp_order(
            chain, plan, initiator=initiator, session_key=session_key, note=note,
            client_id=client_id,
        )  # fmt: skip

    def _lp_remember(self, chain: ChainSpec, token: dict[str, Any]) -> str:
        """Keep a plan token in the token table (so orders can show it); its key."""
        address = str(token.get("address") or NATIVE_ADDRESS)
        if is_native(address):
            return NATIVE_ADDRESS
        key = normalize_address(address)
        if self.ledger.get_token(chain.chain_id, key) is None:
            symbol = str(token.get("symbol") or "")
            decimals = int(token.get("decimals") or 18)
            self._remember_token(TokenMeta(chain.chain_id, key, symbol, symbol, decimals))
        # The user put money into (or took it out of) this token on purpose.
        self.ledger.touch_token(chain.chain_id, key)
        return key

    async def _lp_order(
        self,
        chain: ChainSpec,
        plan: dict[str, Any],
        *,
        initiator: str,
        session_key: str | None,
        note: str | None,
        client_id: str | None,
    ) -> dict[str, Any]:
        """Insert the order for a plan and park it: every LP write waits for a human."""
        from agentos.trading.lp_write import ORDER_KIND

        op = str(plan["op"])
        kind = ORDER_KIND[op]
        try:
            record = self.vault.get(str(plan["wallet"]))
        except (VaultError, WalletNotFoundError) as exc:
            raise TradingError(
                "trading.lp.not_owner", f"{plan['wallet']} is not a wallet in this vault"
            ) from exc
        base = self._lp_remember(chain, plan["token"])
        quote = self._lp_remember(chain, plan["quote"])
        value = (plan.get("expected") or {}).get("usd")
        liquidity = int(plan.get("liquidity") or 0)
        # Spend is read before the row exists, so the order does not count against itself.
        spent = self._spent_today(record)
        order_id = new_order_id()
        now = self._now()
        try:
            self.ledger.insert_order(
                {
                    "order_id": order_id,
                    "created_at": now,
                    "updated_at": now,
                    "chain_id": chain.chain_id,
                    "wallet": record.key,
                    "token_in": base,
                    "token_out": quote,
                    "amount_raw": str(liquidity),
                    "amount_human": str(liquidity),
                    "value_usd": value,
                    "gas_usd": plan.get("gasUsd"),
                    "slippage_pct": plan.get("slippagePct"),
                    "status": "quoted",
                    "initiator": initiator,
                    "session_key": session_key,
                    "note": note,
                    "kind": kind,
                    "recipient": record.key if op in ("collect", "remove") else None,
                    "quote_json": json.dumps(plan),
                    "provider": LP_PROVIDER,
                    "client_order_id": client_id,
                }
            )
        except sqlite3.IntegrityError:
            if client_id is not None:
                twins = self.ledger.find_orders_by_client_id(client_id)
                if twins:
                    return self._order_dict(twins[0])
            raise
        verdict = guardrails.evaluate_lp_write(
            op=op,
            initiator=initiator,
            value_usd=value,
            daily_cap_usd=self.config.daily_cap_usd,
            spent_today_usd=spent,
        )
        await self._decide_batch([order_id], verdict, batch_id=None, wait=False)
        await self._emit("trading.changed", {"reason": "order", "orderId": order_id})
        return self.get_order(order_id)

    async def _execute_lp(self, row: dict[str, Any], *, wait: bool) -> None:
        """Re-validate an approved LP plan, run its approvals, then ``modifyLiquidities``.

        Approvals are exact (the plan's maxima, never unlimited) and go through
        ``_send`` like a swap's: ``ERC20.approve(Permit2)`` when that allowance
        is short, then ``Permit2.approve(token, PositionManager, amount, now +
        30 min)`` when that one is short or lapsing. Each waits for its receipt
        -- and for the node to show it -- before the next step.
        """
        from agentos.trading import lp, lp_write

        order_id = str(row["order_id"])
        chain = CHAINS[int(row["chain_id"])]
        record = self.vault.get(str(row["wallet"]))
        key = self.vault.private_key(record.address)
        plan = self._lp_plan(row)
        plan.pop("settlement", None)
        evm = self.evm(chain)
        for token in {NATIVE_ADDRESS, str(row["token_in"]), str(row["token_out"])}:
            await self.syncer.ensure_opening(record, chain, token, evm)
        permit2 = str(lp.unilp().chains.CHAINS[chain.key]["permit2"]).lower()
        async with self._wallet_lock(record.key):
            check = await self._lp_run(chain, lambda env: lp_write.revalidate(env, plan))
            for step in [s for s in check["approvals"] if s["needed"]]:
                expiration = int(check["timestamp"]) + lp_write.PERMIT2_EXPIRY_S

                def encode(env: Any, s: dict[str, Any] = step, exp: int = expiration) -> Any:
                    return lp_write.approval_call(env, s, exp)

                to, data = await self._lp_run(chain, encode)
                tx_hash = await self._send(
                    chain,
                    record,
                    key,
                    {
                        "from": record.address,
                        "to": to,
                        "data": data,
                        "value": "0",
                        "chainId": chain.chain_id,
                    },
                    order_id=order_id,
                    hash_field="approval_tx_hash",
                    value_usd=row.get("value_usd"),
                )
                receipt = await evm.wait_for_sealed_receipt(tx_hash, timeout_s=RECEIPT_TIMEOUT_S)
                if receipt is None:
                    raise TradingError(
                        "trading.tx_pending",
                        f"approval {tx_hash} was not mined within the wait window; "
                        "approve the order again once it lands",
                    )
                if not receipt_succeeded(receipt):
                    raise TradingError(
                        "trading.tx_failed", f"approval transaction failed ({tx_hash})"
                    )
                await self._record_gas(
                    chain,
                    record,
                    receipt,
                    kind="approval",
                    order_id=order_id,
                    initiator=str(row["initiator"]),
                )
                self._lp_mark_approval(plan, step, tx_hash)
                self.ledger.update_order(order_id, quote_json=json.dumps(plan))
                amount = int(step["amountRaw"])
                if step["step"] == "erc20->permit2":
                    await self._await_allowance(
                        evm,
                        token=str(step["token"]).lower(),
                        owner=record.address,
                        spenders=frozenset({permit2}),
                        amount_raw=amount,
                    )
                else:
                    await self._await_permit2(chain, record.address, step, check)
            # The approvals above can take minutes each: sign with a deadline
            # counted from the chain's clock now, not from the re-validation.
            deadline = await self._lp_deadline(chain, int(check["deadline"]))
            to, data, value = lp_write.modify_liquidities_call(plan, deadline)
            pre_native = await evm.get_balance(record.address)
            tx_hash = await self._send(
                chain,
                record,
                key,
                {
                    "from": record.address,
                    "to": to,
                    "data": data,
                    "value": value,
                    "chainId": chain.chain_id,
                },
                order_id=order_id,
                value_usd=row.get("value_usd"),
            )
        await self._emit("trading.changed", {"reason": "order", "orderId": order_id})
        await self._watch(order_id, tx_hash, {"in": 0, "out": 0, "native": pre_native}, wait=wait)

    async def _lp_deadline(self, chain: ChainSpec, previous: int) -> int:
        """A ``modifyLiquidities`` deadline counted from the latest block (else wall clock).

        Never earlier than ``previous``; ``plan_hash`` leaves the deadline out,
        so moving it later does not invalidate the approved plan.
        """
        from agentos.trading import lp_write

        try:
            ts = int(await self._lp_run(chain, lp_write.block_timestamp))
        except TradingError as exc:
            log.warning("trading.lp_deadline_clock", error=str(exc))
            ts = 0
        if ts <= 0:
            ts = int(self._now())
        return max(previous, ts + lp_write.DEADLINE_S)

    @staticmethod
    def _lp_mark_approval(plan: dict[str, Any], step: dict[str, Any], tx_hash: str) -> None:
        """Record a mined approval's hash on the plan's matching ``approvals[]`` entry."""
        steps = plan.setdefault("approvals", [])
        for entry in steps:
            same_token = str(entry.get("token")).lower() == str(step["token"]).lower()
            if same_token and entry.get("step") == step["step"]:
                entry["txHash"] = tx_hash
                entry["amountRaw"] = step["amountRaw"]
                return
        steps.append({**step, "txHash": tx_hash})

    async def _await_permit2(
        self, chain: ChainSpec, owner: str, step: dict[str, Any], check: dict[str, Any]
    ) -> None:
        """Wait until the node shows the Permit2 allowance just mined (never fatal)."""
        from agentos.trading import lp_write

        amount = int(step["amountRaw"])
        token = str(step["token"])
        now = int(check["timestamp"])
        for attempt in range(CHAIN_CATCHUP_ATTEMPTS):
            if attempt:
                await asyncio.sleep(CHAIN_CATCHUP_POLL_S)
            try:
                seen = await self._lp_run(
                    chain,
                    lambda env: lp_write.permit2_visible(env, owner, token, amount, now),
                )
            except TradingError:
                continue
            if seen:
                return
        log.warning("trading.permit2_not_visible", token=step["token"], owner=owner)

    async def _lp_native_delta(
        self,
        evm: EvmClient,
        record: WalletRecord,
        receipt: dict[str, Any],
        *,
        gas_wei: int,
        pre_native: int,
        plausible: Callable[[int], bool],
    ) -> tuple[int | None, int | None]:
        """(native the write moved for the wallet, gas excluded; its balance after).

        ETH has no Transfer log: a deposit is ``value`` less the SWEEP refund, a
        payout is an internal call. So the balance is read -- against the
        pre-send snapshot first (retried until the node has the block), then
        pinned to the receipt's block and the one before it, which no lagging
        ``latest`` can distort. A delta ``plausible`` rejects is not believed.
        ``(None, None)`` when nothing believable came back.
        """
        if pre_native:
            post = await self._native_after(evm, record.address, receipt, pre=pre_native)
            delta = post - pre_native + gas_wei
            if post != pre_native and plausible(delta):
                return delta, post
        block_number = int(str(receipt.get("blockNumber") or "0x0"), 16)
        if block_number > 0:
            try:
                before = await evm.get_balance(record.address, hex(block_number - 1))
                after = await evm.get_balance(record.address, hex(block_number))
            except (EvmRpcError, EvmTransportError) as exc:
                log.warning("trading.lp_native_read_failed", error=str(exc))
            else:
                delta = after - before + gas_wei
                if plausible(delta):
                    return delta, after
        return None, None

    async def _settle_lp(
        self,
        order_id: str,
        row: dict[str, Any],
        tx_hash: str,
        receipt: dict[str, Any],
        pre: dict[str, int],
        chain: ChainSpec,
        record: WalletRecord,
    ) -> None:
        """Book a mined LP write from its receipt (``docs/lp-write.md``, execution 7).

        ERC-20 legs are the receipt's Transfer logs to and from the wallet; the
        native leg is measured (see ``_lp_native_delta``) and, failing that,
        taken from the plan. Entries of the order's kind, lots by the
        deposit/withdraw convention, the new tokenId of a mint, and -- for an
        agent's ``add`` -- the day's spend, all with the confirmation.
        """
        from agentos.trading import lp
        from agentos.trading.lp_write import minted_token_id
        from agentos.trading.sync import lp_note

        kind = str(row["kind"])
        evm = self.evm(chain)
        gas_wei = receipt_gas_wei(receipt)
        gas_usd = await self._gas_usd(chain, gas_wei)
        if not receipt_succeeded(receipt):
            await self._settle_reverted(
                order_id,
                row,
                tx_hash,
                gas_wei,
                gas_usd,
                chain,
                record,
                note=f"reverted {kind.replace('_', ' ')}",
            )
            return
        plan = self._lp_plan(row)
        plan.pop("settlement", None)
        wallet = record.key
        ins: dict[str, list[int]] = {}
        outs: dict[str, list[int]] = {}
        for t in receipt_transfers(receipt):
            if t.recipient == wallet and t.sender != wallet:
                ins.setdefault(t.token, [0, t.log_index])[0] += t.amount
            elif t.sender == wallet and t.recipient != wallet:
                outs.setdefault(t.token, [0, t.log_index])[0] += t.amount
        key = (plan.get("pool") or {}).get("poolKey") or {}
        native_leg = is_native(str(key.get("currency0") or "0x1"))
        post_native: int | None = None
        if native_leg:
            expected_native = self._lp_native_expected(plan)
            fee_native = 0
            if kind == "lp_add":
                ceiling = int(plan.get("value") or 0)
                # An increase that closes each currency pays uncollected fees
                # beyond what the new liquidity needs back out: with native
                # fees larger than the deposit, the wallet nets ETH *in*. Up to
                # those fees (plus what they may have grown since planning).
                if "CLOSE_CURRENCY" in (plan.get("actions") or []):
                    fee_native = self._lp_native_expected(plan, "fees")
                payout = fee_native + fee_native // LP_FEE_GROWTH_TOLERANCE_DIV

                def plausible(delta: int) -> bool:
                    return -ceiling <= delta <= payout
            else:

                def plausible(delta: int) -> bool:
                    return delta >= 0

            delta, post_native = await self._lp_native_delta(
                evm,
                record,
                receipt,
                gas_wei=gas_wei,
                pre_native=int(pre.get("native") or 0),
                plausible=plausible,
            )
            if delta is None:
                log.warning("trading.lp_native_assumed", order=order_id, booked=expected_native)
                delta = fee_native - expected_native if kind == "lp_add" else expected_native
            if delta > 0:
                ins[NATIVE_ADDRESS] = [delta, LP_NATIVE_LOG_INDEX]
            elif delta < 0:
                outs[NATIVE_ADDRESS] = [-delta, LP_NATIVE_LOG_INDEX]
        reason: str | None = None
        if kind == "lp_add" and not plan.get("increase"):
            minted = minted_token_id(receipt, str(plan.get("positionManager") or ""), wallet)
            if minted is None:
                reason = "mined, but the new position's tokenId was not in the receipt"
                log.warning("trading.lp_mint_id_missing", order=order_id, tx=tx_hash)
            else:
                plan["tokenId"] = str(minted)
        block_number = int(str(receipt.get("blockNumber") or "0x0"), 16)
        ts: int | None = None
        if block_number:
            try:
                ts = await evm.block_timestamp(block_number)
            except (EvmRpcError, EvmTransportError) as exc:
                log.warning("trading.block_timestamp_failed", order=order_id, error=str(exc))
        in_list = [(t, a, i) for t, (a, i) in ins.items()]
        out_list = [(t, a, i) for t, (a, i) in outs.items()]
        await self.syncer._book_lp(
            record,
            chain,
            kind=kind,
            ins=in_list,
            outs=out_list,
            ts=float(ts or self._now()),
            tx_hash=tx_hash,
            initiator=str(row["initiator"]),
            gas_usd=gas_usd,
            order_id=order_id,
            session_key=row.get("session_key"),
            note=row.get("note") or lp_note(kind, plan),
        )
        # The balance cache follows, so the next sync does not book the LP
        # write's movements a second time as deposits or withdrawals.
        for token in {str(row["token_in"]), str(row["token_out"])} - {NATIVE_ADDRESS}:
            meta = await self._token_meta(chain, token)
            cached = self.ledger.get_balance(chain.chain_id, wallet, token) or 0
            moved = ins.get(token, [0])[0] - outs.get(token, [0])[0]
            post = await self._balance_after(chain, record, meta, fallback=max(0, cached + moved))
            self.ledger.set_balance(chain.chain_id, wallet, token, post)
        if post_native is None:
            post_native = await self._native_after(evm, record.address, receipt)
        self.ledger.set_balance(chain.chain_id, wallet, NATIVE_ADDRESS, post_native)

        async def side(moves: dict[str, list[int]]) -> dict[str, Any]:
            out: dict[str, Any] = {}
            for which in ("token", "quote"):
                token = plan.get(which) or {}
                address = str(token.get("address") or NATIVE_ADDRESS).lower()
                address = NATIVE_ADDRESS if is_native(address) else address
                raw = moves.get(address, [0])[0]
                price = await self.prices.price(chain, address) if raw else None
                out["base" if which == "token" else "quote"] = lp.amount_json(
                    raw, int(token.get("decimals") or 18), price
                )
            return out

        plan["settlement"] = {
            "txHash": tx_hash,
            "block": block_number,
            "tokenId": plan.get("tokenId"),
            "received": await side(ins),
            "spent": await side(outs),
            "ins": [list(m) for m in in_list],
            "outs": [list(m) for m in out_list],
            "gasUsd": gas_usd,
        }
        self.ledger.settle_order(
            order_id,
            expect_status="submitted",
            spend=self._spend_for(row, record) if kind == "lp_add" else None,
            status="confirmed",
            reason=reason,
            gas_wei=str(gas_wei),
            gas_usd=gas_usd,
            quote_json=json.dumps(plan),
        )
        self._wake(order_id)
        await self._emit("trading.order.finished", {"order": self.get_order(order_id)})
        await self._emit("trading.changed", {"reason": "order", "orderId": order_id})

    @staticmethod
    def _lp_native_expected(plan: dict[str, Any], field: str = "expected") -> int:
        """The plan's native (ETH) amount: what an add deposits or a removal pays out.

        ``field="fees"``: the native side of the position's uncollected fees.
        """
        expected = plan.get(field) or {}
        for which, leg in (("token", "base"), ("quote", "quote")):
            if is_native(str((plan.get(which) or {}).get("address") or "0x1")):
                return int(((expected.get(leg) or {}).get("raw")) or 0)
        return 0

    # ── allowances ─────────────────────────────────────────────────────

    async def allowances(
        self,
        chain: ChainSpec,
        wallet: str | None,
        *,
        full: bool = False,
        wait: bool = False,
    ) -> dict[str, Any]:
        """What this wallet has let others spend, with the live amounts.

        The Approval logs are scanned incrementally from where the last scan
        stopped (a new wallet starts at its creation block, an imported one
        at the sync's look-back); the (token, spender) pairs are cached, and
        each pair's allowance is read from the chain now. A pair that reads
        as zero is dropped from the cache. ``full`` rescans from the start.

        The scan runs in the background — a wallet's first pass on a 0.1 s
        chain is millions of blocks — and saves its place after every
        window, so this returns at once with what is known and
        ``scanning: True``; ``wait`` blocks until the pass is done. Every
        window that adds a pair, and the end of the pass, broadcast
        ``trading.changed`` with ``reason: "allowances"``.
        """
        record = self.vault.resolve(wallet)
        evm = self.evm(chain)
        latest = await evm.block_number()
        key = (chain.chain_id, record.key)
        if full:
            # A pass still running would write its next window's cursor over
            # the cleared one and the rescan would silently resume mid-way.
            running = self._allowance_scans.get(key)
            if running is not None and not running.done():
                running.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await running
            self.ledger.clear_allowance_scan(chain.chain_id, record.key)
        scan = self.ledger.allowance_scan(chain.chain_id, record.key)
        if scan is None:
            created = record.created_block.get(str(chain.chain_id))
            state = self.ledger.sync_state(chain.chain_id, record.key)
            if created is not None:
                start = int(created)
            elif state is not None and state.get("oldest_block") is not None:
                start = int(state["oldest_block"])
            else:
                start = max(0, latest - self.syncer.initial_lookback)
        else:
            start = int(scan["last_block"]) + 1
        task = self._allowance_scans.get(key)
        if start <= latest and (task is None or task.done()):
            task = asyncio.get_running_loop().create_task(
                self._scan_allowances(chain, record, start, latest),
                name=f"trading-allowances-{chain.chain_id}-{record.key[:10]}",
            )
            self._allowance_scans[key] = task
        if wait and task is not None and not task.done():
            await task
        # "Scanning" is a catch-up worth telling the user about — more than
        # one window still to read. The few hundred blocks since the last
        # look are read in one call and are not a reason to say "partial".
        behind = latest - start + 1
        scanning = task is not None and not task.done() and behind > int(chain.approval_log_span)
        scan = self.ledger.allowance_scan(chain.chain_id, record.key)
        rows = self.ledger.allowances(chain.chain_id, record.key)
        pairs = [(str(r["token"]), str(r["spender"])) for r in rows]
        live = await evm.erc20_allowances(record.address, pairs)
        held = await evm.erc20_balances(record.address, {t for t, _ in pairs})
        out: list[dict[str, Any]] = []
        unlimited_count = 0
        for row in rows:
            token = str(row["token"])
            spender = str(row["spender"])
            amount = live.get((token, spender))
            if amount == 0:
                self.ledger.delete_allowance(chain.chain_id, record.key, token, spender)
                continue
            meta = await self.token_meta(chain, token)
            balance = held.get(token)
            price = await self.prices.price(chain, token)
            exposed = min(amount, balance) if amount is not None and balance is not None else None
            exposure_usd = (
                float(to_human(exposed, meta.decimals)) * price
                if exposed is not None and price is not None
                else None
            )
            unlimited = amount is not None and is_unlimited(amount)
            if unlimited:
                unlimited_count += 1
            out.append(
                {
                    "chainId": chain.chain_id,
                    "wallet": record.address,
                    "token": meta.to_dict(),
                    "spender": checksum_address(spender),
                    "spenderLabel": spender_label(spender),
                    "spenderUrl": chain.address_url(checksum_address(spender)),
                    "allowanceRaw": str(amount) if amount is not None else None,
                    "allowance": None
                    if amount is None
                    else ("unlimited" if unlimited else format_amount(amount, meta.decimals)),
                    "unlimited": unlimited,
                    "readFailed": amount is None,
                    "balanceRaw": str(balance) if balance is not None else None,
                    "balance": format_amount(balance, meta.decimals)
                    if balance is not None
                    else None,
                    "exposureUsd": exposure_usd,
                    "lastBlock": int(row["last_block"]),
                    "lastTxHash": row.get("last_tx_hash"),
                    "explorerUrl": chain.tx_url(str(row["last_tx_hash"]))
                    if row.get("last_tx_hash")
                    else None,
                }
            )
        # Unlimited first, then by how much is at stake, then most recent.
        out.sort(
            key=lambda a: (
                not a["unlimited"],
                -(a["exposureUsd"] or 0.0),
                -int(a["lastBlock"]),
            )
        )
        return {
            "chainId": chain.chain_id,
            "wallet": record.address,
            "allowances": out,
            "count": len(out),
            "unlimitedCount": unlimited_count,
            "scanning": scanning,
            "scannedTo": int(scan["last_block"]) if scan else None,
            "scanFrom": start if scanning else None,
            "head": latest,
        }

    async def _scan_allowances(
        self, chain: ChainSpec, record: WalletRecord, start: int, latest: int
    ) -> None:
        """Walk the Approval logs from ``start`` to ``latest`` one window at a time.

        Progress is written after every window, so a gateway restart (or a
        node outage half-way) resumes where it stopped instead of starting
        over. A window that fails ends the pass; the next review call picks
        it up from the last saved block.
        """
        evm = self.evm(chain)
        window = max(1, int(chain.approval_log_span))
        cursor = start
        while cursor <= latest:
            end = min(latest, cursor + window - 1)
            try:
                logs = await evm.approval_logs(
                    record.address, from_block=cursor, to_block=end, max_span=window
                )
            except (EvmRpcError, EvmTransportError) as exc:
                log.warning(
                    "trading.allowance_scan_failed",
                    chain=chain.key,
                    wallet=record.address,
                    block=cursor,
                    error=str(exc),
                )
                await self._emit(
                    "trading.changed", {"reason": "allowances", "wallet": record.address}
                )
                return
            added = 0
            for entry in logs:
                if entry.owner != record.key:
                    continue
                self.ledger.upsert_allowance(
                    chain.chain_id,
                    record.key,
                    entry.token,
                    entry.spender,
                    block=entry.block_number,
                    tx_hash=entry.tx_hash,
                )
                added += 1
            self.ledger.set_allowance_scan(chain.chain_id, record.key, last_block=end)
            if added:
                await self._emit(
                    "trading.changed", {"reason": "allowances", "wallet": record.address}
                )
            cursor = end + 1
        await self._emit("trading.changed", {"reason": "allowances", "wallet": record.address})

    # ── decode ─────────────────────────────────────────────────────────

    async def decode(
        self,
        chain: ChainSpec,
        *,
        tx_hash: str | None = None,
        data: str | None = None,
        to: str | None = None,
    ) -> dict[str, Any]:
        """A transaction hash or raw calldata, explained.

        With a hash the transaction and its receipt are read and every
        Transfer/Approval in the receipt is named; with calldata only the
        call itself is decoded. Unknown functions stay unknown.
        """
        evm = self.evm(chain)
        tx: dict[str, Any] | None = None
        receipt: dict[str, Any] | None = None
        value_wei = 0
        if tx_hash:
            tx_hash = tx_hash.strip().lower()
            if not tx_hash.startswith("0x") or len(tx_hash) != 66:
                raise TradingError("trading.invalid", f"not a transaction hash: {tx_hash}")
            tx = await evm.get_transaction(tx_hash)
            if tx is None:
                raise TradingError("trading.invalid", f"no transaction {tx_hash} on {chain.name}")
            receipt = await evm.get_transaction_receipt(tx_hash)
            data = str(tx.get("input") or tx.get("data") or "0x")
            to = str(tx.get("to") or "") or None
            value_wei = decode_uint(str(tx.get("value") or "0x0"))
        elif data is None:
            raise TradingError("trading.invalid", "txHash or data is required")
        call = decode_calldata(data)
        target = to.lower() if to else None
        target_label: str | None = spender_label(target) if target else None
        target_token: dict[str, Any] | None = None
        if target and not target_label and self.ledger.get_token(chain.chain_id, target):
            target_token = self._token_dict(chain.chain_id, target)
        decoded: dict[str, Any] | None = None
        if (
            call.known
            and call.function in ("transfer", "approve")
            and len(call.args) == 2
            and target
        ):
            try:
                meta = await self.token_meta(chain, target)
            except TradingError:
                meta = None
            if meta is not None:
                amount = int(str(call.args[1]["value"]))
                counterparty = str(call.args[0]["value"])
                decoded = {
                    "function": call.function,
                    "token": meta.to_dict(),
                    "counterparty": checksum_address(counterparty),
                    "counterpartyLabel": spender_label(counterparty),
                    "amountRaw": str(amount),
                    "amount": "unlimited"
                    if call.function == "approve" and is_unlimited(amount)
                    else format_amount(amount, meta.decimals),
                    "unlimited": call.function == "approve" and is_unlimited(amount),
                }
                target_token = meta.to_dict()
        transfers, approvals = receipt_movements(receipt)
        movements: list[dict[str, Any]] = []
        for entry in transfers[:50]:
            token = await self._safe_token_dict(chain, entry.token)
            decimals = int(token.get("decimals", 18))
            movements.append(
                {
                    "token": token,
                    "from": checksum_address(entry.sender),
                    "to": checksum_address(entry.recipient),
                    "amountRaw": str(entry.amount),
                    "amount": format_amount(entry.amount, decimals),
                    "logIndex": entry.log_index,
                }
            )
        grants: list[dict[str, Any]] = []
        for grant in approvals[:50]:
            token = await self._safe_token_dict(chain, grant.token)
            decimals = int(token.get("decimals", 18))
            grants.append(
                {
                    "token": token,
                    "owner": checksum_address(grant.owner),
                    "spender": checksum_address(grant.spender),
                    "spenderLabel": spender_label(grant.spender),
                    "amountRaw": str(grant.amount),
                    "amount": "unlimited"
                    if is_unlimited(grant.amount)
                    else format_amount(grant.amount, decimals),
                    "unlimited": is_unlimited(grant.amount),
                    "logIndex": grant.log_index,
                }
            )
        ours = {w.key for w in self.vault.list()} if self.vault.initialized else set()
        parties = {str(tx.get("from") or "").lower(), target or ""} if tx else {target or ""}
        for entry in transfers:
            parties.update({entry.sender, entry.recipient})
        involved = sorted(checksum_address(p) for p in parties if p and p in ours)
        return {
            "chainId": chain.chain_id,
            "call": call.to_dict(),
            "description": describe_call(call, to=target, value_wei=value_wei),
            "to": checksum_address(target) if target else None,
            "toLabel": target_label,
            "toToken": target_token,
            "decoded": decoded,
            "tx": tx_summary(tx, receipt) if tx_hash else None,
            "transfers": movements,
            "approvals": grants,
            "wallets": involved,
            "explorerUrl": chain.tx_url(tx_hash) if tx_hash else None,
        }

    async def _safe_token_dict(self, chain: ChainSpec, address: str) -> dict[str, Any]:
        try:
            return (await self.token_meta(chain, address)).to_dict()
        except (TradingError, EvmRpcError, EvmTransportError):
            return TokenMeta(chain.chain_id, address.lower(), "", "", 18).to_dict()

    # ── network ────────────────────────────────────────────────────────

    async def network(self, *, fresh: bool = False) -> dict[str, Any]:
        """Head block, its age, gas and latency for every chain; cached briefly.

        The age is the number that matters: a load-balanced endpoint that
        answers from a node a few blocks behind (see ``_native_after``) shows
        up here as a head older than the chain's block time, which is what a
        person needs to see before trusting a balance.
        """
        now = self._now()
        if (
            not fresh
            and self._network_cache is not None
            and now - self._network_cache_at < NETWORK_TTL_S
        ):
            return self._network_cache
        chains: list[dict[str, Any]] = []
        for chain in self.chains():
            evm = self.evm(chain)
            row: dict[str, Any] = {
                "chainId": chain.chain_id,
                "key": chain.key,
                "name": chain.name,
                "native": chain.native_symbol,
                "rpcUrl": evm.display_url,
                "healthy": False,
                "latencyMs": None,
                "blockNumber": None,
                "blockAgeS": None,
                "blockTimeS": chain.block_time_s,
                "baseFeeGwei": None,
                "priorityFeeGwei": None,
                "error": None,
            }
            started = time.monotonic()
            try:
                block = await evm.get_block("latest")
                row["latencyMs"] = int((time.monotonic() - started) * 1000)
                if not block:
                    raise EvmTransportError("no head block")
                number = decode_uint(str(block.get("number") or "0x0"))
                ts = decode_uint(str(block.get("timestamp") or "0x0"))
                row["blockNumber"] = number
                row["blockAgeS"] = max(0, int(now - ts)) if ts else None
                base_fee = decode_uint(str(block.get("baseFeePerGas") or "0x0"))
                try:
                    max_fee, tip = await evm.fee_data()
                except (EvmRpcError, EvmTransportError):
                    max_fee, tip = 0, 0
                if not base_fee and max_fee:
                    base_fee = max(0, (max_fee - tip) // 2)
                row["baseFeeGwei"] = base_fee / 1e9 if base_fee else None
                row["priorityFeeGwei"] = tip / 1e9 if tip else None
                age = row["blockAgeS"]
                row["healthy"] = age is not None and age <= STALE_HEAD_S
            except (EvmRpcError, EvmTransportError, ValueError, TypeError) as exc:
                row["error"] = str(exc)[:160]
            chains.append(row)
        result = {"chains": chains, "checkedAt": int(now * 1000)}
        self._network_cache = result
        self._network_cache_at = now
        return result

    async def swap(
        self,
        *,
        chain: ChainSpec,
        wallets: Any,
        token_in: str,
        token_out: str,
        amount_in: str | None,
        amount_pct: float | None,
        slippage_pct: float | None,
        initiator: Initiator,
        session_key: str | None,
        note: str | None,
        wait: bool = False,
        amount_usd: float | None = None,
        client_order_id: str | None = None,
        expected_out_raw: int | None = None,
        min_out_raw: int | None = None,
        quote_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Swap from one or many wallets; one order per wallet.

        ``client_order_id`` makes the call idempotent: a repeat with the
        same key returns the orders it created the first time and creates
        nothing — a client that lost the reply to a timeout can ask again
        without a second swap going out.

        ``expected_out_raw`` / ``min_out_raw`` / ``quote_id`` are the quote
        the client confirmed. The engine always re-quotes, but the client's
        quote is the one the user agreed to: an execution that would deliver
        more than twice the slippage below it is refused (manual) or parked
        for approval (agent) instead of going out on a number nobody saw.
        """
        if not getattr(self.config, "enabled", True):
            raise TradingError("trading.disabled", "Trading is disabled in config")
        if initiator not in ("manual", "agent"):
            raise TradingError("trading.invalid", "initiator must be 'manual' or 'agent'")
        client_id = _client_order_id(client_order_id)
        if client_id is not None:
            existing = self.ledger.find_orders_by_client_id(client_id)
            if existing:
                return [self._order_dict(r) for r in existing]
        if not self.ensure_unlocked():
            raise TradingError("wallet.locked", "Wallet vault is locked")
        self.provider()  # raises early when the provider is not usable (no key)
        records = self._wallets_for(wallets)
        meta_in = await self.resolve_token(chain, token_in)
        meta_out = await self.resolve_token(chain, token_out)
        if meta_in.address == meta_out.address:
            raise TradingError("trading.invalid", "tokenIn and tokenOut are the same token")
        sizes = [s for s in (amount_in, amount_pct, amount_usd) if s is not None]
        if len(sizes) != 1:
            raise TradingError(
                "trading.invalid", "exactly one of amountIn, amountPct or amountUsd is required"
            )
        if amount_pct is not None and not (0 < float(amount_pct) <= 100):
            raise TradingError("trading.invalid", "amountPct must be between 0 and 100")
        slippage = slippage_pct if slippage_pct is not None else self.config.default_slippage_pct
        self._check_agent_slippage(initiator, slippage)
        client_quote = _client_quote_json(expected_out_raw, min_out_raw, quote_id)
        results: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            if index and BATCH_PAUSE_S > 0:
                await asyncio.sleep(BATCH_PAUSE_S)
            order_id = new_order_id()
            try:
                amount_raw = await self._amount_for(
                    chain, record, meta_in, amount_in, amount_pct, amount_usd
                )
                try:
                    row = self._new_order(
                        order_id,
                        chain,
                        record,
                        meta_in,
                        meta_out,
                        amount_raw,
                        slippage,
                        initiator,
                        session_key,
                        note,
                        client_order_id=client_id,
                        quote_json=client_quote,
                    )
                except sqlite3.IntegrityError:
                    # Two identical calls raced past the lookup above: the
                    # other one owns this wallet's order under this key.
                    if client_id is None:
                        raise
                    twin = next(
                        (
                            r
                            for r in self.ledger.find_orders_by_client_id(client_id)
                            if str(r["wallet"]) == record.key
                        ),
                        None,
                    )
                    if twin is None:
                        raise
                    order_id = str(twin["order_id"])
                    results.append(self.get_order(order_id))
                    continue
                await self._process_order(row, wait=wait)
            except Exception as exc:  # one wallet failing never blocks the batch
                error = _err(exc)
                if self.ledger.get_order(order_id) is None:
                    self.ledger.insert_order(
                        {
                            "order_id": order_id,
                            "created_at": self._now(),
                            "updated_at": self._now(),
                            "chain_id": chain.chain_id,
                            "wallet": record.key,
                            "token_in": meta_in.address,
                            "token_out": meta_out.address,
                            "amount_raw": "0",
                            "amount_human": amount_in or "0",
                            "status": "failed",
                            "reason": f"{error.code}: {error}",
                            "initiator": initiator,
                            "session_key": session_key,
                            "note": note,
                            "slippage_pct": slippage,
                            "client_order_id": client_id,
                        }
                    )
                    log.warning("trading.order_failed", order=order_id, error=str(error))
                    await self._emit("trading.order.finished", {"order": self.get_order(order_id)})
                else:
                    await self._fail_order(order_id, error)
            results.append(self.get_order(order_id))
            await self._emit("trading.changed", {"reason": "order", "orderId": order_id})
        return results

    async def _amount_for(
        self,
        chain: ChainSpec,
        record: WalletRecord,
        meta_in: TokenMeta,
        amount_in: str | None,
        amount_pct: float | None,
        amount_usd: float | None = None,
    ) -> int:
        if amount_in is not None:
            raw = to_raw(amount_in, meta_in.decimals)
        elif amount_usd is not None:
            raw = await self._raw_for_usd(chain, meta_in, float(amount_usd))
        else:
            balance = await self._balance_raw(chain, record, meta_in)
            if meta_in.native and float(amount_pct or 0) >= 100:
                # Keep a gas reserve when spending the whole native balance.
                if balance - NATIVE_GAS_RESERVE_WEI <= 0:
                    raise TradingError(
                        "trading.invalid",
                        f"balance {format_amount(balance, meta_in.decimals)} "
                        f"{meta_in.symbol or 'ETH'} is below the "
                        f"{format_amount(NATIVE_GAS_RESERVE_WEI, 18)} "
                        f"{meta_in.symbol or 'ETH'} gas reserve; nothing to sell",
                    )
                balance -= NATIVE_GAS_RESERVE_WEI
            raw = balance * int(float(amount_pct or 0) * 100) // 10_000
        if raw <= 0:
            raise TradingError("trading.invalid", "amount must be greater than zero")
        return raw

    async def _balance_raw(self, chain: ChainSpec, record: WalletRecord, meta: TokenMeta) -> int:
        evm = self.evm(chain)
        if meta.native:
            return await evm.get_balance(record.address)
        return await evm.erc20_balance_of(meta.address, record.address)

    def _new_order(
        self,
        order_id: str,
        chain: ChainSpec,
        record: WalletRecord,
        meta_in: TokenMeta,
        meta_out: TokenMeta,
        amount_raw: int,
        slippage: float | None,
        initiator: str,
        session_key: str | None,
        note: str | None,
        client_order_id: str | None = None,
        quote_json: str | None = None,
    ) -> dict[str, Any]:
        now = self._now()
        row = {
            "order_id": order_id,
            "created_at": now,
            "updated_at": now,
            "chain_id": chain.chain_id,
            "wallet": record.key,
            "token_in": meta_in.address,
            "token_out": meta_out.address,
            "amount_raw": str(amount_raw),
            "amount_human": format_amount(amount_raw, meta_in.decimals),
            "status": "quoted",
            "initiator": initiator,
            "session_key": session_key,
            "note": note,
            "slippage_pct": slippage,
            "provider": self.provider_id(),
            "client_order_id": client_order_id,
        }
        if quote_json is not None:
            row["quote_json"] = quote_json
        self.ledger.insert_order(row)
        return row

    async def _process_order(self, row: dict[str, Any], *, wait: bool) -> None:
        """Quote, apply guardrails, and execute or park the order."""
        chain = CHAINS[int(row["chain_id"])]
        record = self.vault.get(str(row["wallet"]))
        meta_in = await self.token_meta(chain, str(row["token_in"]))
        meta_out = await self.token_meta(chain, str(row["token_out"]))
        amount_raw = int(row["amount_raw"])
        balance = await self._balance_raw(chain, record, meta_in)
        if balance < amount_raw:
            raise TradingError(
                "trading.insufficient_balance",
                f"{record.label} holds {format_amount(balance, meta_in.decimals)} "
                f"{meta_in.symbol or 'tokens'}, needs "
                f"{format_amount(amount_raw, meta_in.decimals)}",
            )
        provider = self.provider(str(row.get("provider") or ""))
        quote = await self._priced(
            provider,
            chain=chain,
            swapper=record.address,
            meta_in=meta_in,
            meta_out=meta_out,
            amount_raw=amount_raw,
            slippage_pct=row.get("slippage_pct"),
            decision_origin=_origin(str(row["initiator"])),
        )
        value = await self._order_value_usd(
            chain, meta_in, amount_raw, meta_out, quote.amount_out_raw
        )
        self.ledger.update_order(
            row["order_id"],
            expected_out_raw=str(quote.amount_out_raw),
            min_out_raw=str(quote.min_out_raw),
            value_usd=value,
            price_impact_pct=quote.price_impact_pct,
            gas_usd=quote.gas_usd,
            slippage_pct=quote.slippage_pct
            if quote.slippage_pct is not None
            else row.get("slippage_pct"),
        )
        # The client confirmed a number; the engine's fresh quote must not
        # be far below it, or the user is executing a trade they never saw.
        client_expected = _client_expected_out(row)
        if client_expected is not None:
            slip = float(row.get("slippage_pct") or quote.slippage_pct or 0.5)
            floor = client_expected * (1 - 2 * slip / 100.0)
            if quote.amount_out_raw < floor:
                reason = (
                    "price moved more than twice the slippage since you confirmed "
                    f"(expected {format_amount(client_expected, meta_out.decimals)}, "
                    f"now {format_amount(quote.amount_out_raw, meta_out.decimals)} "
                    f"{meta_out.symbol or ''}".rstrip()
                    + "); quote again"
                )
                if row["initiator"] == "agent":
                    self.ledger.update_order(
                        row["order_id"],
                        status="awaiting_approval",
                        reason=reason,
                        expires_at=self._now() + int(self.config.approval_ttl_seconds),
                    )
                    await self._emit(
                        "trading.approval.requested", {"order": self.get_order(row["order_id"])}
                    )
                    return
                raise TradingError("trading.price_moved", reason)
        # The row is still ``quoted`` here, so its own value is not yet in
        # ``open_agent_value_usd``; the verdict adds it on top of everything
        # else in flight for this wallet.
        verdict = self._verdict(
            initiator=str(row["initiator"]),
            value=value,
            record=record,
            quote=quote,
            order_id=str(row["order_id"]),
        )
        if verdict.decision == "blocked_daily_cap":
            self.ledger.update_order(row["order_id"], status="rejected", reason=verdict.reason)
            await self._emit("trading.order.finished", {"order": self.get_order(row["order_id"])})
            return
        if verdict.decision == "needs_approval":
            ttl = int(self.config.approval_ttl_seconds)
            self.ledger.update_order(
                row["order_id"],
                status="awaiting_approval",
                reason=verdict.reason,
                expires_at=self._now() + ttl,
            )
            await self._emit(
                "trading.approval.requested", {"order": self.get_order(row["order_id"])}
            )
            return
        await self._execute(row["order_id"], quote, wait=wait)

    async def _execute(self, order_id: str, quote: ProviderQuote | None, *, wait: bool) -> None:
        row = self.ledger.get_order(order_id)
        assert row is not None
        kind = str(row.get("kind") or "swap")
        if kind == "send":
            await self._execute_send(row, wait=wait)
            return
        if kind == "revoke":
            await self._execute_revoke(row, wait=wait)
            return
        if kind in LP_ORDER_KINDS:
            await self._execute_lp(row, wait=wait)
            return
        chain = CHAINS[int(row["chain_id"])]
        record = self.vault.get(str(row["wallet"]))
        meta_in = await self.token_meta(chain, str(row["token_in"]))
        meta_out = await self.token_meta(chain, str(row["token_out"]))
        amount_raw = int(row["amount_raw"])
        key = self.vault.private_key(record.address)
        evm = self.evm(chain)
        provider = self.provider(str(row.get("provider") or ""))
        origin = _origin(str(row["initiator"]))
        # First contact with this wallet on this chain: book what it already
        # holds so the swap's cost basis and the balance cache start right.
        for token in {NATIVE_ADDRESS, meta_in.address, meta_out.address}:
            await self.syncer.ensure_opening(record, chain, token, evm)

        # Everything that signs for this wallet runs under its lock: a second
        # order for the same wallet waits, so no two sends read one nonce.
        async with self._wallet_lock(record.key):
            # 1. ERC-20 allowance for the provider's spender.
            if quote is None or not quote.fresh:
                quote = await self._priced(
                    provider,
                    chain=chain,
                    swapper=record.address,
                    meta_in=meta_in,
                    meta_out=meta_out,
                    amount_raw=amount_raw,
                    slippage_pct=row.get("slippage_pct"),
                    decision_origin=origin,
                )
            if not meta_in.native:
                approval_tx = await provider.approval_tx(quote, evm=evm, decision_origin=origin)
                if approval_tx is not None:
                    spender = self._check_approval_tx(
                        approval_tx,
                        token=meta_in.address,
                        amount_raw=amount_raw,
                        spenders=provider.trusted_spenders(chain, quote),
                    )
                    # The provider's calldata was only ever read for its
                    # spender; what gets signed is our own encoding of
                    # exactly the order's amount, whatever the provider
                    # asked for (see ``_check_approval_tx``).
                    approval_tx = {**approval_tx, "data": encode_approve(spender, amount_raw)}
                    tx_hash = await self._send(
                        chain,
                        record,
                        key,
                        approval_tx,
                        order_id=order_id,
                        hash_field="approval_tx_hash",
                        value_usd=row.get("value_usd"),
                    )
                    receipt = await evm.wait_for_sealed_receipt(
                        tx_hash, timeout_s=RECEIPT_TIMEOUT_S
                    )
                    if receipt is None:
                        raise TradingError(
                            "trading.tx_pending",
                            f"approval {tx_hash} was not mined within the wait window; "
                            "retry once it lands",
                        )
                    if not receipt_succeeded(receipt):
                        raise TradingError(
                            "trading.tx_failed", f"approval transaction failed ({tx_hash})"
                        )
                    await self._record_gas(
                        chain,
                        record,
                        receipt,
                        kind="approval",
                        order_id=order_id,
                        initiator=str(row["initiator"]),
                    )
                    await self._await_allowance(
                        evm,
                        token=meta_in.address,
                        owner=record.address,
                        spenders=provider.trusted_spenders(chain, quote),
                        amount_raw=amount_raw,
                    )

            # 2. Fresh quote (the approval may have taken a while).
            if not quote.fresh:
                quote = await self._priced(
                    provider,
                    chain=chain,
                    swapper=record.address,
                    meta_in=meta_in,
                    meta_out=meta_out,
                    amount_raw=amount_raw,
                    slippage_pct=row.get("slippage_pct"),
                    decision_origin=origin,
                )
            if not await self._enforce_floor(row, quote):
                return
            value = row.get("value_usd")
            if value is None:
                # Parked without a price and approved later: price it now so
                # the daily cap counts it rather than adding zero.
                value = await self._order_value_usd(
                    chain, meta_in, amount_raw, meta_out, quote.amount_out_raw
                )
            self.ledger.update_order(
                order_id,
                expected_out_raw=str(quote.amount_out_raw),
                min_out_raw=str(quote.min_out_raw),
                price_impact_pct=quote.price_impact_pct,
                gas_usd=quote.gas_usd,
                value_usd=value,
            )

            # 3. Calldata, sign, broadcast. A provider whose quote went
            # stale re-fetches inside ``build``; that re-quote is judged
            # by the same floor as the one before it, and the row keeps
            # the numbers that are actually about to be signed.
            fetched_before = quote.fetched_at
            tx = await provider.build(
                quote,
                deadline=int(self._now()) + 600,
                sign_permit=lambda permit: self._sign_permit(permit, key),
                decision_origin=origin,
            )
            if quote.fetched_at != fetched_before:
                if not await self._enforce_floor(row, quote):
                    return
                self.ledger.update_order(
                    order_id,
                    expected_out_raw=str(quote.amount_out_raw),
                    min_out_raw=str(quote.min_out_raw),
                    price_impact_pct=quote.price_impact_pct,
                    gas_usd=quote.gas_usd,
                )
            validate_transaction(tx)
            self._check_swap_tx(
                tx,
                chain=chain,
                record=record,
                meta_in=meta_in,
                amount_raw=amount_raw,
                provider=provider.id,
                targets=provider.trusted_targets(chain, quote),
            )
            pre_in = await self._balance_raw(chain, record, meta_in)
            pre_out = await self._balance_raw(chain, record, meta_out)
            pre_native = await evm.get_balance(record.address)
            # ``_send`` records the hash and flips the row to ``submitted``
            # *before* the broadcast; nothing is written here.
            tx_hash = await self._send(chain, record, key, tx, order_id=order_id, value_usd=value)
        await self._emit("trading.changed", {"reason": "order", "orderId": order_id})
        await self._watch(
            order_id, tx_hash, {"in": pre_in, "out": pre_out, "native": pre_native}, wait=wait
        )

    async def _enforce_floor(self, row: dict[str, Any], quote: ProviderQuote) -> bool:
        """Refuse or park an order whose fresh quote fell too far below the judged one.

        The guardrail judged a price; if the market has moved past twice the
        slippage since, that judgement is stale. An agent order goes back to
        the human (``False``: the caller stops); a manual order is refused so
        the user re-quotes with open eyes. ``True`` means carry on.
        """
        if not row.get("expected_out_raw"):
            return True
        order_id = str(row["order_id"])
        expected = int(row["expected_out_raw"])
        slip = float(row.get("slippage_pct") or quote.slippage_pct or 0.5)
        floor = expected * (1 - 2 * slip / 100.0)
        if quote.amount_out_raw >= floor:
            return True
        if row["initiator"] == "agent":
            self.ledger.update_order(
                order_id,
                status="awaiting_approval",
                reason="price moved since approval; please re-approve",
                expected_out_raw=str(quote.amount_out_raw),
                min_out_raw=str(quote.min_out_raw),
                expires_at=self._now() + int(self.config.approval_ttl_seconds),
            )
            await self._emit("trading.approval.requested", {"order": self.get_order(order_id)})
            return False
        raise TradingError(
            "trading.price_moved",
            "price moved more than twice the slippage since the quote; quote again",
        )

    async def _watch(self, order_id: str, tx_hash: str, pre: dict[str, int], *, wait: bool) -> None:
        """Settle the order from its receipt, now or in the background."""
        if wait:
            await self._confirm(order_id, tx_hash, pre)
            return
        loop = asyncio.get_running_loop()
        task = loop.create_task(
            self._confirm(order_id, tx_hash, pre), name=f"trading-confirm-{order_id}"
        )
        self._confirm_tasks.add(task)
        task.add_done_callback(self._confirm_tasks.discard)

    async def _execute_send(self, row: dict[str, Any], *, wait: bool) -> None:
        """Sign and broadcast one leg of a send: ETH by value, an ERC-20 by ``transfer``."""
        order_id = str(row["order_id"])
        chain = CHAINS[int(row["chain_id"])]
        record = self.vault.get(str(row["wallet"]))
        meta = await self.token_meta(chain, str(row["token_in"]))
        amount_raw = int(row["amount_raw"])
        recipient = checksum_address(str(row["recipient"]))
        key = self.vault.private_key(record.address)
        evm = self.evm(chain)
        for token in {NATIVE_ADDRESS, meta.address}:
            await self.syncer.ensure_opening(record, chain, token, evm)
        async with self._wallet_lock(record.key):
            balance = await self._balance_raw(chain, record, meta)
            if balance < amount_raw:
                raise TradingError(
                    "trading.insufficient_balance",
                    f"{record.label} holds {format_amount(balance, meta.decimals)} "
                    f"{meta.symbol or 'tokens'}, needs "
                    f"{format_amount(amount_raw, meta.decimals)}",
                )
            if meta.native:
                tx: dict[str, Any] = {
                    "from": record.address,
                    "to": recipient,
                    "data": "0x",
                    "value": amount_raw,
                    "chainId": chain.chain_id,
                }
            else:
                tx = {
                    "from": record.address,
                    "to": checksum_address(meta.address),
                    "data": encode_transfer(recipient, amount_raw),
                    "value": "0",
                    "chainId": chain.chain_id,
                }
            pre_native = await evm.get_balance(record.address)
            tx_hash = await self._send(
                chain,
                record,
                key,
                tx,
                plain=meta.native,
                order_id=order_id,
                value_usd=row.get("value_usd"),
            )
        await self._emit("trading.changed", {"reason": "order", "orderId": order_id})
        await self._watch(
            order_id, tx_hash, {"in": balance, "out": 0, "native": pre_native}, wait=wait
        )

    async def _execute_revoke(self, row: dict[str, Any], *, wait: bool) -> None:
        """``approve(spender, 0)`` on the token; the allowance is gone once it mines."""
        order_id = str(row["order_id"])
        chain = CHAINS[int(row["chain_id"])]
        record = self.vault.get(str(row["wallet"]))
        meta = await self.token_meta(chain, str(row["token_in"]))
        spender = str(row["recipient"])
        key = self.vault.private_key(record.address)
        evm = self.evm(chain)
        async with self._wallet_lock(record.key):
            tx = {
                "from": record.address,
                "to": checksum_address(meta.address),
                "data": encode_approve(spender, 0),
                "value": "0",
                "chainId": chain.chain_id,
            }
            pre_native = await evm.get_balance(record.address)
            tx_hash = await self._send(
                chain, record, key, tx, order_id=order_id, value_usd=row.get("value_usd")
            )
        await self._emit("trading.changed", {"reason": "order", "orderId": order_id})
        await self._watch(order_id, tx_hash, {"in": 0, "out": 0, "native": pre_native}, wait=wait)

    @staticmethod
    def _tx_value(tx: dict[str, Any]) -> int:
        raw = tx.get("value")
        return int(str(raw), 0) if raw not in (None, "") else 0

    def _check_swap_tx(
        self,
        tx: dict[str, Any],
        *,
        chain: ChainSpec,
        record: WalletRecord,
        meta_in: TokenMeta,
        amount_raw: int,
        provider: str,
        targets: frozenset[str],
    ) -> None:
        """What a provider's swap transaction must satisfy before it is signed.

        The provider is data, not authority: its calldata is opaque, but the
        envelope is not. The sender is this wallet, the chain is the order's,
        the native value is exactly the order's amount (zero for an ERC-20
        sale) — a transaction asking for more ETH than the order says spends
        more than the user approved — and the target is one of the contracts
        pinned for this provider on this chain (``targets``, lowercase). A
        native sell has no approval to anchor the target to, so this is the
        only thing standing between a tampered response and ETH sent to an
        attacker; an empty ``targets`` refuses every swap.
        """
        if str(tx.get("from") or "").lower() != record.key:
            raise TradingError("trading.tx_failed", "swap transaction is not from this wallet")
        tx_chain = tx.get("chainId")
        if tx_chain not in (None, "") and int(str(tx_chain), 0) != chain.chain_id:
            raise TradingError(
                "trading.tx_failed",
                f"swap transaction is for chain {tx_chain}, order is on {chain.chain_id}",
            )
        expected_value = amount_raw if meta_in.native else 0
        value = self._tx_value(tx)
        if value != expected_value:
            raise TradingError(
                "trading.tx_failed",
                f"swap transaction carries {value} wei, order allows {expected_value}",
            )
        to = str(tx.get("to") or "").lower()
        if to == record.key or to == meta_in.address.lower():
            raise TradingError("trading.tx_failed", "swap transaction has an implausible 'to'")
        if not targets or to not in targets:
            raise TradingError(
                "trading.tx_failed",
                f"swap target {to or '(none)'} is not a contract this desk trusts for "
                f"{provider_label(provider)} on {chain.name}"
                + ("" if targets else " (no swap contract is pinned for this chain)"),
            )

    @staticmethod
    def _check_approval_tx(
        tx: dict[str, Any], *, token: str, amount_raw: int, spenders: frozenset[str]
    ) -> str:
        """An approval is signed only when it says exactly what we meant.

        ``approve(spender, amount)`` on the token being sold, to a spender the
        provider is known to use, for no more than the order needs. Anything
        else — another token, another spender, native value attached, an
        unlimited allowance — is refused unsigned. Returns the spender: the
        caller re-encodes the approval for exactly the order's amount, so
        the provider's calldata never reaches the signer as it came.
        """
        if str(tx.get("to") or "").lower() != token.lower():
            raise TradingError("trading.tx_failed", "approval is not on the token being sold")
        if TradingService._tx_value(tx) != 0:
            raise TradingError("trading.tx_failed", "approval transaction carries native value")
        data = str(tx.get("data") or "")
        if not data.startswith("0x095ea7b3") or len(data) != 2 + 8 + 64 + 64:
            raise TradingError(
                "trading.tx_failed", "approval calldata is not approve(address,uint256)"
            )
        spender = "0x" + data[10 + 24 : 10 + 64]
        amount = int(data[10 + 64 :], 16)
        if spender.lower() not in {s.lower() for s in spenders}:
            raise TradingError("trading.tx_failed", f"approval spender {spender} is not trusted")
        if amount > amount_raw:
            raise TradingError(
                "trading.tx_failed", f"approval amount {amount} exceeds the order's {amount_raw}"
            )
        return spender.lower()

    async def _send(
        self,
        chain: ChainSpec,
        record: WalletRecord,
        key: bytes,
        tx: dict[str, Any],
        *,
        plain: bool = False,
        order_id: str | None = None,
        hash_field: str = "tx_hash",
        value_usd: float | None = None,
    ) -> str:
        """Simulate, price, sign, record and broadcast ``tx`` from ``record``.

        ``plain`` admits empty calldata: a native send carries value and
        nothing else, which for a provider's swap would mean it forgot the
        swap.

        Gas is this desk's decision, not the provider's: its fee fields are
        ignored outright, its gas limit is taken only under the chain's cap
        (never clamped — a limit above the cap is refused), and the worst
        case cost in dollars must stay under the larger of
        ``GAS_CEILING_USD`` and ``GAS_CEILING_SHARE`` of ``value_usd``.

        With ``order_id`` the signed transaction's hash is written to the
        order (``hash_field``: the swap leg flips the row to ``submitted``,
        the approval leg only records its hash) *before* the broadcast. A
        node that then refuses it is not believed outright — a load-balanced
        endpoint can say no on one node after another accepted — so the
        swap leg records the refusal as its reason and returns the hash for
        the confirm loop to watch (``REJECTED_GIVE_UP_S``); the approval leg
        raises, since nothing downstream can proceed without it.
        """
        evm = self.evm(chain)
        to = str(tx.get("to") or "")
        data = str(tx.get("data") or "0x")
        if not to or (data in ("", "0x") and not plain):
            raise TradingError("trading.tx_failed", "transaction from the provider is incomplete")
        # eth-account refuses to sign a ``to`` that is not EIP-55 checksummed,
        # and providers disagree about case: Uniswap answers checksummed, the
        # aggregator lowercase, and our own token addresses are normalised to
        # lowercase. Re-checksum here, once, for every provider and both legs
        # (approval and swap) — the alternative is a signer error at the very
        # last step, after the approval has already been mined.
        try:
            to = checksum_address(to)
        except ValueError as exc:
            raise TradingError(
                "trading.tx_failed", f"transaction 'to' is not an address: {to}"
            ) from exc
        value = int(str(tx.get("value") or "0"), 0) if tx.get("value") else 0
        base_tx: dict[str, Any] = {"from": record.address, "to": to, "data": data, "value": value}
        native = await evm.get_balance(record.address)
        if native < value:
            raise TradingError(
                "trading.insufficient_balance", "not enough native balance for value"
            )
        try:
            await evm.simulate(base_tx)
        except EvmRpcError as exc:
            raise TradingError("trading.tx_failed", f"simulation reverted: {exc}") from exc
        gas_cap = int(chain.max_gas_limit)
        gas_limit = int(str(tx.get("gasLimit") or "0"), 0) if tx.get("gasLimit") else 0
        if gas_limit > gas_cap:
            raise TradingError(
                "trading.tx_failed",
                f"gas limit {gas_limit} exceeds cap {gas_cap} on {chain.name}",
            )
        if gas_limit <= 0:
            estimate = await evm.estimate_gas(base_tx)
            if estimate > gas_cap:
                raise TradingError(
                    "trading.tx_failed",
                    f"gas estimate {estimate} exceeds cap {gas_cap} on {chain.name}",
                )
            gas_limit = min(int(estimate * 1.2), gas_cap)
        # The provider's own maxFeePerGas / maxPriorityFeePerGas are ignored:
        # the node's fee history, under the chain's caps, is the only source.
        max_fee, max_priority = await evm.fee_data()
        gas_cost_wei = gas_limit * max_fee
        if native < value + gas_cost_wei:
            raise TradingError(
                "trading.insufficient_balance",
                f"not enough {chain.native_symbol} for gas "
                f"(need ~{format_amount(value + gas_cost_wei, 18)})",
            )
        gas_cost_usd = await self._gas_usd(chain, gas_cost_wei)
        if gas_cost_usd is not None:
            ceiling = max(GAS_CEILING_USD, GAS_CEILING_SHARE * float(value_usd or 0.0))
            if gas_cost_usd > ceiling:
                raise TradingError(
                    "trading.gas_too_high",
                    f"gas could cost up to ${gas_cost_usd:.2f}, above the ${ceiling:.2f} "
                    f"ceiling for this order ({gas_limit} gas at "
                    f"{format_amount(max_fee, 9)} gwei)",
                    details={
                        "gasUsd": gas_cost_usd,
                        "ceilingUsd": ceiling,
                        "gasLimit": gas_limit,
                        "maxFeePerGas": str(max_fee),
                    },
                )
        nonce = await evm.nonce(record.address)
        full_tx = {
            "chainId": chain.chain_id,
            "nonce": nonce,
            "to": to,
            "value": value,
            "data": data,
            "gas": gas_limit,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": max_priority,
            "type": 2,
        }
        raw = self._sign_tx(full_tx, key)
        local_hash = _local_tx_hash(raw)
        # Record before send: from here on the ledger knows a transaction
        # with this hash may exist, whatever happens to the process or the
        # node in the next few milliseconds.
        if order_id is not None:
            if hash_field == "tx_hash":
                self.ledger.update_order(
                    order_id, status="submitted", tx_hash=local_hash, reason=None
                )
            else:
                self.ledger.update_order(order_id, **{hash_field: local_hash})
        try:
            sent = await evm.send_raw_transaction(raw)
        except (EvmRpcError, EvmTransportError) as exc:
            if order_id is not None:
                self.ledger.update_order(order_id, reason=f"{BROADCAST_REJECTED} {exc}")
            if order_id is None or hash_field != "tx_hash":
                raise TradingError("trading.tx_failed", f"{BROADCAST_REJECTED} {exc}") from exc
            log.warning("trading.broadcast_rejected", order=order_id, tx=local_hash, error=str(exc))
            return local_hash
        if sent and sent != local_hash:
            # A node answers with keccak(raw) by definition; anything else is
            # a node (or a test double) that indexes the transaction under
            # its own name, and that is the name the receipt lives under.
            log.warning("trading.tx_hash_mismatch", local=local_hash, node=sent, order=order_id)
            if order_id is not None:
                self.ledger.update_order(order_id, **{hash_field: sent})
            return sent
        return local_hash

    async def _record_gas(
        self,
        chain: ChainSpec,
        record: WalletRecord,
        receipt: dict[str, Any] | None,
        *,
        kind: str,
        order_id: str | None,
        initiator: str = "manual",
    ) -> float | None:
        if not receipt:
            return None
        gas_wei = receipt_gas_wei(receipt)
        eth_price = await self.prices.price(chain, NATIVE_ADDRESS)
        gas_usd = float(to_human(gas_wei, 18)) * eth_price if eth_price is not None else None
        tx_hash = str(receipt.get("transactionHash") or "").lower() or None
        if kind == "approval":
            self.ledger.insert_entry(
                ts=self._now(),
                chain_id=chain.chain_id,
                wallet=record.key,
                kind="approval",
                tx_hash=tx_hash,
                log_index=0,
                gas_usd=gas_usd,
                initiator=initiator,
                order_id=order_id,
            )
        return gas_usd

    async def _confirm(
        self,
        order_id: str,
        tx_hash: str,
        pre: dict[str, int] | None,
        *,
        timeout_s: float = RECEIPT_TIMEOUT_S,
    ) -> None:
        """Settle a submitted order from its receipt.

        ``pre`` is the balance snapshot taken before the send; ``None`` when
        the order is being recovered after a restart, in which case the legs
        come from the receipt's logs and the order's own amount. No receipt
        within the window leaves the order ``submitted``: a transaction that
        has not mined yet is not a failed one, and ``tick`` keeps looking.
        """
        row = self.ledger.get_order(order_id)
        if row is None or row["status"] != "submitted":
            return
        chain = CHAINS[int(row["chain_id"])]
        record = self.vault.get(str(row["wallet"]))
        meta_in = await self.token_meta(chain, str(row["token_in"]))
        meta_out = await self.token_meta(chain, str(row["token_out"]))
        evm = self.evm(chain)
        try:
            receipt = await evm.wait_for_sealed_receipt(tx_hash, timeout_s=timeout_s)
        except (EvmRpcError, EvmTransportError) as exc:
            receipt = None
            log.warning("trading.receipt_error", order=order_id, error=str(exc))
        if receipt is None:
            age = self._now() - float(row["created_at"])
            reason = str(row.get("reason") or "")
            rejected = reason.startswith(BROADCAST_REJECTED)
            # A broadcast the node refused is watched for minutes, not hours:
            # if it was secretly accepted it mines in the next block or two.
            give_up = REJECTED_GIVE_UP_S if rejected else SUBMITTED_GIVE_UP_S
            if age > give_up:
                why = reason.removeprefix(BROADCAST_REJECTED).strip()
                self.ledger.update_order(
                    order_id,
                    expect_status="submitted",
                    status="failed",
                    reason=(
                        f"transaction never mined ({why})"
                        if rejected
                        else "transaction never mined (dropped or replaced)"
                    ),
                )
                self._wake(order_id)
                await self._emit("trading.order.finished", {"order": self.get_order(order_id)})
            elif not rejected:
                # The refusal is kept as the reason: it is what decides the
                # shorter give-up above, and what the user should see.
                self.ledger.update_order(order_id, reason="waiting for the transaction to be mined")
            return
        if pre is None:
            pre = {"in": 0, "out": 0, "native": 0}
        # Only one settlement of a given order may book entries: the original
        # confirm task and a recovery pass can both hold a receipt for it.
        if order_id in self._settling:
            return
        self._settling.add(order_id)
        try:
            kind = str(row.get("kind") or "swap")
            if kind == "send":
                await self._settle_send(
                    order_id, row, tx_hash, receipt, pre, chain, record, meta_in
                )
            elif kind == "revoke":
                await self._settle_revoke(order_id, row, tx_hash, receipt, chain, record, meta_in)
            elif kind in LP_ORDER_KINDS:
                await self._settle_lp(order_id, row, tx_hash, receipt, pre, chain, record)
            else:
                await self._settle(
                    order_id, row, tx_hash, receipt, pre, chain, record, meta_in, meta_out
                )
        finally:
            self._settling.discard(order_id)

    async def _gas_usd(self, chain: ChainSpec, gas_wei: int) -> float | None:
        eth_price = await self.prices.price(chain, NATIVE_ADDRESS)
        return float(to_human(gas_wei, 18)) * eth_price if eth_price is not None else None

    def _spend_for(
        self, row: dict[str, Any], record: WalletRecord
    ) -> tuple[str, float, str] | None:
        """What a settled order adds to today's cap: an agent order's value; nothing else."""
        if row["initiator"] == "agent" and row.get("value_usd"):
            return (record.key, float(row["value_usd"]), local_day(self._now()))
        return None

    async def _settle_reverted(
        self,
        order_id: str,
        row: dict[str, Any],
        tx_hash: str,
        gas_wei: int,
        gas_usd: float | None,
        chain: ChainSpec,
        record: WalletRecord,
        *,
        note: str,
    ) -> None:
        self.ledger.update_order(
            order_id,
            status="failed",
            reason="transaction reverted on-chain",
            gas_wei=str(gas_wei),
        )
        self.ledger.insert_entry(
            ts=self._now(),
            chain_id=chain.chain_id,
            wallet=record.key,
            kind="gas",
            tx_hash=tx_hash,
            log_index=0,
            gas_usd=gas_usd,
            initiator=str(row["initiator"]),
            order_id=order_id,
            note=note,
        )
        self._wake(order_id)
        await self._emit("trading.order.finished", {"order": self.get_order(order_id)})

    async def _settle_send(
        self,
        order_id: str,
        row: dict[str, Any],
        tx_hash: str,
        receipt: dict[str, Any],
        pre: dict[str, int],
        chain: ChainSpec,
        record: WalletRecord,
        meta: TokenMeta,
    ) -> None:
        """Book a mined send as a withdrawal and bring the balance cache along.

        The amount is the order's own: a transfer moves exactly what it says
        or reverts, so no balance diff is needed — which also means a lagging
        node (``_native_after``) cannot mis-book it. The cache is set from
        the pre-send snapshot for the same reason.
        """
        evm = self.evm(chain)
        gas_wei = receipt_gas_wei(receipt)
        gas_usd = await self._gas_usd(chain, gas_wei)
        if not receipt_succeeded(receipt):
            await self._settle_reverted(
                order_id, row, tx_hash, gas_wei, gas_usd, chain, record, note="reverted send"
            )
            return
        amount = int(row["amount_raw"])
        recipient = str(row["recipient"])
        log_index = 0
        if not meta.native:
            for t in receipt_transfers(receipt):
                if t.token == meta.address and t.sender == record.key and t.recipient == recipient:
                    log_index = t.log_index
                    break
        block_number = int(str(receipt.get("blockNumber") or "0x0"), 16)
        ts = await evm.block_timestamp(block_number) if block_number else None
        await self.syncer._book_withdraw(
            record,
            chain,
            meta.address,
            amount,
            float(ts or self._now()),
            tx_hash=tx_hash,
            log_index=log_index,
            note=row.get("note") or f"sent to {checksum_address(recipient)}",
            initiator=str(row["initiator"]),
            order_id=order_id,
            session_key=row.get("session_key"),
            gas_usd=gas_usd,
        )
        if pre.get("native"):
            spent_native = amount + gas_wei if meta.native else gas_wei
            post_native = max(0, pre["native"] - spent_native)
        else:
            post_native = await self._native_after(evm, record.address, receipt)
        self.ledger.set_balance(chain.chain_id, record.key, NATIVE_ADDRESS, post_native)
        if not meta.native:
            post_token = max(0, pre["in"] - amount) if pre.get("in") else None
            if post_token is None:
                post_token = await self._balance_raw(chain, record, meta)
            self.ledger.set_balance(chain.chain_id, record.key, meta.address, post_token)
        self.ledger.settle_order(
            order_id,
            expect_status="submitted",
            spend=self._spend_for(row, record),
            status="confirmed",
            reason=None,
            spent_in_raw=str(amount),
            gas_wei=str(gas_wei),
        )
        self._wake(order_id)
        await self._emit("trading.order.finished", {"order": self.get_order(order_id)})
        await self._emit("trading.changed", {"reason": "order", "orderId": order_id})

    async def _settle_revoke(
        self,
        order_id: str,
        row: dict[str, Any],
        tx_hash: str,
        receipt: dict[str, Any],
        chain: ChainSpec,
        record: WalletRecord,
        meta: TokenMeta,
    ) -> None:
        gas_wei = receipt_gas_wei(receipt)
        gas_usd = await self._gas_usd(chain, gas_wei)
        if not receipt_succeeded(receipt):
            await self._settle_reverted(
                order_id, row, tx_hash, gas_wei, gas_usd, chain, record, note="reverted revoke"
            )
            return
        spender = str(row["recipient"])
        self.ledger.insert_entry(
            ts=self._now(),
            chain_id=chain.chain_id,
            wallet=record.key,
            kind="approval",
            tx_hash=tx_hash,
            log_index=0,
            token_in=meta.address,
            amount_in_raw=0,
            gas_usd=gas_usd,
            initiator=str(row["initiator"]),
            order_id=order_id,
            session_key=row.get("session_key"),
            note=row.get("note")
            or f"revoked {spender_label(spender) or checksum_address(spender)}",
        )
        self.ledger.delete_allowance(chain.chain_id, record.key, meta.address, spender)
        self.ledger.update_order(
            order_id,
            expect_status="submitted",
            status="confirmed",
            reason=None,
            gas_wei=str(gas_wei),
        )
        self._wake(order_id)
        await self._emit("trading.order.finished", {"order": self.get_order(order_id)})
        await self._emit("trading.changed", {"reason": "order", "orderId": order_id})

    @staticmethod
    async def _await_allowance(
        evm: EvmClient,
        *,
        token: str,
        owner: str,
        spenders: frozenset[str],
        amount_raw: int,
    ) -> None:
        """Wait until the endpoint can see the allowance we just mined.

        The approval's receipt proves it is on chain; it does not prove that
        the node answering the *next* ``eth_call`` has applied it. Measured
        on Base: the swap simulated as "ERC20: transfer amount exceeds
        allowance" against an allowance the same endpoint reported as
        present a moment later. Simulation happens before the send, so the
        cost of not waiting is a failed order rather than burnt gas — but it
        is a failure with no cause the user could act on.

        Never fatal: if the allowance stays invisible the swap goes on to
        simulate and fails there, with the chain's own reason attached.
        """
        for attempt in range(CHAIN_CATCHUP_ATTEMPTS):
            if attempt:
                await asyncio.sleep(CHAIN_CATCHUP_POLL_S)
            for spender in spenders:
                try:
                    if await evm.erc20_allowance(token, owner, spender) >= amount_raw:
                        return
                except (EvmRpcError, EvmTransportError):
                    continue
        log.warning("trading.allowance_not_visible", token=token, spenders=sorted(spenders))

    @staticmethod
    async def _native_after(
        evm: EvmClient, address: str, receipt: dict[str, Any], *, pre: int | None = None
    ) -> int:
        """The sender's native balance once the node has actually seen the swap.

        A swap into the gas coin has no Transfer log to fall back on — the
        router unwraps WETH and forwards ETH internally — so whatever this
        returns is booked verbatim. Behind a load balancer that is a trap:
        the receipt comes from a node that has the block while the balance
        comes from one that does not, and dRPC answers the stale read
        *without an error*, block tag or no block tag. Measured on Base on
        2026-09-19 across two live swaps: ``receivedOut`` came back as the
        gas cost alone, because post and pre were the same number.

        So the read is pinned to the receipt's own block **and** checked:
        the sender of a mined transaction has paid for its gas, so a balance
        equal to ``pre`` is proof the node is behind rather than proof that
        nothing arrived. That case is retried rather than believed.
        """
        block = str(receipt.get("blockNumber") or "")
        tags = [tag for tag in (block, "latest") if tag]
        latest = pre if pre is not None else 0
        for attempt in range(CHAIN_CATCHUP_ATTEMPTS):
            if attempt:
                await asyncio.sleep(CHAIN_CATCHUP_POLL_S)
            for tag in tags:
                try:
                    latest = await evm.get_balance(address, tag)
                except (EvmRpcError, EvmTransportError):
                    continue
                if pre is None or latest != pre:
                    return latest
            log.debug("trading.balance_lagging", address=address, block=block)
        return latest

    async def _balance_after(
        self, chain: ChainSpec, record: WalletRecord, meta: TokenMeta, *, fallback: int
    ) -> int:
        """A post-receipt balance read that retries and then settles for ``fallback``.

        Used only once the transaction is mined: the receipt's logs override
        an ERC-20 leg anyway, so a node that keeps refusing costs accuracy
        of the balance cache, not the booking — and the next sync fixes it.
        """
        for attempt in range(CHAIN_CATCHUP_ATTEMPTS):
            if attempt:
                await asyncio.sleep(CHAIN_CATCHUP_POLL_S)
            try:
                return await self._balance_raw(chain, record, meta)
            except (EvmRpcError, EvmTransportError) as exc:
                log.debug("trading.balance_read_failed", token=meta.address, error=str(exc))
        log.warning("trading.balance_read_gave_up", token=meta.address, wallet=record.address)
        return fallback

    async def _native_received(
        self,
        evm: EvmClient,
        order_id: str,
        row: dict[str, Any],
        record: WalletRecord,
        receipt: dict[str, Any],
        *,
        gas_wei: int,
        received: int,
        post_native: int,
        pre_known: bool,
    ) -> tuple[int, int]:
        """How much of the gas coin a mined swap delivered, when the diff is not believable.

        The balance diff is trusted when the pre-send snapshot exists and the
        diff clears the order's ``min_out_raw`` — a swap that mined without
        reverting delivered at least that. Otherwise (a recovery after a
        restart has no snapshot; a lagging node makes the diff come out as
        the gas alone) the balance is read pinned to the receipt's block and
        the one before it, which no ``latest`` lag can distort. If even that
        is unavailable or still short, the order's own expected amount is
        booked with a warning rather than a zero that would look like theft.
        """
        min_out = int(row.get("min_out_raw") or 0)
        if pre_known and received > 0 and received >= min_out:
            return received, post_native
        block_number = int(str(receipt.get("blockNumber") or "0x0"), 16)
        if block_number > 0:
            try:
                before = await evm.get_balance(record.address, hex(block_number - 1))
                after = await evm.get_balance(record.address, hex(block_number))
            except (EvmRpcError, EvmTransportError) as exc:
                log.warning("trading.pinned_balance_failed", order=order_id, error=str(exc))
            else:
                pinned = (after - before) + gas_wei
                if pinned > 0 and pinned >= min_out:
                    return pinned, after
        fallback = int(row.get("expected_out_raw") or row.get("min_out_raw") or 0)
        if fallback <= 0:
            return max(received, 0), post_native
        log.warning(
            "trading.native_received_assumed",
            order=order_id,
            diff=received,
            booked=fallback,
        )
        return fallback, post_native

    async def _settle(
        self,
        order_id: str,
        row: dict[str, Any],
        tx_hash: str,
        receipt: dict[str, Any],
        pre: dict[str, int],
        chain: ChainSpec,
        record: WalletRecord,
        meta_in: TokenMeta,
        meta_out: TokenMeta,
    ) -> None:
        evm = self.evm(chain)
        gas_wei = receipt_gas_wei(receipt)
        eth_price = await self.prices.price(chain, NATIVE_ADDRESS)
        gas_usd = float(to_human(gas_wei, 18)) * eth_price if eth_price is not None else None
        if not receipt_succeeded(receipt):
            self.ledger.update_order(
                order_id,
                status="failed",
                reason="transaction reverted on-chain",
                gas_wei=str(gas_wei),
            )
            self.ledger.insert_entry(
                ts=self._now(),
                chain_id=chain.chain_id,
                wallet=record.key,
                kind="gas",
                tx_hash=tx_hash,
                log_index=0,
                gas_usd=gas_usd,
                initiator=str(row["initiator"]),
                order_id=order_id,
                note="reverted swap",
            )
            await self._emit("trading.order.finished", {"order": self.get_order(order_id)})
            return
        # Every read from here on is *after* the receipt: the swap is on
        # chain whatever the node says next, so a read that fails is retried
        # and then replaced by the best fallback, never allowed to raise —
        # raising here would strand a mined swap (see ``_fail_order``).
        post_in = await self._balance_after(chain, record, meta_in, fallback=pre["in"])
        post_out = await self._balance_after(chain, record, meta_out, fallback=pre["out"])
        post_native = await self._native_after(
            evm, record.address, receipt, pre=pre["native"] or None
        )
        transfers = receipt_transfers(receipt)
        # ERC-20 legs come from the receipt's Transfer logs: a load-balanced
        # RPC can still answer "latest" balances from a node that has not
        # seen the block yet, which would book the swap as received 0.
        spent = pre["in"] - post_in
        received = post_out - pre["out"]
        delivered = meta_out.address
        if not meta_in.native:
            left = sum(
                t.amount for t in transfers if t.token == meta_in.address and t.sender == record.key
            )
            if left > 0:
                spent = left
                post_in = pre["in"] - left
        if not meta_out.native:
            arrived = sum(
                t.amount
                for t in transfers
                if t.token == meta_out.address and t.recipient == record.key
            )
            if arrived > 0:
                received = arrived
                post_out = pre["out"] + arrived
        if meta_in.native:
            spent = (pre["native"] - post_native) - gas_wei if pre["native"] else 0
        if meta_out.native:
            received = (post_native - pre["native"]) + gas_wei if pre["native"] else 0
            # On an L2 the router may hand back WETH instead of unwrapping.
            weth = None
            try:
                weth = await self.weth_for(chain)
            except (EvmRpcError, EvmTransportError, httpx.HTTPError) as exc:
                log.warning("trading.weth_lookup_failed", order=order_id, error=str(exc))
            if weth and received <= 0:
                arrived = sum(
                    t.amount for t in transfers if t.token == weth and t.recipient == record.key
                )
                if arrived > 0:
                    delivered = weth
                    received = arrived
                    try:
                        meta_out = await self.token_meta(chain, weth)
                    except (EvmRpcError, EvmTransportError) as exc:
                        log.warning("trading.weth_meta_failed", order=order_id, error=str(exc))
                        meta_out = TokenMeta(chain.chain_id, weth, "WETH", "Wrapped Ether", 18)
            if delivered == meta_out.address and meta_out.native:
                received, post_native = await self._native_received(
                    evm,
                    order_id,
                    row,
                    record,
                    receipt,
                    gas_wei=gas_wei,
                    received=received,
                    post_native=post_native,
                    pre_known=bool(pre["native"]),
                )
        if spent <= 0:
            spent = int(row["amount_raw"])
        received = max(received, 0)
        block_number = int(str(receipt.get("blockNumber") or "0x0"), 16)
        ts = None
        if block_number:
            try:
                ts = await evm.block_timestamp(block_number)
            except (EvmRpcError, EvmTransportError) as exc:
                log.warning("trading.block_timestamp_failed", order=order_id, error=str(exc))
        await self.syncer._book_swap(
            record,
            chain,
            token_in=meta_in.address,
            amount_in=spent,
            token_out=delivered,
            amount_out=received,
            ts=float(ts or self._now()),
            tx_hash=tx_hash,
            log_index=0,
            initiator=str(row["initiator"]),
            gas_usd=gas_usd,
            order_id=order_id,
            session_key=row.get("session_key"),
            note=row.get("note"),
        )
        # Keep the balance cache consistent so the next sync does not book the
        # swap's native movement as a deposit/withdrawal.
        self.ledger.set_balance(chain.chain_id, record.key, NATIVE_ADDRESS, post_native)
        if not meta_in.native:
            self.ledger.set_balance(chain.chain_id, record.key, meta_in.address, post_in)
        if not meta_out.native:
            self.ledger.set_balance(chain.chain_id, record.key, meta_out.address, post_out)
        # A mined swap that delivered less than the quote's minimum should
        # have reverted; that it did not means the calldata's own floor was
        # lower than the one we were shown. The order is still confirmed —
        # the tokens moved — but it says so, loudly.
        reason: str | None = None
        min_out = int(row.get("min_out_raw") or 0)
        if delivered == str(row["token_out"]) and min_out > 0 and received < min_out:
            reason = f"short fill: received {received} below min {min_out}"
            log.warning(
                "trading.short_fill",
                order=order_id,
                received=received,
                min_out=min_out,
                token=delivered,
            )
        # Compare-and-set on the final flip, with the spend in the same
        # transaction: if anything else already settled this order, neither
        # lands; if the spend write fails, the flip does not either.
        self.ledger.settle_order(
            order_id,
            expect_status="submitted",
            spend=self._spend_for(row, record),
            status="confirmed",
            reason=reason,
            spent_in_raw=str(spent),
            received_out_raw=str(received),
            gas_wei=str(gas_wei),
            delivered_token=delivered,
        )
        self._wake(order_id)
        await self._emit("trading.order.finished", {"order": self.get_order(order_id)})
        await self._emit("trading.changed", {"reason": "order", "orderId": order_id})

    # ── approvals ──────────────────────────────────────────────────────

    def _wake(self, order_id: str) -> None:
        event = self._order_events.pop(order_id, None)
        if event is not None:
            event.set()

    def _pending_group(self, order_id: str) -> tuple[dict[str, Any], list[str]]:
        """The order and every order decided with it (its whole batch, when it has one)."""
        row = self.ledger.get_order(order_id)
        if row is None:
            raise TradingError("trading.invalid", f"no order {order_id}")
        if row["status"] != "awaiting_approval":
            raise TradingError("trading.invalid", f"order {order_id} is {row['status']}")
        batch_id = row.get("batch_id")
        if not batch_id:
            return row, [order_id]
        members = [
            str(o["order_id"])
            for o in self.ledger.batch_orders(str(batch_id))
            if o["status"] == "awaiting_approval"
        ]
        return row, members or [order_id]

    async def approve(self, order_id: str, *, wait: bool = False) -> dict[str, Any]:
        """Run an order the agent parked. Approving one leg of a multisend approves them all."""
        row, members = self._pending_group(order_id)
        if row.get("expires_at") and float(row["expires_at"]) <= self._now():
            for member in members:
                self.ledger.update_order(
                    member, expect_status="awaiting_approval", status="expired", reason="expired"
                )
            raise TradingError("trading.quote_expired", f"order {order_id} expired")
        # Compare-and-set: two approvals racing each other both passed the
        # status read above; only the one that flips the row may execute.
        claimed = [
            member
            for member in members
            if self.ledger.update_order(
                member, expect_status="awaiting_approval", status="approved", reason=None
            )
            is not None
        ]
        if not claimed:
            raise TradingError(
                "trading.invalid", f"order {order_id} is no longer awaiting approval"
            )
        await self._run_legs(claimed, wait=wait)
        await self._emit(
            "trading.changed",
            {"reason": "approval", "orderId": order_id, "batchId": row.get("batch_id")},
        )
        return self.get_order(order_id)

    async def reject(self, order_id: str, reason: str | None = None) -> dict[str, Any]:
        """Refuse a parked order; a multisend is refused whole."""
        row, members = self._pending_group(order_id)
        rejected = [
            member
            for member in members
            if self.ledger.update_order(
                member,
                expect_status="awaiting_approval",
                status="rejected",
                reason=f"user: {reason}" if reason else "user",
            )
            is not None
        ]
        if not rejected:
            raise TradingError(
                "trading.invalid", f"order {order_id} is no longer awaiting approval"
            )
        for member in rejected:
            self._wake(member)
            await self._emit("trading.order.finished", {"order": self.get_order(member)})
        await self._emit(
            "trading.changed",
            {"reason": "approval", "orderId": order_id, "batchId": row.get("batch_id")},
        )
        return self.get_order(order_id)

    async def wait_order(self, order_id: str, timeout_s: float = 60.0) -> dict[str, Any]:
        deadline = self._now() + max(0.0, min(float(timeout_s), 900.0))
        while True:
            order = self.get_order(order_id)
            # ``quoted`` is the row's first breath: the pipeline moves it on
            # (or fails it) within the same call, so it is not an answer yet.
            if order["status"] in ORDER_FINAL_STATUSES:
                return order
            remaining = deadline - self._now()
            if remaining <= 0:
                return order
            event = self._order_events.setdefault(order_id, asyncio.Event())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=min(remaining, 2.0))
            if event.is_set():
                self._order_events.pop(order_id, None)

    async def expire_orders(self) -> list[dict[str, Any]]:
        expired = self.ledger.expire_orders(self._now())
        out = []
        for row in expired:
            self._wake(str(row["order_id"]))
            order = self.get_order(str(row["order_id"]))
            out.append(order)
            await self._emit("trading.order.finished", {"order": order})
        if out:
            await self._emit("trading.changed", {"reason": "approval"})
        return out


# ── module singleton ───────────────────────────────────────────────────────

_service: TradingService | None = None


def get_trading_service(
    config: Any | None = None, *, broadcast: Broadcast | None = None
) -> TradingService:
    """The process-wide service, created from ``config`` on first use.

    ``config`` is the gateway config (anything with a ``trading`` attribute)
    or a bare ``TradingConfig``; ``broadcast`` is how events leave the engine
    (the gateway passes its WebSocket registry). Later calls may refresh
    both — a hot config write or a reconnected transport takes effect
    without rebuilding the ledger or re-unlocking the vault.
    """
    global _service
    if _service is None:
        if config is None:
            raise ValueError("get_trading_service needs a config on first use")
        _service = TradingService(config, broadcast=broadcast)
        return _service
    if config is not None:
        trading_cfg = getattr(config, "trading", config)
        _service.config = trading_cfg
        _service._gateway_config = config
    if broadcast is not None:
        _service._broadcast = broadcast
    return _service


def set_trading_service(service: TradingService | None) -> None:
    global _service
    _service = service


def reset_trading_service() -> None:
    set_trading_service(None)
