"""Uniswap V4 liquidity read-outs for LP cards (``application/vnd.agentos.lp+json``).

``docs/lp-cards.md`` is the contract: four payload kinds (``pool``, ``ranges``,
``position``, ``positions``), one envelope, amounts as decimal strings and USD
values that are ``null`` -- never ``0`` -- when no price is known.

Every chain read goes through the V4 library that ships with the bundled
``senior-unilp-manager`` skill (``scripts/unilp``: pure stdlib, exact TickMath,
launchpad registries, the tick-bitmap walk). It is loaded here under a private
package name rather than copied, so the skill and the engine can never disagree
about the math. What changes is who configures it: the RPC URL is the engine's
(``chains.rpc_url_for``) and is handed to the client explicitly, the skill's
dotenv loader is switched off, and its caches live under the engine's state
directory. Prices come from the engine's own lookup first and GeckoTerminal
second, through one process-wide :class:`PriceBook` that concurrent reads share.

The library does blocking HTTP, so every read runs in a worker thread; the
engine's async services (prices, the Blockscout indexer) are reached from that
thread through the event loop that started it.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import math
import os
import queue
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Coroutine, Iterable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import structlog

from agentos.paths import state_dir
from agentos.trading.chains import (
    BASE,
    CHAINS,
    NATIVE_ADDRESS,
    ROBINHOOD,
    ChainSpec,
    is_native,
    normalize_address,
    rpc_url_for,
)
from agentos.trading.service import TradingError

if TYPE_CHECKING:  # pragma: no cover
    from agentos.trading.service import TradingService

log = structlog.get_logger(__name__)

LP_MIME = "application/vnd.agentos.lp+json"
PAYLOAD_VERSION = 1
#: ``pool.topRanges`` length.
TOP_RANGES = 5
#: Bitmap words the tick walk reads before it centres a window on the current
#: price and reports the scan as truncated (the library's own default).
MAX_BITMAP_WORDS = 600
#: Live pools of a token are pre-ranked by active liquidity (cheap: one
#: multicall); only this many of the best are tick-walked to compare TVL.
MAX_POOLS_COMPARED = 4
#: Pages of the Blockscout holder index read per wallet (50 NFTs a page).
INDEXER_MAX_PAGES = 20
#: When a node refuses a full-history ``eth_getLogs``, position NFTs are
#: searched in windows walking back from the head, this many per JSON-RPC
#: batch, until every wallet is accounted for or the budget is spent.
LOG_WINDOWS_PER_BATCH = 10
LOG_SCAN_BUDGET_S = 30.0
#: A batch of windows that failed transiently is sent again this many times
#: before the walk falls back to the narrower span.
WALK_RETRIES = 2
#: Worker threads for multicall chunks (ownership checks, pool states, token
#: metadata, uncollected fees) and for the two Transfer-log queries.
POSITION_WORKERS = 6
#: Calls per multicall chunk; bigger reads are split and sent concurrently.
MULTICALL_CHUNK = 100
#: Rows a ``positions`` card lists; ``totals`` still cover every position.
#: A launch bot can hold a thousand positions, and one row is ~1.5 KB.
MAX_POSITIONS_LISTED = 50
#: A node that refused a full-history ``eth_getLogs`` is not asked again for
#: this long (per URL); the refusal itself can take seconds on a metered node.
#: Only an explicit *span* refusal is remembered -- never a timeout, and never
#: a result-size refusal ("more than 10000 results"), which is about one
#: filter, not the node: a narrower filter on the same node is still served.
FULL_RANGE_REFUSAL_TTL_S = 600.0
#: A full-history request that failed transiently is asked once more after this.
LOG_RETRY_DELAY_S = 1.0
#: A successful full-history answer is shared with identical requests this long.
FULL_RANGE_SHARE_TTL_S = 15.0
#: When it fails twice, the last this-many blocks are searched instead, in
#: chunks of ``RECENT_LOG_CHUNK`` sent concurrently, for at most the budget.
RECENT_LOG_WINDOW = 2_000_000
RECENT_LOG_CHUNK = 50_000
RECENT_LOG_WORKERS = 8
RECENT_LOG_BUDGET_S = 8.0
#: A node whose stated span limit is below this is not walked at all (Base's
#: public node: 2,000 blocks); above it the chunks shrink to the limit and the
#: window with them, so the walk stays at most ``RECENT_LOG_WINDOW //
#: RECENT_LOG_CHUNK`` requests.
RECENT_LOG_MIN_SPAN = 10_000
#: ``lp positions`` wall-clock budget: discovery stops early enough that the
#: whole command answers within it, flagged partial when it had to stop.
POSITIONS_BUDGET_S = 25.0
POSITIONS_BUDGET_MIN_S = 5.0
POSITIONS_BUDGET_MAX_S = 300.0
#: The slice of the budget kept for pricing, fees and the card once
#: discovery stops (a share of the budget, within these bounds).
POSITIONS_RESERVE_SHARE = 0.3
POSITIONS_RESERVE_MIN_S = 4.0
POSITIONS_RESERVE_MAX_S = 10.0
#: A chain still working this long after the budget answers with what it has.
POSITIONS_HARD_GRACE_S = 4.0
#: Discovery still busy (a verify wave or a log batch in flight) this long past
#: its deadline is cut off: what it verified so far is priced and listed.
DISCOVERY_GRACE_S = 1.0
#: At the hard deadline, what a chain found is turned into rows within this
#: (only the pools of positions found before their pools were read need a call).
POSITIONS_SALVAGE_S = 2.0
#: The log walk starts this long after the indexer unless the indexer is done.
INDEXER_HEAD_START_S = 2.0
#: Distinct tokens priced with one lookup; above it only the listed rows'
#: tokens (and the most-held others, up to this many) are looked up.
PRICE_LOOKUP_MAX = 60
#: A USD price found for an LP read is reused by every read in this process for
#: this long (GeckoTerminal's free tier allows ~30 calls a minute, and a burst
#: of reads asking for the same quote asset must cost one call, not one each).
PRICE_TTL_S = 60.0
#: A price source that did not answer (429, 5xx, timeout) is asked once more
#: after this pause -- or after the engine's hold on that miss, when longer
#: (``prices.UNAVAILABLE_HOLD_S``), up to ``PRICE_RETRY_MAX_WAIT_S``.
PRICE_RETRY_DELAY_S = 1.0
PRICE_RETRY_MAX_WAIT_S = 5.0
#: Per-request timeout of the GeckoTerminal fallback.
GECKO_TIMEOUT_S = 10.0
#: Quote assets worth $1.00: when no source prices one it is valued at par
#: (and the card says so) rather than left unpriced.
STABLE_SYMBOLS = frozenset({"USDC", "USDG", "USDT", "USDBC", "DAI", "USDS"})
#: Chain order for a read that names no chain.
DEFAULT_CHAINS: tuple[ChainSpec, ...] = (BASE, ROBINHOOD)

_UNILP_DIR = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "bundled"
    / "senior-unilp-manager"
    / "scripts"
    / "unilp"
)
_PACKAGE = "_agentos_unilp"
_MODULES = (
    "abi_codec",
    "abi_defs",
    "chains",
    "fmt",
    "hexutil",
    "launchers",
    "poolcache",
    "prices",
    "rpc",
    "simulate",
    "v4_actions",
    "v4_math",
    "v4_pool",
)
_load_lock = threading.Lock()
_loaded: Unilp | None = None
#: RPC URL -> (monotonic time a full-history ``eth_getLogs`` span was refused
#: there, the span limit the refusal stated or ``None``).
_full_range_refused: dict[str, tuple[float, int | None]] = {}
#: Full-history requests in flight, and answers shared for a few seconds, by
#: (URL, address, topics): see :func:`full_range_logs`.
_flights_lock = threading.Lock()
_in_flight: dict[tuple[str, str, str], Future[tuple[list[Any] | None, str, int | None]]] = {}
_shared_logs: dict[tuple[str, str, str], tuple[float, list[Any]]] = {}

PriceFn = Callable[[list[str]], dict[str, float | None]]
#: ``nft_ids(owner, on_page=..., deadline=...)``: an indexer's tokenIds for
#: ``owner``, each page handed to ``on_page`` as it arrives (optional).
NftIdsFn = Callable[..., list[int] | None]
PairTokensFn = Callable[[str], list[str]]


# ── the skill's library ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Unilp:
    """The skill's ``unilp`` modules, loaded once under a private package name."""

    abi: ModuleType
    abi_codec: ModuleType
    chains: ModuleType
    fmt: ModuleType
    hexutil: ModuleType
    launchers: ModuleType
    poolcache: ModuleType
    prices: ModuleType
    rpc: ModuleType
    simulate: ModuleType
    v4_actions: ModuleType
    v4_math: ModuleType
    v4_pool: ModuleType


def unilp() -> Unilp:
    """Load ``senior-unilp-manager/scripts/unilp`` (once) and return its modules.

    The modules import each other relatively, so the package can live under any
    name; a private one keeps it off ``sys.path`` and away from a script that
    imports ``unilp`` itself. Three hooks are rebound on this private copy only:
    the dotenv loader (the engine configures RPCs, not ``~/.agentos/.env``), the
    PoolKey cache root and the price cache file (both under the engine's state
    directory, which tests isolate).
    """
    global _loaded
    with _load_lock:
        if _loaded is not None:
            return _loaded
        init = _UNILP_DIR / "__init__.py"
        spec = importlib.util.spec_from_file_location(
            _PACKAGE, init, submodule_search_locations=[str(_UNILP_DIR)]
        )
        if spec is None or spec.loader is None or not init.is_file():
            raise RuntimeError(f"the Uniswap V4 library is missing from {_UNILP_DIR}")
        package = importlib.util.module_from_spec(spec)
        sys.modules[_PACKAGE] = package
        spec.loader.exec_module(package)
        mods = {name: importlib.import_module(f"{_PACKAGE}.{name}") for name in _MODULES}
        setattr(mods["chains"], "_env_loaded", True)
        setattr(mods["poolcache"], "state_root", lambda: state_dir("unilp"))
        setattr(mods["prices"], "_cache_path", lambda: state_dir("unilp", "prices.json"))
        _loaded = Unilp(abi=mods.pop("abi_defs"), **mods)
        return _loaded


# ── per-read environment ────────────────────────────────────────────────────


@dataclass
class PositionsProgress:
    """What ``lp positions`` has on one chain so far, published stage by stage.

    The worker thread cannot be interrupted, so a chain cut off at the hard
    deadline answers from here instead of with nothing: 970 verified
    positions were dropped whole when pricing ran a few seconds late.
    """

    #: The live search (its verified positions grow as discovery runs).
    search: Any | None = None
    #: Discovery is over and ``found`` is what it verified.
    found: list[dict[str, Any]] | None = None
    #: (position, pool state) per listable position, metadata read.
    jobs: list[tuple[dict[str, Any], dict[str, Any]]] | None = None
    #: Uncollected fees by tokenId, once read.
    fees: dict[int, tuple[int, int] | None] | None = None


@dataclass
class ChainEnv:
    """Everything one chain's read needs: the client, prices, and what it learnt."""

    spec: ChainSpec
    client: Any
    prices: PriceFn
    lib: Unilp = field(default_factory=unilp)
    nft_ids: NftIdsFn | None = None
    pair_tokens: PairTokensFn | None = None
    #: Vault wallets, lower-cased address -> label.
    vault: dict[str, str] = field(default_factory=dict)
    #: A second node for the full-history log request only (the chain's public
    #: default, when the engine is configured with another endpoint).
    log_client: Any | None = None
    #: The first block a wallet can hold anything at (its creation block, when
    #: the vault recorded one), lower-cased address -> block.
    first_blocks: dict[str, int] = field(default_factory=dict)
    block: int = 0
    fetched_at: str = ""
    partial: bool = False
    warnings: list[str] = field(default_factory=list)
    #: ``time.monotonic()`` by which discovery stops, and by which the whole
    #: read should be done; ``None`` is unbounded (the single-card reads).
    deadline: float | None = None
    budget_end: float | None = None
    budget_s: float | None = None
    #: Set once the bounded price lookup is done: later misses stay unpriced
    #: (valued at the pool's own price where a side is known) instead of
    #: each triggering one more lookup.
    prices_frozen: bool = False
    progress: PositionsProgress = field(default_factory=PositionsProgress)
    _metas: dict[str, dict[str, Any]] = field(default_factory=dict)
    _prices: dict[str, float | None] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def chain(self) -> dict[str, Any]:
        return dict(self.lib.chains.CHAINS[self.spec.key])

    def warn(self, message: str) -> None:
        with self._lock:
            if message not in self.warnings:
                self.warnings.append(message)

    def remaining(self, until: float | None = None) -> float | None:
        """Seconds left before ``until`` (default: the discovery deadline)."""
        end = self.deadline if until is None else until
        return None if end is None else end - time.monotonic()

    def expired(self) -> bool:
        left = self.remaining()
        return left is not None and left <= 0


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def start(env: ChainEnv) -> ChainEnv:
    """Read the head block once; everything after it is "as of" this block."""
    env.fetched_at = _now_iso()
    env.block = int(env.client.block_number())
    return env


def chain_json(spec: ChainSpec) -> dict[str, Any]:
    return {
        "id": spec.chain_id,
        "key": spec.key,
        "name": spec.name,
        "explorer": spec.explorer_url,
    }


def envelope(kind: str, env: ChainEnv | None, **extra: Any) -> dict[str, Any]:
    return {
        "version": PAYLOAD_VERSION,
        "kind": kind,
        "chain": chain_json(env.spec) if env is not None else None,
        "asOfBlock": env.block if env is not None else 0,
        "fetchedAt": env.fetched_at if env is not None else _now_iso(),
        "partialScan": bool(env.partial) if env is not None else False,
        "warnings": list(env.warnings) if env is not None else [],
        **extra,
    }


def finite(value: Any) -> float | None:
    """A JSON-safe number: ``None`` for missing, NaN or infinite."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _payload(value: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = _clean(value)
    return cleaned


def _clean(value: Any) -> Any:
    """Drop every non-finite float from a payload (JSON has no Infinity)."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, float):
        return finite(value)
    return value


def multicall(env: ChainEnv, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``client.multicall`` with big reads split into chunks sent concurrently.

    The library sends its chunks one after the other; a wallet with hundreds of
    positions needs thousands of calls, and a round trip per hundred adds up.
    """
    if len(calls) <= MULTICALL_CHUNK:
        results: list[dict[str, Any]] = env.client.multicall(calls)
        return results
    chunks = [calls[i : i + MULTICALL_CHUNK] for i in range(0, len(calls), MULTICALL_CHUNK)]
    with ThreadPoolExecutor(max_workers=POSITION_WORKERS) as pool:
        parts = list(pool.map(env.client.multicall, chunks))
    return [result for part in parts for result in part]


#: A passing condition: worth one retry, and never remembered.
_TRANSIENT_LOG_ERROR = re.compile(
    r"timed out|timeout|deadline|temporar|try again|retry|busy|overload|unavailable"
    r"|rate.?limit|too many requests|connection|reset by peer|http (429|5\d\d)\b",
    re.IGNORECASE,
)
#: A rule about one filter's answer, not the node: this query matched too many
#: logs. A narrower window of the same filter is still served, so it is never
#: remembered against the node.
_OVERSIZED_LOG_RESULT = re.compile(
    r"more than [\d,]+ (results|logs)|too many (results|logs)|response (size|too (big|large))"
    r"|max(imum)? (results|logs)|(result|log) (count|limit)",
    re.IGNORECASE,
)
#: A rule of the node's: the block span is too big, now and later.
_REFUSED_LOG_RANGE = re.compile(r"range|span|blocks|exceeds|limit", re.IGNORECASE)
#: The span a refusal states: "limited to a 2,000 range", "range over 100000
#: blocks", "exceeds max block span 10000".
_STATED_SPAN = re.compile(
    r"(?:limited to (?:a )?|(?:range|span) (?:of |over |is )?|over )(\d[\d,]{2,})"
    r"|(\d[\d,]{2,}) blocks?\b",
    re.IGNORECASE,
)


def log_failure_kind(error: object) -> str:
    """``"transient"``, ``"oversized"``, ``"refused"`` or ``"unknown"``.

    ``"oversized"``: this filter matched too many logs (a narrower window of it
    is served); ``"refused"``: the node's own rule on the block span.

    Read from the message, whatever the exception type: Robinhood Chain's node
    answers an overloaded request with a JSON-RPC *error* ("log query timed
    out"), which is no more a rule than a dropped connection is. ``error`` is
    an exception or a batch entry's ``error`` member (an object or a string).
    """
    message = _log_error_message(error)
    if _TRANSIENT_LOG_ERROR.search(message):
        return "transient"
    if _OVERSIZED_LOG_RESULT.search(message):
        return "oversized"
    if _REFUSED_LOG_RANGE.search(message):
        return "refused"
    return "unknown"


def _log_error_message(error: object) -> str:
    return str(error.get("message", error) if isinstance(error, dict) else error)


def stated_span(error: object) -> int | None:
    """The block span a span refusal says the node allows, when it says one."""
    match = _STATED_SPAN.search(_log_error_message(error))
    if match is None:
        return None
    span = int((match.group(1) or match.group(2)).replace(",", ""))
    return span or None


def full_range_logs(
    env: ChainEnv, address: str, topics: list[Any], *, recent_fallback: bool = False
) -> list[Any] | None:
    """One ``eth_getLogs`` over the chain's whole V4 history; ``None`` when refused.

    Asked of the chain's public node when the engine is configured with another
    endpoint: Robinhood Chain's serves the whole history in a second, while a
    metered endpoint refuses any span that wide -- and can take seconds of
    retries to say so. An explicit span refusal is remembered per URL for a
    while (the request is not sent again, the search still happens); a
    timeout or a node under load is asked once more after a second and never
    remembered, and neither is a result-size refusal, which is about this
    filter only. With ``recent_fallback`` any of the three falls back to the
    last ``RECENT_LOG_WINDOW`` blocks (see :func:`recent_logs`).

    Concurrent reads asking the same node the same question share one request
    (and its answer, for ``FULL_RANGE_SHARE_TTL_S``): nine desk turns reading
    one wallet sent nine identical full-history requests, and Robinhood
    Chain's node answered HTTP 429 to most of them.
    """
    client = env.log_client or env.client
    url = getattr(client, "url", None)
    refused = _full_range_refused.get(url) if isinstance(url, str) else None
    if refused is not None and time.monotonic() - refused[0] < FULL_RANGE_REFUSAL_TTL_S:
        # The node's span rule still stands: skip the doomed request, not the search.
        return _after_failure(env, client, address, topics, "refused", refused[1], recent_fallback)
    genesis = int(env.chain["logScan"].get("fromBlock", 0))
    request = {"address": address, "topics": topics, "fromBlock": hex(genesis), "toBlock": "latest"}
    if not isinstance(url, str):
        logs, kind, span = _full_range_request(env, client, url, request)
    else:
        key = (url, address.lower(), json.dumps(topics).lower())
        with _flights_lock:
            now = time.monotonic()
            for stale in [
                k for k, (at, _) in _shared_logs.items() if now - at >= FULL_RANGE_SHARE_TTL_S
            ]:
                _shared_logs.pop(stale, None)
            shared = _shared_logs.get(key)
            flight = _in_flight.get(key)
            leader = shared is None and flight is None
            if leader:
                flight = _in_flight[key] = Future()
        if shared is not None:
            return list(shared[1])
        assert flight is not None
        if leader:
            logs, kind, span = None, "unknown", None
            try:
                logs, kind, span = _full_range_request(env, client, url, request)
            finally:
                with _flights_lock:
                    _in_flight.pop(key, None)
                    if logs is not None:
                        _shared_logs[key] = (time.monotonic(), logs)
                flight.set_result((logs, kind, span))
        else:
            left = env.remaining()
            try:
                logs, kind, span = flight.result(timeout=None if left is None else max(left, 0.0))
            except TimeoutError:
                return None
    if logs is not None:
        return list(logs)
    return _after_failure(env, client, address, topics, kind, span, recent_fallback)


def _after_failure(
    env: ChainEnv,
    client: Any,
    address: str,
    topics: list[Any],
    kind: str,
    span: int | None,
    recent_fallback: bool,
) -> list[Any] | None:
    """What a full-history request that failed with ``kind`` leaves: recent logs, or ``None``."""
    if not recent_fallback or kind not in ("transient", "oversized", "refused"):
        return None
    if kind == "refused":
        if span is not None and span < RECENT_LOG_MIN_SPAN:
            # A walk in spans this narrow would take hundreds of requests.
            return None
        chunk = min(RECENT_LOG_CHUNK, span or RECENT_LOG_CHUNK)
        return recent_logs(env, client, address, topics, chunk=chunk, reason="was refused")
    if kind == "oversized":
        return recent_logs(env, client, address, topics, reason="matched too many logs")
    return recent_logs(env, client, address, topics)


def _full_range_request(
    env: ChainEnv, client: Any, url: str | None, request: dict[str, Any]
) -> tuple[list[Any] | None, str, int | None]:
    """The request itself, retried once on a transient failure.

    Returns (logs, failure kind, the span a span refusal stated). Only a span
    refusal is remembered against ``url``.
    """
    for attempt in (1, 2):
        try:
            logs = client.get_logs(request)
        except Exception as exc:  # noqa: BLE001 - a refused range is an expected answer
            kind = log_failure_kind(exc)
            log.debug(
                "trading.lp_full_range_refused",
                chain=env.spec.key,
                error=str(exc),
                kind=kind,
                attempt=attempt,
            )
            if kind == "refused":
                span = stated_span(exc)
                if isinstance(url, str):
                    _full_range_refused[url] = (time.monotonic(), span)
                return None, kind, span
            if kind != "transient":
                return None, kind, None
            left = env.remaining()
            if attempt == 1 and (left is None or left > LOG_RETRY_DELAY_S + 1.0):
                time.sleep(LOG_RETRY_DELAY_S)
                continue
            return None, kind, None
        return (logs if isinstance(logs, list) else None), "ok", None
    return None, "transient", None


def recent_logs(
    env: ChainEnv,
    client: Any,
    address: str,
    topics: list[Any],
    *,
    chunk: int = RECENT_LOG_CHUNK,
    reason: str = "timed out twice",
) -> list[Any]:
    """The last ``RECENT_LOG_WINDOW`` blocks of logs, chunked and concurrent, time-boxed.

    What is left when the node would not serve the whole history (it timed
    out twice, refused the span, or the filter matched too many logs):
    anything older is not searched, and the card says so (partial). A node
    that only serves a narrower ``chunk`` gets as many requests over a
    proportionally shorter window.
    """
    genesis = int(env.chain["logScan"].get("fromBlock", 0))
    head = env.block or int(env.client.block_number())
    window = chunk * max(1, RECENT_LOG_WINDOW // RECENT_LOG_CHUNK)
    floor = max(genesis, head - window + 1)
    windows = []
    top = head
    while top >= floor:
        low = max(floor, top - chunk + 1)
        windows.append((low, top))
        top = low - 1
    budget = RECENT_LOG_BUDGET_S
    left = env.remaining()
    if left is not None:
        budget = max(1.0, min(budget, left))

    def one(window: tuple[int, int]) -> list[Any]:
        low, high = window
        found = client.get_logs(
            {"address": address, "topics": topics, "fromBlock": hex(low), "toBlock": hex(high)}
        )
        return found if isinstance(found, list) else []

    out: list[Any] = []
    missed = 0
    pool = ThreadPoolExecutor(max_workers=RECENT_LOG_WORKERS)
    futures = [pool.submit(one, w) for w in windows]
    end = time.monotonic() + budget
    for future in futures:
        try:
            out.extend(future.result(timeout=max(0.0, end - time.monotonic())))
        except Exception:  # noqa: BLE001 - a window that failed or ran out of time
            missed += 1
    pool.shutdown(wait=False, cancel_futures=True)
    if missed:
        what = (
            f"{len(windows) - missed} of {len(windows)} windows of the last "
            f"{head - floor + 1:,} blocks"
        )
    else:
        what = f"the last {head - floor + 1:,} blocks"
    # An older pool (or position) may be missing from what follows: say so.
    env.partial = True
    env.warn(f"{env.spec.name}: the full log history {reason}; only {what} were searched")
    return out


# ── pools a token is known to have (poolcache, by token) ────────────────────


def _token_pools_path(env: ChainEnv) -> Path:
    return state_dir("unilp", "pools", f"{env.spec.key}-tokens.json")


def _read_token_pools(env: ChainEnv) -> dict[str, list[str]]:
    try:
        data = json.loads(_token_pools_path(env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def cached_token_pools(env: ChainEnv, token: str) -> list[dict[str, Any]]:
    """Pools confirmed for ``token`` on an earlier read, with their PoolKeys.

    The skill's poolcache maps poolId -> PoolKey; this index adds token ->
    poolIds, so a pool only an ``Initialize`` log revealed (BONER's 0.9 % one)
    is still found on a read whose log request failed.
    """
    ids = _read_token_pools(env).get(token.lower())
    out = []
    for pool_id in ids if isinstance(ids, list) else []:
        key = env.lib.poolcache.lookup(env.chain, str(pool_id))
        if key:
            out.append({"poolId": str(pool_id).lower(), "poolKey": dict(key)})
    return out


def remember_token_pools(env: ChainEnv, token: str, pools: list[dict[str, Any]]) -> None:
    """Add ``pools`` (confirmed live) to ``token``'s entry. Never raises."""
    fresh = [str(p["poolId"]).lower() for p in pools]
    if not fresh:
        return
    try:
        current = _read_token_pools(env)
        known = [str(i).lower() for i in current.get(token.lower()) or []]
        merged = list(dict.fromkeys([*known, *fresh]))
        if merged == known:
            return
        current[token.lower()] = merged
        path = _token_pools_path(env)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tokens-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(current, handle, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        return


# ── tokens, prices, amounts ─────────────────────────────────────────────────


def token_meta(env: ChainEnv, address: str | None) -> dict[str, Any]:
    """symbol / decimals / totalSupply of one currency (cached per read)."""
    return token_metas(env, [address or NATIVE_ADDRESS])[(address or NATIVE_ADDRESS).lower()]


def token_metas(env: ChainEnv, addresses: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Metadata for many currencies: every uncached one in a single multicall."""
    lib = env.lib
    keys = list(dict.fromkeys((a or NATIVE_ADDRESS).lower() for a in addresses))
    missing = [k for k in keys if k not in env._metas and k != NATIVE_ADDRESS]
    if NATIVE_ADDRESS in keys and NATIVE_ADDRESS not in env._metas:
        native = env.chain["nativeCurrency"]
        env._metas[NATIVE_ADDRESS] = {
            "address": NATIVE_ADDRESS,
            "symbol": native["symbol"],
            "decimals": int(native["decimals"]),
            "totalSupply": None,
            "isNative": True,
        }
    if missing:
        fields = ("symbol", "decimals", "totalSupply")
        checksummed = [lib.hexutil.checksum_address(k) for k in missing]
        results = multicall(
            env,
            [
                {"address": address, "abi": lib.abi.ERC20_ABI, "functionName": fn}
                for address in checksummed
                for fn in fields
            ],
        )
        for i, address in enumerate(checksummed):
            symbol, decimals, supply = results[3 * i : 3 * i + 3]
            env._metas[address.lower()] = {
                "address": address,
                "symbol": (
                    str(symbol["result"])
                    if symbol["status"] == "success" and symbol["result"]
                    else lib.fmt.short(address)
                ),
                "decimals": int(decimals["result"]) if decimals["status"] == "success" else 18,
                "totalSupply": int(supply["result"]) if supply["status"] == "success" else None,
                "isNative": False,
            }
    return {k: env._metas[k] for k in keys}


def usd_prices(
    env: ChainEnv, addresses: Iterable[str], *, timeout: float | None = None
) -> dict[str, float | None]:
    """USD price per lower-cased address; unknown stays ``None``. One lookup per token.

    ``timeout`` bounds the lookup (what has not answered by then is unpriced);
    once ``env.prices_frozen`` is set nothing new is looked up at all.
    """
    wanted = [a.lower() for a in addresses if a]
    missing = [a for a in dict.fromkeys(wanted) if a not in env._prices]
    if missing and not env.prices_frozen:
        try:
            found = _within(timeout, lambda: env.prices(missing))
        except Exception as exc:  # noqa: BLE001 - a price is decoration, never fatal
            log.debug("trading.lp_price_failed", chain=env.spec.key, error=str(exc))
            found = {}
        for address in missing:
            env._prices[address] = finite(found.get(address))
    stables = stable_quotes(env)
    for address in dict.fromkeys(wanted):
        if env._prices.get(address) is None and address in stables:
            env._prices[address] = 1.0
            env.warn(f"{stables[address]} priced at $1.00 (provider had no quote)")
    return {a: env._prices.get(a) for a in wanted}


def stable_quotes(env: ChainEnv) -> dict[str, str]:
    """The chain's dollar-pegged quote assets, lower-cased address -> symbol."""
    known = env.chain.get("knownQuotes") or {}
    out = {str(a).lower(): str(s) for a, s in known.items() if str(s).upper() in STABLE_SYMBOLS}
    if env.spec.usdc:
        out.setdefault(env.spec.usdc.lower(), "USDC")
    return out


def _within[T](timeout: float | None, fn: Callable[[], T]) -> T:
    """``fn()``, abandoned (``TimeoutError``) after ``timeout`` seconds; ``None`` waits."""
    if timeout is None:
        return fn()
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            box["error"] = exc

    worker = threading.Thread(target=run, name="lp-timeboxed", daemon=True)
    worker.start()
    worker.join(max(timeout, 0.0))
    if worker.is_alive():
        raise TimeoutError(f"no answer within {timeout:.1f} s")
    if "error" in box:
        raise box["error"]
    value: T = box["value"]
    return value


def amount_json(raw: int, decimals: int, price: float | None) -> dict[str, Any]:
    raw = int(raw)
    human = raw / 10**decimals
    if raw == 0:
        usd: float | None = 0.0
    else:
        usd = None if price is None else finite(human * price)
    return {"raw": str(raw), "human": _format_units(raw, decimals), "usd": usd}


def _format_units(raw: int, decimals: int) -> str:
    return str(unilp().hexutil.format_units(int(raw), int(decimals)))


def _sum_usd(*values: float | None) -> float | None:
    """A total that is only known when every part is known."""
    if any(v is None for v in values):
        return None
    return finite(sum(v for v in values if v is not None))


# ── orientation: which side is "base" ───────────────────────────────────────


@dataclass
class Side:
    """The pool seen from ``base``: prices are quote per one base token."""

    pool_key: dict[str, Any]
    base: dict[str, Any]
    quote: dict[str, Any]
    base_is_currency1: bool
    base_usd: float | None
    quote_usd: float | None
    meta0: dict[str, Any]
    meta1: dict[str, Any]

    @property
    def has_supply(self) -> bool:
        supply = self.base.get("totalSupply")
        return supply is not None and supply > 0

    def math_kwargs(self) -> dict[str, Any]:
        return {
            "token_is_currency1": self.base_is_currency1,
            "decimals0": self.meta0["decimals"],
            "decimals1": self.meta1["decimals"],
            "total_supply": self.base.get("totalSupply") or 0,
            "token_decimals": self.base["decimals"],
            "quote_usd": self.quote_usd,
        }

    def price_at(self, tick: int) -> float | None:
        return finite(
            unilp().v4_math.token_price_in_quote_at_tick(
                tick, self.base_is_currency1, self.meta0["decimals"], self.meta1["decimals"]
            )
        )

    def mcap_at(self, tick: int) -> float | None:
        if not self.has_supply or self.quote_usd is None:
            return None
        return finite(unilp().v4_math.mcap_at_tick(tick, **self.math_kwargs()))

    def split(self, amount0: int, amount1: int) -> tuple[int, int]:
        """(base, quote) from (currency0, currency1)."""
        return (amount1, amount0) if self.base_is_currency1 else (amount0, amount1)

    def spot(self, tick: int, liquid: bool = True) -> tuple[float | None, float | None]:
        """(priceUsd, mcapUsd) of base at ``tick``: pool-implied, else the price lookup.

        A pool with no active liquidity has a price anyone can move for free, so
        the lookup wins there whenever it has one.
        """
        in_quote = self.price_at(tick)
        implied = (
            finite(in_quote * self.quote_usd)
            if in_quote is not None and self.quote_usd is not None
            else None
        )
        if not liquid and self.base_usd is not None:
            implied = None
        price = implied if implied is not None else self.base_usd
        if implied is not None:
            mcap = self.mcap_at(tick)
        elif price is not None and self.has_supply:
            mcap = finite(self.base["totalSupply"] / 10 ** self.base["decimals"] * price)
        else:
            mcap = None
        return price, mcap

    def token_json(self, which: str) -> dict[str, Any]:
        meta = self.base if which == "base" else self.quote
        price = self.base_usd if which == "base" else self.quote_usd
        return {
            "address": meta["address"],
            "symbol": meta["symbol"],
            "decimals": meta["decimals"],
            "priceUsd": price,
        }

    def range_json(self, tick_lower: int, tick_upper: int) -> dict[str, Any]:
        at = (tick_lower, tick_upper)
        prices = sorted(p for p in (self.price_at(t) for t in at) if p is not None)
        mcaps = sorted(m for m in (self.mcap_at(t) for t in at) if m is not None)
        return {
            "tickLower": tick_lower,
            "tickUpper": tick_upper,
            "priceLower": prices[0] if len(prices) == 2 else None,
            "priceUpper": prices[1] if len(prices) == 2 else None,
            "mcapLower": mcaps[0] if len(mcaps) == 2 else None,
            "mcapUpper": mcaps[1] if len(mcaps) == 2 else None,
        }

    def status(self, tick: int, tick_lower: int, tick_upper: int, liquidity: int) -> str:
        """``Status`` in quote-per-base terms (the library speaks in ticks)."""
        if liquidity <= 0:
            return "closed"
        by_tick = unilp().v4_math.range_status(tick, tick_lower, tick_upper)
        if by_tick == "in-range":
            return "in-range"
        # A higher tick is a higher currency1-per-currency0 price: "above" in
        # ticks is above-range for a currency0 base and below-range otherwise.
        above = by_tick == "above"
        if self.base_is_currency1:
            above = not above
        return "above-range" if above else "below-range"

    def band_label(self, tick_lower: int, tick_upper: int) -> str | None:
        if not self.has_supply or self.quote_usd is None:
            return None
        lib = unilp()
        band = lib.v4_math.mcap_band_for_range(
            tick_lower,
            tick_upper,
            tick_spacing=int(self.pool_key["tickSpacing"]),
            **self.math_kwargs(),
        )
        return str(lib.fmt.fmt_band(band))


def make_side(env: ChainEnv, pool_key: dict[str, Any], base: str | None = None) -> Side:
    """Orient a pool. ``base`` defaults to the side that is not a known quote asset."""
    meta0 = token_meta(env, pool_key["currency0"])
    meta1 = token_meta(env, pool_key["currency1"])
    if base is None:
        known = env.chain.get("knownQuotes") or {}
        quote0 = bool(known.get(meta0["address"].lower()))
        quote1 = bool(known.get(meta1["address"].lower()))
        # currency0 unless it is the only known quote asset in the pair.
        base = meta1["address"] if quote0 and not quote1 else meta0["address"]
    base_is_1 = meta1["address"].lower() == base.lower()
    prices = usd_prices(env, [meta0["address"], meta1["address"]])
    base_meta, quote_meta = (meta1, meta0) if base_is_1 else (meta0, meta1)
    return Side(
        pool_key=pool_key,
        base=base_meta,
        quote=quote_meta,
        base_is_currency1=base_is_1,
        base_usd=prices.get(base_meta["address"].lower()),
        quote_usd=prices.get(quote_meta["address"].lower()),
        meta0=meta0,
        meta1=meta1,
    )


def is_liquid(state: dict[str, Any]) -> bool:
    return int(state.get("activeLiquidity") or 0) > 0


def with_implied_base_price(side: Side, state: dict[str, Any], *, liquid_only: bool = True) -> Side:
    """Fill a missing base price from the pool (quote price × pool price).

    By default only from a pool with active liquidity: an empty pool's price is
    whatever the last swap (or the initializer) left behind. A position is the
    exception (``liquid_only=False``): its amounts are computed at that same
    pool price, so valuing them at it is at least consistent.
    """
    if side.base_usd is None and (is_liquid(state) or not liquid_only):
        in_quote = side.price_at(state["tick"])
        if in_quote is not None and side.quote_usd is not None:
            side.base_usd = finite(in_quote * side.quote_usd)
    return side


# ── pools ───────────────────────────────────────────────────────────────────


def _is_pool_id(value: str) -> bool:
    text = value.strip().lower()
    if not text.startswith("0x") or len(text) != 66:
        return False
    try:
        int(text[2:], 16)
    except ValueError:
        return False
    return True


#: The dynamic-fee flag a V4 PoolKey carries in ``fee`` (``--fee dynamic``).
DYNAMIC_FEE = 0x800000
#: The highest static LP fee V4 accepts: 100 %, in hundredths of a bip.
MAX_LP_FEE = 1_000_000
#: Tick spacings tried for a named fee tier, besides the fee/100 and fee/50
#: that launchpads and the v3 convention derive from the fee itself.
_FEE_SPACINGS = (1, 10, 60, 200)


def _fee_spacings(fee: int) -> tuple[int, ...]:
    derived = {fee // d for d in (100, 50) if fee % d == 0}
    return tuple(sorted(s for s in {*_FEE_SPACINGS, *derived} if 1 <= s <= 32_767))


def parse_fee(value: Any) -> int | None:
    """A fee tier as a V4 PoolKey stores it (hundredths of a bip); ``None`` if not given.

    Takes what a person or an agent writes: ``0.05``, ``0.05%``, ``0.3``, ``1``
    (percent) or ``500``, ``3000`` (V4's own units -- any whole number of 100
    or more), and ``dynamic``. Below 100 a bare number is a percent, so
    ``1`` is the 1 % tier and ``100`` the 0.01 % one.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise TradingError("trading.invalid", "feePct must be a fee tier such as 0.05 or 0.3%")
    text = str(value).strip().lower()
    if text == "dynamic":
        return DYNAMIC_FEE
    pct = text.endswith("%")
    try:
        number = float(text.removesuffix("%").strip())
    except ValueError:
        number = math.nan
    if not math.isfinite(number) or number < 0:
        raise TradingError(
            "trading.invalid",
            f"not a fee tier: {value!r} (use a percent such as 0.05, 0.3%, 1 or V4 units "
            "such as 500)",
        )
    if not pct and number >= 100 and number.is_integer():
        pips = float(number)
    else:
        pips = number * 10_000
    fee = round(pips)
    if abs(pips - fee) > 1e-6 or fee > MAX_LP_FEE:
        raise TradingError(
            "trading.invalid",
            f"not a fee tier: {value!r} (V4 fees are whole hundredths of a bip, at most 100%)",
        )
    return fee


def fee_label(fee: int) -> str:
    """``500`` -> ``0.05%``; the dynamic flag -> ``dynamic`` (as ``fee_pct`` prints it)."""
    return fee_pct({"fee": fee})


def split_pair(target: str, quote: str | None) -> tuple[str, str | None]:
    """``TOKEN/QUOTE`` -> ``(TOKEN, QUOTE)``; anything else passes through with ``quote``."""
    text = (target or "").strip()
    if "/" not in text:
        return text, quote
    base, _, other = text.partition("/")
    base, other = base.strip(), other.strip()
    if not base or not other or "/" in other:
        raise TradingError("trading.invalid", f"not a pair: {target!r} (write TOKEN/QUOTE)")
    if quote and quote.strip().lower() != other.lower():
        raise TradingError(
            "trading.invalid", f"{target} names {other} as the quote, but --quote says {quote}"
        )
    return base, other


def _fee_matches(state: dict[str, Any], fee: int) -> bool:
    actual = int(state["poolKey"]["fee"])
    if fee == DYNAMIC_FEE:
        return bool(actual & DYNAMIC_FEE)
    return actual == fee


def select_fee(
    env: ChainEnv,
    live: list[dict[str, Any]],
    fee: int | None,
    *,
    token: str,
    quote: str | None,
) -> list[dict[str, Any]]:
    """The live pools on fee tier ``fee`` (all of them when ``fee`` is ``None``).

    None on that tier is ``trading.lp.not_found`` naming the tiers that do
    exist for the pair, so the caller can pick one instead of guessing again.
    """
    if fee is None:
        return live
    matching = [p for p in live if _fee_matches(p, fee)]
    if matching:
        return matching
    tiers = sorted(
        {int(p["poolKey"]["fee"]) for p in live},
        key=lambda f: (bool(f & DYNAMIC_FEE), f),
    )
    labels = list(dict.fromkeys(fee_label(f) for f in tiers))
    try:
        names = token_metas(env, [token, *([quote] if quote else [])])
        what = str(names[token.lower()]["symbol"] or token)
        if quote:
            what += f"/{names[quote.lower()]['symbol'] or quote}"
    except Exception:  # noqa: BLE001 - the message only; the address will do
        what = f"{token}/{quote}" if quote else token
    exist = f"tiers that exist: {', '.join(labels)}" if labels else "no V4 pool at all"
    raise TradingError(
        "trading.lp.not_found",
        f"no {fee_label(fee)} Uniswap V4 pool for {what} on {env.spec.name}; {exist}",
        details={
            "token": token,
            "quote": quote,
            "chainId": env.spec.chain_id,
            "feePct": fee_label(fee),
            "tiers": labels,
        },
    )


def check_pool_fee(state: dict[str, Any], fee: int | None) -> None:
    """A named poolId must be on the named tier, if one was named."""
    if fee is not None and not _fee_matches(state, fee):
        raise TradingError(
            "trading.invalid",
            f"pool {state['poolId']} is a {fee_pct(state['poolKey'])} pool, not {fee_label(fee)}",
        )


def pool_states(env: ChainEnv, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """slot0 + liquidity for every candidate in one multicall; uninitialised ones dropped."""
    if not candidates:
        return []
    state_view = env.chain["stateView"]
    abi = env.lib.abi.STATE_VIEW_ABI
    results = multicall(
        env,
        [
            call
            for c in candidates
            for call in (
                {
                    "address": state_view,
                    "abi": abi,
                    "functionName": "getSlot0",
                    "args": [c["poolId"]],
                },
                {
                    "address": state_view,
                    "abi": abi,
                    "functionName": "getLiquidity",
                    "args": [c["poolId"]],
                },
            )
        ],
    )
    out = []
    for i, candidate in enumerate(candidates):
        slot0, liquidity = results[2 * i], results[2 * i + 1]
        if slot0["status"] != "success" or int(slot0["result"][0]) == 0:
            continue
        sqrt_price, tick, protocol_fee, lp_fee = slot0["result"]
        out.append(
            {
                "poolId": candidate["poolId"],
                "poolKey": candidate["poolKey"],
                "sqrtPriceX96": int(sqrt_price),
                "tick": int(tick),
                "protocolFee": int(protocol_fee),
                "lpFee": int(lp_fee),
                "activeLiquidity": (
                    int(liquidity["result"]) if liquidity["status"] == "success" else None
                ),
            }
        )
    return out


def _launcher_candidates(
    env: ChainEnv, token: str, quote: str | None
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Pools a launchpad registry (or a labelled launcher hook) says hold ``token``."""
    launchers = env.lib.launchers
    chain = env.chain
    found = launchers.resolve_launcher(env.client, chain, token)
    if found:
        numeraire = found.get("numeraire") or quote
        return (
            launchers.derive_pool_candidates(chain, token, hook=found["hook"], numeraire=numeraire),
            found,
        )
    candidates: list[dict[str, Any]] = []
    for entry, hook in launchers.labelled_hooks(chain):
        for candidate in launchers.derive_pool_candidates(chain, token, hook=hook, numeraire=quote):
            candidates.append({**candidate, "_entry": entry})
    return candidates, None


def initialized_pools(env: ChainEnv, token: str, quote: str | None = None) -> list[dict[str, Any]]:
    """Every pool the PoolManager initialised pairing ``token`` with a known quote.

    One ``Initialize`` log request, filtered on both currencies, over the whole
    history -- whatever the fee, tick spacing or hook. That is the only route
    to a pool opened at an unconventional fee (0.9 %, 1.002 % ...), which the
    fee-tier and launchpad routes cannot guess. A node that will not serve the
    whole history is searched over its recent blocks instead; one that only
    serves spans narrower than ``RECENT_LOG_MIN_SPAN`` (Base's public node) is
    not searched, and the other routes still run there.
    """
    lib = env.lib
    quotes = [quote] if quote else list(env.chain.get("knownQuotes") or {})
    mine = lib.hexutil.pad(token.lower(), size=32)
    theirs = [lib.hexutil.pad(q.lower(), size=32) for q in quotes if q.lower() != token.lower()]
    if not theirs:
        return []
    initialize = lib.abi.TOPIC_INITIALIZE
    # Token as currency0, then as currency1: two narrow requests at once. One
    # filter with both sides "any of" also matches every quote/quote pool.
    with ThreadPoolExecutor(max_workers=2) as pool:
        parts = list(
            pool.map(
                lambda topics: full_range_logs(
                    env, env.chain["poolManager"], topics, recent_fallback=True
                ),
                ([initialize, None, mine, theirs], [initialize, None, theirs, mine]),
            )
        )
    logs = [entry for part in parts for entry in part or []]
    out: list[dict[str, Any]] = []
    for entry in logs or []:
        try:
            decoded = lib.v4_pool.decode_initialize_log(entry)
        except Exception:  # noqa: BLE001 - one malformed log is not the whole answer
            continue
        key = decoded["poolKey"]
        if token.lower() in (key["currency0"].lower(), key["currency1"].lower()):
            out.append({"poolId": str(decoded["poolId"]).lower(), "poolKey": key})
    return out


def discover_pools(
    env: ChainEnv, token: str, quote: str | None = None, fee: int | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Live V4 pools holding ``token``, from every route that is cheap on this chain.

    A launchpad registry (the only log-free route to a hooked pool), the
    hook-less fee tiers against the chain's known quotes, the pools an earlier
    read confirmed for this token, and -- where a node serves it -- the
    PoolManager's ``Initialize`` log for any fee or hook. The union is
    confirmed with one slot0 multicall and remembered; ranking is the caller's.
    A named static ``fee`` also derives the hook-less pools on that tier at the
    usual tick spacings, so an unusual tier is found without Initialize logs.
    """
    lib = env.lib
    launched, launcher = _launcher_candidates(env, token, quote)
    quotes = [lib.hexutil.checksum_address(quote)] if quote else None
    vanilla = lib.v4_pool.derive_vanilla_candidates(env.chain, token, quotes)
    if fee is not None and not fee & DYNAMIC_FEE:
        tiers = tuple((fee, spacing) for spacing in _fee_spacings(fee))
        vanilla = [
            *vanilla,
            *lib.v4_pool.derive_vanilla_candidates(env.chain, token, quotes, tiers),
        ]
    candidates: dict[str, dict[str, Any]] = {}
    for candidate in (
        *launched,
        *vanilla,
        *cached_token_pools(env, token),
        *initialized_pools(env, token, quote),
    ):
        candidates.setdefault(str(candidate["poolId"]).lower(), candidate)
    live = pool_states(env, list(candidates.values()))
    if launcher is None:
        # A labelled launcher hook matched: name the launcher it belongs to.
        hooked = next(
            (
                (p, candidates[str(p["poolId"]).lower()]["_entry"])
                for p in live
                if "_entry" in candidates[str(p["poolId"]).lower()]
            ),
            None,
        )
        if hooked is not None:
            state, entry = hooked
            launcher = {
                "launcher": entry["id"],
                "name": entry["name"],
                "kind": entry["kind"],
                "hook": state["poolKey"]["hooks"],
                "locker": None,
                "numeraire": None,
            }
    if quote:
        wanted = quote.lower()
        live = [
            p
            for p in live
            if wanted in (p["poolKey"]["currency0"].lower(), p["poolKey"]["currency1"].lower())
        ]
    lib.poolcache.remember(env.chain, live)
    remember_token_pools(env, token, live)
    return live, launcher


def pool_key_for_id(env: ChainEnv, pool_id: str) -> dict[str, Any]:
    """Recover the PoolKey behind a poolId.

    The skill's PoolKey cache first (content-addressed, never stale); then the
    pair's tokens from the price index, whose launcher and hook-less candidates
    are hashed until one reproduces the id; then the pool's own ``Initialize``
    log, which names the key outright but only a node serving full-history
    logs can return (Robinhood Chain's public node does, Base's do not).
    """
    lib = env.lib
    wanted = pool_id.lower()
    cached = lib.poolcache.lookup(env.chain, wanted)
    if cached:
        return dict(cached)
    try:
        tokens = env.pair_tokens(wanted) if env.pair_tokens is not None else []
    except Exception as exc:  # noqa: BLE001 - the price index is one route of three
        log.debug("trading.lp_pair_tokens_failed", chain=env.spec.key, error=str(exc))
        tokens = []
    for token in tokens:
        others = [t for t in tokens if t.lower() != token.lower()]
        quotes = list(env.chain.get("knownQuotes") or {}) + others
        pools, _ = _launcher_candidates(env, lib.hexutil.checksum_address(token), None)
        pools += lib.v4_pool.derive_vanilla_candidates(env.chain, token, quotes)
        for candidate in pools:
            if candidate["poolId"].lower() == wanted:
                key = dict(candidate["poolKey"])
                lib.poolcache.remember(env.chain, [{"poolId": wanted, "poolKey": key}])
                return key
    # The chain's quote assets paired with each other (ETH/USDC, WETH/USDC):
    # hashing the hook-less fee tiers costs no request at all.
    known = list(env.chain.get("knownQuotes") or {})
    for token in known:
        others = [q for q in known if q.lower() != token.lower()]
        for candidate in lib.v4_pool.derive_vanilla_candidates(env.chain, token, others):
            if candidate["poolId"].lower() == wanted:
                key = dict(candidate["poolKey"])
                lib.poolcache.remember(env.chain, [{"poolId": wanted, "poolKey": key}])
                return key
    logs = full_range_logs(
        env, env.chain["poolManager"], [lib.abi.TOPIC_INITIALIZE, wanted], recent_fallback=True
    )
    for entry in logs or []:
        try:
            decoded = lib.v4_pool.decode_initialize_log(entry)
        except Exception:  # noqa: BLE001
            continue
        if str(decoded["poolId"]).lower() == wanted:
            key = dict(decoded["poolKey"])
            lib.poolcache.remember(env.chain, [{"poolId": wanted, "poolKey": key}])
            return key
    raise TradingError(
        "trading.lp.pool_key_unknown",
        f"could not recover the PoolKey for pool {pool_id} on {env.spec.name}: the price "
        "index does not list it under a known fee tier and no node served its Initialize "
        f"log. Pass the token address instead: agentos trade lp pool <token> --chain "
        f"{env.spec.key}",
        details={"poolId": pool_id, "chainId": env.spec.chain_id},
    )


def scan_pool(env: ChainEnv, state: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Liquidity segments from the tick bitmap (no logs), the scan's coverage, and its warnings.

    Touches nothing on ``env``: a read comparing several pools scans each one
    on its own and adopts only the chosen pool's warnings (``adopt_scan``), so
    a candidate that lost cannot mark the card it is not on partial or failed.
    """
    lib = env.lib
    res: dict[str, Any] = lib.v4_pool.walk_tick_ranges(
        env.client, env.chain, state["poolId"], state, max_words=MAX_BITMAP_WORDS
    )
    spacing = int(state["poolKey"]["tickSpacing"])
    full = (
        (lib.v4_math.max_usable_tick(spacing) // spacing) // 256
        - (lib.v4_math.min_usable_tick(spacing) // spacing) // 256
        + 1
    )
    truncated = res.get("truncated")
    res["scan"] = {
        "mode": res.get("mode") or "ticks",
        "scannedWords": int(truncated["scannedWords"]) if truncated else full,
        "fullWords": int(truncated["fullWords"]) if truncated else full,
        "truncated": bool(truncated),
    }
    notes: list[str] = []
    if truncated:
        notes.append(
            f"partial scan: {truncated['scannedWords']} of {truncated['fullWords']} tick-bitmap "
            "words around the current price were read; liquidity further out is not included"
        )
    if res.get("check", {}).get("ok") is False:
        notes.append(
            "self-check failed: the summed in-range liquidity does not match the pool's "
            "on-chain liquidity, so these numbers are not trustworthy"
        )
    return res, notes


def adopt_scan(env: ChainEnv, res: dict[str, Any], notes: Iterable[str]) -> None:
    """Make one pool's scan the card's: its partial flag and its warnings."""
    if res["scan"]["truncated"]:
        env.partial = True
    for note in notes:
        env.warn(note)


def walk(env: ChainEnv, state: dict[str, Any]) -> dict[str, Any]:
    """``scan_pool`` for the one pool a card is about: its warnings are the card's."""
    res, notes = scan_pool(env, state)
    adopt_scan(env, res, notes)
    return res


def fee_pct(pool_key: dict[str, Any]) -> str:
    fee = int(pool_key["fee"])
    if unilp().v4_pool.is_dynamic_fee(fee):
        return "dynamic"
    return f"{fee / 10_000:g}%"


def pool_json(side: Side, state: dict[str, Any], tvl_usd: float | None = None) -> dict[str, Any]:
    price, mcap = side.spot(state["tick"], liquid=is_liquid(state))
    hooks = state["poolKey"]["hooks"]
    return {
        "poolId": state["poolId"],
        "hook": None if int(hooks, 16) == 0 else hooks,
        "tickSpacing": int(state["poolKey"]["tickSpacing"]),
        "feePct": fee_pct(state["poolKey"]),
        "tick": int(state["tick"]),
        "liquidity": str(state.get("activeLiquidity") or 0),
        "priceUsd": price,
        "mcapUsd": mcap,
        "tvlUsd": tvl_usd,
    }


def priced_pool_json(
    env: ChainEnv, side: Side, state: dict[str, Any], tvl_usd: float | None
) -> dict[str, Any]:
    """``pool_json`` for a pool card: with no USD price for the quote asset there is
    no TVL or market cap to show, and the card says why instead of going blank."""
    pool = pool_json(side, state, tvl_usd)
    if side.quote_usd is None:
        pool["tvlUsd"] = None
        pool["mcapUsd"] = None
        env.warn(f"no USD price for {side.quote['symbol']}; TVL and mcap unavailable")
    return pool


def tvl(side: Side, res: dict[str, Any]) -> float | None:
    base_raw, quote_raw = side.split(res["amount0"], res["amount1"])
    return _sum_usd(
        amount_json(base_raw, side.base["decimals"], side.base_usd)["usd"],
        amount_json(quote_raw, side.quote["decimals"], side.quote_usd)["usd"],
    )


def active_depth_usd(side: Side, state: dict[str, Any]) -> float | None:
    """USD value of the in-range (virtual) reserve on the priced side; one multicall's worth.

    ``L / sqrtP`` of currency0 and ``L * sqrtP`` of currency1 are what the active
    liquidity is worth at the current price -- a depth that compares pools of
    different pairs and fee tiers without walking any of them.
    """
    liquidity = int(state.get("activeLiquidity") or 0)
    if liquidity <= 0:
        return 0.0
    sqrt_price = int(state["sqrtPriceX96"]) / 2**96
    if sqrt_price <= 0:
        return None
    virtual0, virtual1 = liquidity / sqrt_price, liquidity * sqrt_price
    base_raw, quote_raw = (virtual1, virtual0) if side.base_is_currency1 else (virtual0, virtual1)
    if side.quote_usd is not None:
        return finite(quote_raw / 10 ** side.quote["decimals"] * side.quote_usd)
    if side.base_usd is not None:
        return finite(base_raw / 10 ** side.base["decimals"] * side.base_usd)
    return None


def quote_rank(env: ChainEnv, side: Side) -> int:
    """0: the chain's own quote assets (WETH, USDC, USDG, ETH); 1: priced; 2: unpriced."""
    if (env.chain.get("knownQuotes") or {}).get(side.quote["address"].lower()):
        return 0
    return 1 if side.quote_usd is not None else 2


@dataclass
class PoolRead:
    state: dict[str, Any]
    side: Side
    res: dict[str, Any]
    tvl: float | None
    launcher: dict[str, Any] | None
    depth: float | None = None
    #: The scan's own warnings, adopted onto the card only if this pool is chosen.
    notes: list[str] = field(default_factory=list)


def _rank(env: ChainEnv, state: dict[str, Any], side: Side, value: float | None) -> tuple[Any, ...]:
    """Liquid before empty, then by quote asset, then deepest (TVL or in-range depth)."""
    return (
        0 if is_liquid(state) else 1,
        quote_rank(env, side),
        -(value if value is not None else -1.0),
        -int(state.get("activeLiquidity") or 0),
    )


def read_pool(
    env: ChainEnv, target: str, quote: str | None = None, fee: int | None = None
) -> PoolRead:
    """The pool a card is about: the deepest one holding a token, or a given poolId.

    "Deepest" is decided in two passes. Every live pool is pre-ranked from its
    slot0 and active liquidity alone (liquid before empty, the chain's quote
    assets before other pairs, then in-range depth in USD); only the best
    ``MAX_POOLS_COMPARED`` are tick-walked, and the winner is the one with the
    highest TVL among those, in the same order of precedence. ``fee`` keeps
    only the pools on that tier (``select_fee``) before any of it.
    """
    lib = env.lib
    if _is_pool_id(target):
        pool_id = target.strip().lower()
        key = pool_key_for_id(env, pool_id)
        if lib.v4_pool.compute_pool_id(key).lower() != pool_id:
            raise TradingError("trading.lp.not_found", f"PoolKey does not hash to {pool_id}")
        states = pool_states(env, [{"poolId": pool_id, "poolKey": key}])
        if not states:
            raise TradingError(
                "trading.lp.not_found", f"pool {pool_id} is not initialised on {env.spec.name}"
            )
        state = states[0]
        check_pool_fee(state, fee)
        side = with_implied_base_price(make_side(env, state["poolKey"]), state)
        launcher = lib.launchers.resolve_launcher(env.client, env.chain, side.base["address"])
        res = walk(env, state)
        return PoolRead(state, side, res, tvl(side, res), launcher)

    token = lib.hexutil.checksum_address(target)
    live, launcher = discover_pools(env, token, quote, fee)
    if live:
        live = select_fee(env, live, fee, token=token, quote=quote)
    if not live:
        what = f"paired with {quote} " if quote else ""
        raise TradingError(
            "trading.lp.not_found",
            f"no Uniswap V4 pool {what}holds {token} on {env.spec.name} "
            "(checked launchpad registries, the hook-less fee tiers and, where the node "
            "serves them, Initialize logs)",
            details={"token": token, "chainId": env.spec.chain_id},
        )
    currencies = [s["poolKey"][k] for s in live for k in ("currency0", "currency1")]
    token_metas(env, currencies)
    usd_prices(env, currencies)
    ranked = []
    for state in live:
        side = with_implied_base_price(make_side(env, state["poolKey"], token), state)
        depth = active_depth_usd(side, state)
        ranked.append((_rank(env, state, side, depth), state, side, depth))
    ranked.sort(key=lambda item: item[0])
    reads = []
    for _, state, side, depth in ranked[:MAX_POOLS_COMPARED]:
        # Each candidate scans into its own notes: only the winner's reach the card.
        res, notes = scan_pool(env, state)
        reads.append(PoolRead(state, side, res, tvl(side, res), launcher, depth, notes))
    reads.sort(key=lambda r: _rank(env, r.state, r.side, r.tvl if r.tvl is not None else r.depth))
    chosen = reads[0]
    adopt_scan(env, chosen.res, chosen.notes)
    symbol = chosen.side.base["symbol"]
    liquid = sum(1 for _, state, _, _ in ranked if is_liquid(state))
    if len(ranked) > 1:
        env.warn(
            f"{len(ranked)} V4 pools hold {symbol} ({liquid} with active liquidity); "
            "showing the deepest"
        )
    if any(r.res["scan"]["truncated"] for r in reads[1:]):
        # Their TVL may be understated; the card itself is not partial for it.
        env.warn("other pools were not fully scanned; their TVL may be understated")
    return chosen


# ── safety ──────────────────────────────────────────────────────────────────


def safety(
    env: ChainEnv, launcher: dict[str, Any] | None, pool_id: str, token: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Who launched the token and whether its LP is locked.

    ``locked`` is ``True`` on the condition ``lp_read.py`` prints "LP is LOCKED"
    for: a position in this pool is held by the launchpad's LP locker. Anything
    short of that evidence is ``None`` (unknown), never ``False``.
    """
    launchers = env.lib.launchers
    chain = env.chain
    if not launcher:
        return (
            {
                "launcher": {"name": None, "address": None},
                "locked": None,
                "note": "no known launchpad deployed this token; lock status unknown",
            },
            [],
        )
    entry: dict[str, Any] = next(
        (e for e in launchers.launchers_for(chain) if e["id"] == launcher.get("launcher")), {}
    )
    address = entry.get("factory") or entry.get("airlock") or launcher.get("hook")
    out: dict[str, Any] = {
        "launcher": {"name": launcher.get("name"), "address": address},
        "locked": None,
        "note": None,
    }
    locker = launcher.get("locker")
    if not locker:
        out["note"] = (
            f"launch liquidity sits with the {launcher.get('name')} hook, not an LP NFT; "
            "lock status unknown"
        )
        return out, []
    label = launchers.label_address(chain, locker) or env.lib.fmt.short(locker)
    positions = launchers.probe_position_ids(env.client, chain, locker, token)
    live = [
        p
        for p in positions
        if p["poolId"].lower() == pool_id.lower() and (p.get("liquidity") or 0) > 0
    ]
    if live:
        out["locked"] = True
        out["note"] = f"LP owned by launchpad locker ({label})"
    else:
        out["note"] = f"launchpad locker {label} holds no live position in this pool"
    return out, live


def _owner_for(segment: dict[str, Any], locked: list[dict[str, Any]], locker: str | None) -> Any:
    """The locker, when locked positions alone account for a segment's liquidity."""
    if not locked or not locker:
        return segment.get("owner")
    cover = sum(
        int(p["liquidity"] or 0)
        for p in locked
        if p["tickLower"] <= segment["tickLower"] and p["tickUpper"] >= segment["tickUpper"]
    )
    return locker if cover >= int(segment["liquidity"]) > 0 else segment.get("owner")


# ── payload builders ────────────────────────────────────────────────────────


def build_pool(
    env: ChainEnv, target: str, quote: str | None = None, fee: int | None = None
) -> dict[str, Any]:
    read = read_pool(env, target, quote, fee)
    side, state, res = read.side, read.state, read.res
    base_raw, quote_raw = side.split(res["amount0"], res["amount1"])
    guard, locked = safety(env, read.launcher, state["poolId"], side.base["address"])
    total = sum(int(r["liquidity"]) for r in res["ranges"]) or 0
    locker = read.launcher.get("locker") if read.launcher else None
    pool = priced_pool_json(env, side, state, read.tvl)
    top = [
        {
            **side.range_json(r["tickLower"], r["tickUpper"]),
            "liquidity": str(r["liquidity"]),
            "share": finite(int(r["liquidity"]) / total) if total else None,
            "owner": _owner_for(r, locked, locker),
        }
        for r in res["ranges"][:TOP_RANGES]
    ]
    return _payload(
        envelope(
            "pool",
            env,
            token=side.token_json("base"),
            quote=side.token_json("quote"),
            pool=pool,
            reserves={
                "base": amount_json(base_raw, side.base["decimals"], side.base_usd),
                "quote": amount_json(quote_raw, side.quote["decimals"], side.quote_usd),
            },
            safety=guard,
            topRanges=top,
        )
    )


def build_ranges(
    env: ChainEnv, target: str, quote: str | None = None, fee: int | None = None
) -> dict[str, Any]:
    read = read_pool(env, target, quote, fee)
    side, state, res = read.side, read.state, read.res
    total = sum(int(r["liquidity"]) for r in res["ranges"]) or 0
    segments = []
    for r in sorted(res["ranges"], key=lambda r: r["tickLower"]):
        base_raw, quote_raw = side.split(r["amount0"], r["amount1"])
        segments.append(
            {
                **side.range_json(r["tickLower"], r["tickUpper"]),
                "liquidity": str(r["liquidity"]),
                "share": finite(int(r["liquidity"]) / total) if total else None,
                "base": amount_json(base_raw, side.base["decimals"], side.base_usd),
                "quote": amount_json(quote_raw, side.quote["decimals"], side.quote_usd),
                "active": r["status"] == "in-range",
            }
        )
    price, _ = side.spot(state["tick"], liquid=is_liquid(state))
    pool = priced_pool_json(env, side, state, read.tvl)
    return _payload(
        envelope(
            "ranges",
            env,
            token=side.token_json("base"),
            quote=side.token_json("quote"),
            pool=pool,
            current={"tick": int(state["tick"]), "priceUsd": price, "mcapUsd": pool["mcapUsd"]},
            segments=segments,
            scan=res["scan"],
        )
    )


def wallet_json(env: ChainEnv, address: str | None) -> dict[str, Any]:
    lib = env.lib
    checksummed = lib.hexutil.checksum_address(address) if address else NATIVE_ADDRESS
    label = env.vault.get(checksummed.lower())
    in_app = label is not None
    if not in_app:
        label = lib.launchers.label_address(env.chain, checksummed)
    return {"address": checksummed, "label": label or None, "inApp": in_app}


def _load_position(env: ChainEnv, token_id: int) -> dict[str, Any]:
    lib = env.lib
    pm = env.chain["positionManager"]
    abi = lib.abi.POSITION_MANAGER_ABI
    info, liquidity, owner = env.client.multicall(
        [
            {"address": pm, "abi": abi, "functionName": fn, "args": [int(token_id)]}
            for fn in ("getPoolAndPositionInfo", "getPositionLiquidity", "ownerOf")
        ]
    )
    if info["status"] != "success" or owner["status"] != "success":
        raise TradingError(
            "trading.lp.not_found",
            f"position #{token_id} does not exist on {env.spec.name} (never minted, or burned)",
            details={"tokenId": str(token_id), "chainId": env.spec.chain_id},
        )
    raw_key, packed = info["result"]
    return {
        "tokenId": int(token_id),
        "poolKey": lib.v4_pool.normalize_pool_key(raw_key),
        **lib.v4_pool.decode_position_info(packed),
        "liquidity": int(liquidity["result"]) if liquidity["status"] == "success" else 0,
        "owner": owner["result"],
    }


def fees_owed(
    env: ChainEnv, items: list[tuple[dict[str, Any], dict[str, Any]]]
) -> dict[int, tuple[int, int] | None]:
    """Uncollected (fees0, fees1) per tokenId for many positions in one multicall.

    The same arithmetic as the library's ``get_fees_owed`` (fee-growth counters
    wrap, so the subtraction is mod 2**256), batched: that function is one
    round trip per position. A position whose read fails maps to ``None``;
    a closed one to ``(0, 0)`` without a read.
    """
    lib = env.lib
    hx = lib.hexutil
    state_view = env.chain["stateView"]
    manager = hx.checksum_address(env.chain["positionManager"])
    abi = lib.abi.STATE_VIEW_ABI
    out: dict[int, tuple[int, int] | None] = {}
    live: list[tuple[int, list[Any]]] = []
    calls: list[dict[str, Any]] = []
    for pos, state in items:
        token_id = int(pos["tokenId"])
        if int(pos["liquidity"]) <= 0:
            out[token_id] = (0, 0)
            continue
        tl, tu = int(pos["tickLower"]), int(pos["tickUpper"])
        salt = hx.pad(hx.to_hex(token_id), size=32)
        calls += [
            {
                "address": state_view,
                "abi": abi,
                "functionName": "getPositionInfo",
                "args": [state["poolId"], manager, tl, tu, salt],
            },
            {
                "address": state_view,
                "abi": abi,
                "functionName": "getFeeGrowthInside",
                "args": [state["poolId"], tl, tu],
            },
        ]
        live.append((token_id, []))
    try:
        results = multicall(env, calls)
    except Exception as exc:  # noqa: BLE001 - fees are a nice-to-have, never fatal
        log.debug("trading.lp_fees_failed", chain=env.spec.key, error=str(exc))
        results = [{"status": "failure", "result": None}] * len(calls)
    q128 = 1 << 128
    for i, (token_id, _) in enumerate(live):
        info, growth = results[2 * i], results[2 * i + 1]
        if info["status"] != "success" or growth["status"] != "success":
            out[token_id] = None
            continue
        liquidity, last0, last1 = (int(v) for v in info["result"])
        current0, current1 = (int(v) for v in growth["result"])
        out[token_id] = (
            liquidity * hx.as_uint_n(256, current0 - last0) // q128,
            liquidity * hx.as_uint_n(256, current1 - last1) // q128,
        )
    return out


def position_json(
    env: ChainEnv,
    pos: dict[str, Any],
    state: dict[str, Any],
    side: Side,
    fees: tuple[int, int] | None,
) -> dict[str, Any]:
    """One ``Position``; ``fees`` is (fees0, fees1) from :func:`fees_owed`, ``None`` if unread."""
    lib = env.lib
    tl, tu, liquidity = int(pos["tickLower"]), int(pos["tickUpper"]), int(pos["liquidity"])
    principal = lib.v4_math.get_amounts_for_liquidity_at_ticks(
        state["sqrtPriceX96"], tl, tu, liquidity
    )
    p_base, p_quote = side.split(principal["amount0"], principal["amount1"])
    principal_json: dict[str, Any] = {
        "base": amount_json(p_base, side.base["decimals"], side.base_usd),
        "quote": amount_json(p_quote, side.quote["decimals"], side.quote_usd),
    }
    principal_json["usd"] = _sum_usd(principal_json["base"]["usd"], principal_json["quote"]["usd"])

    fees_json: dict[str, Any] | None = None
    if fees is not None:
        f_base, f_quote = side.split(*fees)
        fees_json = {
            "base": amount_json(f_base, side.base["decimals"], side.base_usd),
            "quote": amount_json(f_quote, side.quote["decimals"], side.quote_usd),
        }
        fees_json["usd"] = _sum_usd(fees_json["base"]["usd"], fees_json["quote"]["usd"])

    status = side.status(state["tick"], tl, tu, liquidity)
    rng = side.range_json(tl, tu)
    distance: float | None = None
    now_price = side.price_at(state["tick"])
    if now_price is not None and status == "above-range" and rng["priceUpper"]:
        distance = finite(round((now_price / rng["priceUpper"] - 1) * 100, 2))
    elif now_price is not None and status == "below-range" and rng["priceLower"]:
        distance = finite(round((1 - now_price / rng["priceLower"]) * 100, 2))
    return {
        "chain": chain_json(env.spec),
        "tokenId": str(pos["tokenId"]),
        "owner": wallet_json(env, pos.get("owner")),
        "token": side.token_json("base"),
        "quote": side.token_json("quote"),
        "pool": pool_json(side, state),
        "range": rng,
        "status": status,
        "liquidity": str(liquidity),
        "principal": principal_json,
        "fees": fees_json,
        "valueUsd": _sum_usd(principal_json["usd"], fees_json["usd"] if fees_json else None),
        "band": side.band_label(tl, tu),
        "distancePct": distance,
    }


def build_position(env: ChainEnv, token_id: int) -> dict[str, Any]:
    lib = env.lib
    pos = _load_position(env, token_id)
    pool_id = lib.v4_pool.compute_pool_id(pos["poolKey"])
    states = pool_states(env, [{"poolId": pool_id, "poolKey": pos["poolKey"]}])
    if not states:
        raise TradingError("trading.lp.not_found", f"the pool of #{token_id} is not initialised")
    side = with_implied_base_price(make_side(env, pos["poolKey"]), states[0], liquid_only=False)
    if lib.launchers.is_locker_address(env.chain, pos["owner"]):
        label = lib.launchers.label_address(env.chain, pos["owner"])
        env.warn(f"LP is LOCKED: this position is held by the launchpad locker ({label})")
    fees = fees_owed(env, [(pos, states[0])])[int(token_id)]
    if fees is None:
        env.warn(f"uncollected fees of #{token_id} could not be read")
    payload = position_json(env, pos, states[0], side, fees)
    return _payload(envelope("position", env, position=payload))


# ── positions: finding a wallet's NFTs ──────────────────────────────────────


class _PositionSearch:
    """Candidate tokenIds in, owned positions out, and whether each wallet is complete.

    ``balanceOf`` says how many NFTs a wallet holds, so the search knows when
    to stop: every candidate is re-verified with ``ownerOf`` (a Transfer log or
    an indexer row only says the wallet *once* held it), and a wallet counts as
    done when its verified positions reach its balance. The indexer and the
    log walk feed it from two threads at once, and both stop at the env's
    deadline (``stopped`` then says the budget, not the chain, ended it).
    """

    #: Ids verified per wave (three calls each, so one wave is
    #: ``POSITION_WORKERS`` concurrent multicall chunks).
    WAVE = MULTICALL_CHUNK * POSITION_WORKERS // 3

    def __init__(self, env: ChainEnv, expected: dict[str, int]) -> None:
        self.env = env
        self.expected = expected
        self.checked: set[int] = set()
        self.live: dict[int, dict[str, Any]] = {}
        self.stopped = False
        self._lock = threading.Lock()

    def have(self, owner: str) -> int:
        with self._lock:
            return sum(1 for p in self.live.values() if str(p["owner"]).lower() == owner)

    def snapshot(self) -> list[dict[str, Any]]:
        """The positions verified so far, by tokenId."""
        with self._lock:
            return [self.live[t] for t in sorted(self.live)]

    def complete(self) -> bool:
        return all(need >= 0 and self.have(o) >= need for o, need in self.expected.items())

    def out_of_time(self) -> bool:
        if self.env.expired():
            self.stopped = True
        return self.stopped

    def verify(self, ids: Iterable[int], *, until_complete: bool = False) -> None:
        """Check ``ids`` on chain in waves, in the order given.

        Stops at the deadline, and with ``until_complete`` as soon as every
        wallet is accounted for.
        """
        ordered = list(dict.fromkeys(int(i) for i in ids))
        for start_at in range(0, len(ordered), self.WAVE):
            if (until_complete and self.complete()) or self.out_of_time():
                return
            with self._lock:
                fresh = [
                    i for i in ordered[start_at : start_at + self.WAVE] if i not in self.checked
                ]
                self.checked.update(fresh)
            if fresh:
                self._verify(fresh)

    def _read(self, window: list[int]) -> list[dict[str, Any]]:
        lib = self.env.lib
        pm = self.env.chain["positionManager"]
        abi = lib.abi.POSITION_MANAGER_ABI
        return multicall(
            self.env,
            [
                {"address": pm, "abi": abi, "functionName": fn, "args": [token_id]}
                for token_id in window
                for fn in ("ownerOf", "getPositionLiquidity", "getPoolAndPositionInfo")
            ],
        )

    def _verify(self, window: list[int]) -> None:
        lib = self.env.lib
        results = self._read(window)
        by_id = {t: results[3 * j : 3 * j + 3] for j, t in enumerate(window)}
        # A call the node failed (it gave up on the chunk, down to this one
        # call) says nothing about the token: ask once more. A revert -- a
        # burned or never-minted id -- is an answer and is not retried.
        faulted = [
            t
            for t, (owner, _, info) in by_id.items()
            if any(
                r["status"] != "success" and r.get("error") == _NODE_FAULT for r in (owner, info)
            )
        ]
        if faulted and not self.out_of_time():
            again = self._read(faulted)
            by_id.update({t: again[3 * j : 3 * j + 3] for j, t in enumerate(faulted)})
        found: dict[int, dict[str, Any]] = {}
        for token_id, (owner, liq, info) in by_id.items():
            if owner["status"] != "success" or info["status"] != "success":
                continue
            if str(owner["result"]).lower() not in self.expected:
                continue
            raw_key, packed = info["result"]
            found[token_id] = {
                "tokenId": token_id,
                "owner": owner["result"],
                "poolKey": lib.v4_pool.normalize_pool_key(raw_key),
                "liquidity": int(liq["result"]) if liq["status"] == "success" else 0,
                **lib.v4_pool.decode_position_info(packed),
            }
        with self._lock:
            self.live.update(found)


#: The skill's multicall reports a call the node kept failing (not a revert) so.
_NODE_FAULT = "call failed on its own"


def _log_order(entry: dict[str, Any]) -> tuple[int, int]:
    def number(value: Any) -> int:
        if isinstance(value, str):
            return int(value, 16) if value.startswith("0x") else int(value or 0)
        return int(value or 0)

    return number(entry.get("blockNumber")), number(entry.get("logIndex"))


def holdings_from_logs(logs: Iterable[Any], owners: Iterable[str]) -> tuple[list[int], list[int]]:
    """From ERC-721 Transfer logs: (ids whose latest transfer lands with ``owners``,
    every id ``owners`` ever received), both newest receipt first.

    A market-making wallet can have received thousands of position NFTs and
    hold a dozen; with the outgoing transfers in ``logs`` too, the first list
    is those dozen and only they need an ``ownerOf`` check.
    """
    wanted = {o.lower() for o in owners}
    last_to: dict[int, str] = {}
    received: dict[int, tuple[int, int]] = {}
    entries = [e for e in logs if isinstance(e, dict)]
    for entry in sorted(entries, key=_log_order):
        try:
            token_id = int(entry["topics"][3], 16)
            to = "0x" + str(entry["topics"][2])[-40:].lower()
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        last_to[token_id] = to
        if to in wanted:
            received[token_id] = _log_order(entry)

    def newest(ids: Iterable[int]) -> list[int]:
        return sorted(ids, key=lambda t: (received.get(t, (0, 0)), t), reverse=True)

    return newest(t for t, to in last_to.items() if to in wanted), newest(received)


def _search_logs(env: ChainEnv, search: _PositionSearch, owners: list[str]) -> str | None:
    """Feed the search from ERC-721 Transfer logs into and out of ``owners``.

    Full history first, both directions at once, from a node that serves it
    (Robinhood Chain's public one does, in about a second): what the wallets
    still hold by the logs is verified first, then -- only if that falls short
    of the balance -- everything they ever received, newest first. A node that
    refuses walks back from the head in batches of windows it does accept and
    stops as soon as every wallet is complete, at the oldest block any wallet
    can have received a position (its creation block, when the vault knows
    it), or when the time budget runs out.

    Returns what was left unsearched when time ran out (``None`` otherwise).
    """
    lib = env.lib
    chain = env.chain
    padded = [lib.hexutil.pad(o.lower(), size=32) for o in owners]
    manager = chain["positionManager"]
    transfer = lib.abi.TOPIC_ERC721_TRANSFER
    with ThreadPoolExecutor(max_workers=2) as pool:
        received, sent = pool.map(
            lambda topics: full_range_logs(env, manager, topics),
            ([transfer, None, padded], [transfer, padded]),
        )
    if received is not None:
        held, ever = holdings_from_logs([*received, *(sent or [])], owners)
        if sent is not None:
            # Without the outgoing side "held" is everything ever received:
            # leave that to the newest-first waves below.
            search.verify(held)
        search.verify(ever, until_complete=True)
        return "part of the logged transfers" if search.stopped else None

    base_filter = {"address": manager, "topics": [transfer, None, padded]}
    genesis = int(chain["logScan"].get("fromBlock", 0))
    floor = min(env.first_blocks.get(o.lower(), genesis) for o in owners)
    floor = max(floor, genesis)
    deadline = time.monotonic() + LOG_SCAN_BUDGET_S
    if env.deadline is not None:
        deadline = min(deadline, env.deadline)
    top = env.block or int(env.client.block_number())
    spans = [env.spec.approval_log_span, env.spec.max_log_span]
    retries = 0
    while spans and top >= floor and not search.complete():
        if time.monotonic() >= deadline or search.out_of_time():
            search.stopped = True
            return f"Transfer logs before block {top:,}"
        span = spans[0]
        calls = []
        to_block = top
        for _ in range(LOG_WINDOWS_PER_BATCH):
            if to_block < floor:
                break
            from_block = max(to_block - span + 1, floor)
            window = {**base_filter, "fromBlock": hex(from_block), "toBlock": hex(to_block)}
            calls.append({"method": "eth_getLogs", "params": [window]})
            to_block = from_block - 1
        try:
            results = env.client.batch(calls)
        except Exception as exc:  # noqa: BLE001 - classified below, like a per-call error
            results = [{"error": str(exc)}]
        errors = [r["error"] for r in results if isinstance(r, dict) and "error" in r]
        if errors:
            kinds = {log_failure_kind(e) for e in errors}
            if (
                not kinds & {"refused", "oversized"}
                and "transient" in kinds
                and retries < WALK_RETRIES
            ):
                # A busy node: the same windows again, not a narrower span.
                retries += 1
                time.sleep(LOG_RETRY_DELAY_S)
                continue
            retries = 0
            spans.pop(0)
            continue
        retries = 0
        logs = [entry for batch in results if isinstance(batch, list) for entry in batch]
        search.verify(holdings_from_logs(logs, owners)[1], until_complete=True)
        top = to_block
    if top >= floor and not search.complete():
        env.warn(f"{env.spec.name}: the node would not serve Transfer logs before block {top:,}")
    return None


def _search_indexer(
    env: ChainEnv, search: _PositionSearch, owners: list[str], done: threading.Event
) -> str | None:
    """Feed the search from the chain's NFT indexer, page by page, until the deadline.

    The indexer's pages arrive on the event loop; they are verified here as
    they come, so twenty pages of a thousand-position wallet overlap with
    their own ``ownerOf`` checks. Returns what was left unread at the deadline.
    """
    try:
        nft_ids = env.nft_ids
        if nft_ids is None:
            return None
        pages: queue.Queue[list[int] | None] = queue.Queue()
        pending = list(owners)

        def fetch() -> None:
            try:
                while pending and not search.out_of_time():
                    owner = pending[0]
                    try:
                        ids = nft_ids(owner, on_page=pages.put, deadline=env.deadline)
                    except Exception as exc:  # noqa: BLE001 - the indexer is a shortcut
                        log.debug("trading.lp_indexer_failed", chain=env.spec.key, error=str(exc))
                        ids = None
                    # A fake (or an indexer without paging) answers all at once.
                    if ids:
                        pages.put(list(ids))
                    if search.out_of_time():
                        break  # cut off mid-listing: this owner is not done
                    pending.pop(0)
            finally:
                pages.put(None)

        threading.Thread(target=fetch, name="lp-indexer", daemon=True).start()
        while True:
            left = env.remaining()
            try:
                page = pages.get(timeout=None if left is None else max(left, 0.0))
            except queue.Empty:
                search.stopped = True
                break
            if page is None:
                break
            search.verify(page)
            if search.stopped:
                break
        if search.stopped and pending:
            return "the indexer's remaining pages"
        return None
    finally:
        done.set()


def scan_positions(env: ChainEnv, owners: list[str]) -> list[dict[str, Any]]:
    """Every position NFT the ``owners`` hold on this chain, closed ones included.

    Candidates come from the chain's indexer and from Transfer logs, raced:
    the log walk starts once the indexer is done or ``INDEXER_HEAD_START_S``
    later, whichever is first, and both stop at the env's deadline. A wallet
    whose verified count still falls short of its ``balanceOf`` marks the
    scan partial and is named in a warning; so is what the budget cut off.
    """
    lib = env.lib
    pm = env.chain["positionManager"]
    balances = env.client.multicall(
        [
            {
                "address": pm,
                "abi": lib.abi.ERC20_ABI,
                "functionName": "balanceOf",
                "args": [lib.hexutil.checksum_address(o)],
            }
            for o in owners
        ]
    )
    expected: dict[str, int] = {}
    for owner, result in zip(owners, balances, strict=True):
        if result["status"] != "success":
            env.partial = True
            env.warn(f"{env.spec.name}: could not read how many positions {owner} holds")
            expected[owner.lower()] = -1
        elif int(result["result"]) > 0:
            expected[owner.lower()] = int(result["result"])
    if not expected:
        return []
    holders = list(expected)
    search = _PositionSearch(env, expected)
    env.progress.search = search
    indexer_done = threading.Event()

    def logs() -> str | None:
        indexer_done.wait(INDEXER_HEAD_START_S)
        if search.complete() or search.out_of_time():
            return None
        return _search_logs(env, search, holders)

    pool = ThreadPoolExecutor(max_workers=2)
    from_indexer = pool.submit(_search_indexer, env, search, holders, indexer_done)
    from_logs = pool.submit(logs)
    left = env.remaining()
    # A verify wave or a log batch in flight at the deadline is not waited for:
    # what is verified by then is the answer, and the reserve goes to pricing.
    wait(
        [from_indexer, from_logs],
        timeout=None if left is None else max(left, 0.0) + DISCOVERY_GRACE_S,
    )
    pool.shutdown(wait=False)
    skipped: list[str | None] = []
    for future, unread in (
        (from_indexer, "the indexer's remaining pages"),
        (from_logs, "the Transfer logs"),
    ):
        if future.done():
            skipped.append(future.result())
        else:
            search.stopped = True
            skipped.append(unread)
    found = search.snapshot()
    env.progress.found = found
    for owner in holders:
        need = expected[owner]
        have = sum(1 for p in found if str(p["owner"]).lower() == owner)
        if need > have:
            env.partial = True
            env.warn(
                f"{env.spec.name}: found {have} of the {need} position(s) held by "
                f"{lib.hexutil.checksum_address(owner)}"
            )
    if search.stopped and not search.complete():
        env.partial = True
        unread = " and ".join(s for s in skipped if s) or "the rest of the search"
        limit = f"the {env.budget_s:g} s time budget" if env.budget_s else "the time limit"
        env.warn(
            f"{env.spec.name}: {limit} ran out before {unread} could be read; "
            "pass --budget-seconds to look further"
        )
    return found


def _pool_ids(env: ChainEnv, positions: list[dict[str, Any]]) -> None:
    """Set ``poolId`` on every position; one keccak per distinct PoolKey (pure Python)."""
    ids: dict[tuple[Any, ...], str] = {}
    for pos in positions:
        key = pos["poolKey"]
        ident = tuple(str(key[k]).lower() for k in sorted(key))
        if ident not in ids:
            ids[ident] = env.lib.v4_pool.compute_pool_id(key)
        pos["poolId"] = ids[ident]


def _position_rows(
    env: ChainEnv,
    jobs: list[tuple[dict[str, Any], dict[str, Any]]],
    fees: dict[int, tuple[int, int] | None],
) -> list[dict[str, Any]]:
    """One ``Position`` row per job, at the prices ``env`` holds now."""
    rows = []
    for pos, state in jobs:
        side = with_implied_base_price(make_side(env, pos["poolKey"]), state, liquid_only=False)
        rows.append(position_json(env, pos, state, side, fees.get(int(pos["tokenId"]))))
    return rows


def build_chain_positions(
    env: ChainEnv, owners: list[str], include_closed: bool
) -> list[dict[str, Any]]:
    """Every position of ``owners`` on this chain as ``Position`` rows (unsorted).

    Prices: when a wallet holds more distinct tokens than ``PRICE_LOOKUP_MAX``
    (a launch bot holds hundreds), only the chain's quote assets are looked up
    first; the rows are valued from those (a token's own pool gives its price)
    and sorted, and then the tokens of the rows a card lists -- plus the
    most-held others, up to the cap -- are looked up and the rows rebuilt.
    """
    found = scan_positions(env, owners)
    if not include_closed:
        found = [p for p in found if p["liquidity"] > 0]
    _pool_ids(env, found)
    by_pool = {p["poolId"]: {"poolId": p["poolId"], "poolKey": p["poolKey"]} for p in found}
    states = {s["poolId"]: s for s in pool_states(env, list(by_pool.values()))}
    jobs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for pos in found:
        state = states.get(pos["poolId"])
        if state is None:
            env.warn(f"{env.spec.name}: the pool of #{pos['tokenId']} could not be read")
            env.partial = True
            continue
        jobs.append((pos, state))
    currencies = [p["poolKey"][k] for p, _ in jobs for k in ("currency0", "currency1")]
    token_metas(env, currencies)
    env.progress.jobs = jobs
    # Uncollected fees are one multicall; read them while the prices are looked up.
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        pending_fees = pool.submit(fees_owed, env, jobs)
        pending_fees.add_done_callback(lambda done: _publish_fees(env, done))
        return _price_and_build(env, jobs, currencies, pending_fees)
    finally:
        pool.shutdown(wait=False)


def _publish_fees(env: ChainEnv, done: Future[dict[int, tuple[int, int] | None]]) -> None:
    if not done.cancelled() and done.exception() is None:
        env.progress.fees = done.result()


def salvage_positions(env: ChainEnv, include_closed: bool) -> list[dict[str, Any]]:
    """Rows from what a chain cut off at the hard deadline had published.

    Priced from the lookups already made (none are started), fees where they
    were read. Positions found before their pools were read cost one pool-state
    call here; ``lp_positions`` bounds it with ``POSITIONS_SALVAGE_S``.
    """
    progress = env.progress
    # A copy that never starts a price lookup: the worker may still toggle the flag.
    frozen = replace(env, prices_frozen=True)
    jobs = progress.jobs
    if jobs is None:
        search = progress.search
        found = progress.found
        if found is None:
            found = search.snapshot() if isinstance(search, _PositionSearch) else []
            if found:
                env.warn(
                    f"{env.spec.name}: the search was cut off after {len(found)} verified "
                    "position(s)"
                )
        if not include_closed:
            found = [p for p in found if p["liquidity"] > 0]
        if not found:
            return []
        found = [dict(p) for p in found]
        _pool_ids(frozen, found)
        by_pool = {p["poolId"]: {"poolId": p["poolId"], "poolKey": p["poolKey"]} for p in found}
        states = {s["poolId"]: s for s in pool_states(frozen, list(by_pool.values()))}
        jobs = [(p, states[p["poolId"]]) for p in found if p["poolId"] in states]
        token_metas(frozen, [p["poolKey"][k] for p, _ in jobs for k in ("currency0", "currency1")])
    if progress.fees is None and jobs:
        env.warn(f"{env.spec.name}: uncollected fees were not read in time")
    return _position_rows(frozen, jobs, progress.fees or {})


def _price_timeout(env: ChainEnv) -> float | None:
    left = env.remaining(env.budget_end)
    return None if left is None else max(1.0, left - 1.0)


def _price_and_build(
    env: ChainEnv,
    jobs: list[tuple[dict[str, Any], dict[str, Any]]],
    currencies: list[str],
    pending_fees: Future[dict[int, tuple[int, int] | None]],
) -> list[dict[str, Any]]:
    held = Counter(c.lower() for c in currencies)
    known = {q.lower() for q in (env.chain.get("knownQuotes") or {})}
    others = [c for c, _ in held.most_common() if c not in known]
    if len(others) <= PRICE_LOOKUP_MAX:
        usd_prices(env, list(held), timeout=_price_timeout(env))
        env.prices_frozen = True
        return _position_rows(env, jobs, _fees(env, jobs, pending_fees))
    usd_prices(env, [c for c in held if c in known], timeout=_price_timeout(env))
    fees = _fees(env, jobs, pending_fees)
    env.prices_frozen = True
    draft = sort_positions(_position_rows(env, jobs, fees))[:MAX_POSITIONS_LISTED]
    listed = [
        str(t["address"]).lower()
        for row in draft
        for t in (row["token"], row["quote"])
        if str(t["address"]).lower() not in known
    ]
    wanted = list(dict.fromkeys([*listed, *others]))[: max(PRICE_LOOKUP_MAX, len(set(listed)))]
    env.prices_frozen = False
    got = usd_prices(env, wanted, timeout=_price_timeout(env))
    env.prices_frozen = True
    answered = sum(1 for v in got.values() if v is not None)
    if answered:
        env.warn(
            f"{env.spec.name}: USD prices were looked up for {len(wanted)} of the "
            f"{len(others)} tokens held (the listed rows' first); the other positions are "
            "valued at their own pool's price"
        )
    else:
        env.warn(
            f"{env.spec.name}: no price source answered for the {len(others)} tokens held; "
            "positions are valued at their own pool's price where one is known"
        )
    return _position_rows(env, jobs, fees)


def _fees(
    env: ChainEnv,
    jobs: list[tuple[dict[str, Any], dict[str, Any]]],
    pending: Future[dict[int, tuple[int, int] | None]],
) -> dict[int, tuple[int, int] | None]:
    left = env.remaining(env.budget_end)
    try:
        fees = pending.result(timeout=None if left is None else max(left, 0.5))
    except TimeoutError:
        fees = {}
    unread = [str(pos["tokenId"]) for pos, _ in jobs if fees.get(int(pos["tokenId"])) is None]
    if unread:
        shown = ", ".join(f"#{t}" for t in unread[:5]) + (" …" if len(unread) > 5 else "")
        env.warn(f"uncollected fees of {len(unread)} position(s) could not be read ({shown})")
    return fees


_GROUP = {"above-range": 0, "below-range": 0, "in-range": 1, "closed": 2}


def sort_positions(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Out-of-range first, then in-range, then closed; each by value desc, unpriced last."""

    def key(p: dict[str, Any]) -> tuple[int, int, float, int, int]:
        value = p.get("valueUsd")
        return (
            _GROUP.get(str(p.get("status")), 3),
            1 if value is None else 0,
            -(value or 0.0),
            int(p["chain"]["id"]),
            int(p["tokenId"]),
        )

    return sorted(positions, key=key)


def positions_payload(
    envs: list[ChainEnv],
    wallets: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    warnings: list[str] | None = None,
    limit: int = MAX_POSITIONS_LISTED,
) -> dict[str, Any]:
    """The ``positions`` card: at most ``limit`` rows, totals over every position found."""
    ordered = sort_positions(positions)
    values = [p["valueUsd"] for p in ordered]
    fees = [(p.get("fees") or {}).get("usd") for p in ordered]
    priced = [v for v in values if v is not None]
    priced_fees = [f for f in fees if f is not None]
    all_warnings: list[str] = list(warnings or [])
    for env in envs:
        all_warnings.extend(w for w in env.warnings if w not in all_warnings)
    unpriced = len(values) - len(priced)
    if unpriced:
        all_warnings.append(
            f"{unpriced} position(s) have no USD price and are left out of the total"
        )
    # Unpriced fees are left out like unpriced values. But when nothing priced is
    # left except zeros, the total would read "$0.00 waiting" -- the one number a
    # trader acts on -- so it is withheld rather than shown wrong.
    unpriced_fees = len(fees) - len(priced_fees)
    fees_total: float | None = finite(sum(priced_fees)) if priced_fees or not fees else None
    if unpriced_fees:
        if any(f > 0 for f in priced_fees):
            all_warnings.append(
                f"uncollected fees of {unpriced_fees} position(s) have no USD price and "
                "are left out of the fees total"
            )
        else:
            fees_total = None
            all_warnings.append(
                f"uncollected fees of {unpriced_fees} position(s) have no USD price; "
                "the fees total is unavailable"
            )
    if len(ordered) > limit:
        all_warnings.append(
            f"showing {limit} of {len(ordered)} positions (out of range first, then by "
            f"value); the totals cover all {len(ordered)}"
        )
    blocks = [e.block for e in envs if e.block]
    # Blocks of different chains are not comparable: the envelope carries the
    # first chain's, and ``asOfBlocks`` has every chain's by key.
    return _payload(
        {
            "version": PAYLOAD_VERSION,
            "kind": "positions",
            "chain": None,
            "asOfBlock": blocks[0] if blocks else 0,
            "asOfBlocks": {e.spec.key: e.block for e in envs},
            "fetchedAt": min((e.fetched_at for e in envs if e.fetched_at), default=_now_iso()),
            "partialScan": any(e.partial for e in envs),
            "warnings": all_warnings,
            "wallets": wallets,
            "chains": [chain_json(e.spec) for e in envs],
            "positions": ordered[:limit],
            "totals": {
                "valueUsd": finite(sum(priced)) if priced or not values else None,
                "feesUsd": fees_total,
                "count": len(ordered),
                "outOfRange": sum(
                    1 for p in ordered if p["status"] in ("above-range", "below-range")
                ),
            },
        }
    )


def token_contracts(env: ChainEnv, addresses: list[str]) -> dict[str, str]:
    """The ``addresses`` that are ERC-20 token contracts holding no position here.

    A token address pasted where a wallet belongs would otherwise come back as
    a confident empty card. Code alone is not the test -- a Safe or any smart
    wallet has code and must still be scanned -- so a contract counts as a
    token only when it answers ``symbol()`` and ``decimals()``. One that holds
    position NFTs (an LP vault issuing ERC-20 shares) is scanned all the same.
    Address -> symbol; an RPC that fails here reports nothing.
    """
    lib = env.lib
    checksummed = [lib.hexutil.checksum_address(a) for a in addresses]
    codes = env.client.batch(
        [{"method": "eth_getCode", "params": [a, "latest"]} for a in checksummed]
    )
    contracts = [
        address
        for address, code in zip(checksummed, codes, strict=True)
        if isinstance(code, str)
        and len(code) > 2
        # An EIP-7702 delegation designator: an EOA with a delegate, i.e. a wallet.
        and not code.lower().startswith("0xef0100")
    ]
    if not contracts:
        return {}
    pm = env.chain["positionManager"]
    results = env.client.multicall(
        [
            call
            for address in contracts
            for call in (
                {"address": address, "abi": lib.abi.ERC20_ABI, "functionName": "symbol"},
                {"address": address, "abi": lib.abi.ERC20_ABI, "functionName": "decimals"},
                {
                    "address": pm,
                    "abi": lib.abi.ERC20_ABI,
                    "functionName": "balanceOf",
                    "args": [address],
                },
            )
        ]
    )
    out: dict[str, str] = {}
    for i, address in enumerate(contracts):
        symbol, decimals, held = results[3 * i : 3 * i + 3]
        if symbol["status"] != "success" or decimals["status"] != "success":
            continue
        if held["status"] == "success" and int(held["result"]) > 0:
            continue
        out[address.lower()] = str(symbol["result"] or lib.fmt.short(address))
    return out


# ── engine glue (async) ─────────────────────────────────────────────────────


def _bridge[T](loop: asyncio.AbstractEventLoop, coro: Coroutine[Any, Any, T], timeout: float) -> T:
    """Run an engine coroutine from the worker thread on the loop that started it."""
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)


class PriceBook:
    """USD prices shared by every LP read in the process; safe across worker threads.

    A found price is kept for ``PRICE_TTL_S`` (a miss is never kept, so the next
    read asks again), and a lookup already in flight for a token is waited on
    rather than sent a second time: nine concurrent reads that all need USDG
    cost one request. Nothing is ever cleared while another read is using it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[tuple[str, str], tuple[float, float]] = {}
        self._flights: dict[tuple[str, str], Future[float | None]] = {}

    def lookup(self, chain: str, addresses: list[str], fetch: PriceFn) -> dict[str, float | None]:
        """Prices for ``addresses`` (lower-cased keys); ``fetch`` is asked only for the rest."""
        keys = list(dict.fromkeys(a.lower() for a in addresses if a))
        out: dict[str, float | None] = {}
        waits: dict[str, Future[float | None]] = {}
        mine: list[str] = []
        now = time.monotonic()
        with self._lock:
            for key in keys:
                hit = self._hits.get((chain, key))
                if hit is not None and now - hit[0] < PRICE_TTL_S:
                    out[key] = hit[1]
                elif (flight := self._flights.get((chain, key))) is not None:
                    waits[key] = flight
                else:
                    self._flights[(chain, key)] = Future()
                    mine.append(key)
        if mine:
            found: dict[str, float | None] = {}
            try:
                found = fetch(mine)
            finally:
                with self._lock:
                    stamp = time.monotonic()
                    if len(self._hits) > 4096:
                        self._hits = {
                            k: v for k, v in self._hits.items() if stamp - v[0] < PRICE_TTL_S
                        }
                    for key in mine:
                        price = finite((found or {}).get(key))
                        if price is not None:
                            self._hits[(chain, key)] = (stamp, price)
                        self._flights.pop((chain, key)).set_result(price)
                        out[key] = price
        for key, flight in waits.items():
            try:
                out[key] = flight.result(timeout=120.0)
            except Exception:  # noqa: BLE001 - the owner's failure is a miss here too
                out[key] = None
        return out


_price_book = PriceBook()


def _gecko_window(network: str, window: list[str]) -> dict[str, Any] | None:
    """One GeckoTerminal ``token_price`` call; ``None`` when it did not answer.

    Unlike the skill's client this tells "no answer" (429, 5xx, timeout, network)
    apart from "answered, no price", so only the former is retried.
    """
    lib = unilp()
    url = f"{lib.prices._API}/{network}/token_price/{','.join(window)}"
    request = urllib.request.Request(
        url, headers={"accept": "application/json", "User-Agent": lib.rpc.USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=GECKO_TIMEOUT_S) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return None if exc.code == 429 or exc.code >= 500 else {}
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    attributes = ((body or {}).get("data") or {}).get("attributes") or {}
    prices = attributes.get("token_prices") or {}
    return {str(k).lower(): v for k, v in prices.items()} if isinstance(prices, dict) else {}


def gecko_prices(chain: dict[str, Any], addresses: list[str]) -> dict[str, float | None]:
    """USD prices from GeckoTerminal (the fallback source), retried once when unanswered."""
    network = chain.get("geckoNetwork")
    if not network or not addresses:
        return {}
    wrapped = str(chain.get("wrappedNative") or "").lower()
    query = {a: (wrapped if is_native(a) and wrapped else a.lower()) for a in addresses}
    unique = list(dict.fromkeys(query.values()))
    found: dict[str, Any] = {}
    for start in range(0, len(unique), 30):
        window = unique[start : start + 30]
        answer = _gecko_window(network, window)
        if answer is None:
            time.sleep(PRICE_RETRY_DELAY_S)
            answer = _gecko_window(network, window)
        if answer is None:
            log.info("trading.lp_price_unanswered", source="geckoterminal", tokens=len(window))
            continue
        found.update(answer)
    return {a.lower(): finite(_float(found.get(q))) for a, q in query.items()}


def _float(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def _engine_env(
    service: TradingService, spec: ChainSpec, loop: asyncio.AbstractEventLoop
) -> ChainEnv:
    lib = unilp()
    overrides = dict(getattr(service.config, "rpc_urls", {}) or {})
    url = rpc_url_for(spec, overrides)
    references = dict(getattr(service.config, "rpc_header_env", {}) or {})
    header_env = references.get(str(spec.chain_id), references.get(spec.key, {}))
    rpc_class: Any = lib.rpc.RpcClient
    if header_env:
        # Reuse the chain's engine transport, never urllib's redirect-following path.
        # The public log fallback below gets neither this client nor its credentials.
        evm = service.evm(spec)

        class AuthenticatedRpcClient(rpc_class):
            def _post(self, payload: Any, label: str, retries: int = 3) -> Any:
                self.request_count += 1
                return _bridge(
                    loop, evm.post_payload(payload, timeout=self.timeout), self.timeout + 1
                )

        rpc_class = AuthenticatedRpcClient
    client = rpc_class(dict(lib.chains.CHAINS[spec.key]), url)
    log_client = (
        lib.rpc.RpcClient(dict(lib.chains.CHAINS[spec.key]), spec.rpc_url, timeout=20)
        if url.rstrip("/") != spec.rpc_url.rstrip("/")
        else None
    )
    skill_chain = dict(lib.chains.CHAINS[spec.key])

    def engine_prices(
        addresses: list[str],
    ) -> tuple[dict[str, float | None], list[str], float]:
        """(prices, the addresses the engine's source did not answer for, and
        the seconds until it asks the source again -- a retry sooner is
        answered with the same miss)."""
        out: dict[str, float | None] = {}
        unanswered: list[str] = []
        hold = 0.0
        try:
            infos = _bridge(loop, service.prices.prices(spec, addresses), 30.0)
        except Exception as exc:  # noqa: BLE001 - fall through to the fallback source
            log.info("trading.lp_engine_price_failed", chain=spec.key, error=str(exc))
            return out, list(addresses), hold
        for address in addresses:
            requested = NATIVE_ADDRESS if is_native(address) else normalize_address(address)
            info = infos.get(requested)
            out[address.lower()] = info.price_usd if info else None
            if info is not None and info.unavailable:
                unanswered.append(address)
                hold = max(hold, finite(info.retry_in_s) or 0.0)
        return out, unanswered, hold

    def fetch(addresses: list[str]) -> dict[str, float | None]:
        out, unanswered, hold = engine_prices(addresses)
        if unanswered:
            # Wait out the engine's hold on the miss, or the retry is that miss.
            time.sleep(max(PRICE_RETRY_DELAY_S, min(hold, PRICE_RETRY_MAX_WAIT_S)))
            again, _, _ = engine_prices(unanswered)
            out.update({k: v for k, v in again.items() if v is not None})
        missing = [a for a in addresses if out.get(a.lower()) is None]
        if missing:
            for address, price in gecko_prices(skill_chain, missing).items():
                out[address] = price
        return out

    def prices(addresses: list[str]) -> dict[str, float | None]:
        return _price_book.lookup(spec.key, addresses, fetch)

    def nft_ids(
        owner: str,
        on_page: Callable[[list[int]], None] | None = None,
        deadline: float | None = None,
    ) -> list[int] | None:
        """Blockscout's ids for ``owner``; at ``deadline`` the paging is cancelled."""
        if not spec.blockscout_url:
            return None

        def page(ids: list[int]) -> None:
            if on_page is not None:
                on_page(list(ids))

        coro = service.discovery.nft_ids(
            spec,
            owner,
            skill_chain["positionManager"],
            max_pages=INDEXER_MAX_PAGES,
            on_page=page,
        )
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        timeout = 120.0 if deadline is None else max(deadline - time.monotonic(), 0.0)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            # Every page that did arrive went to ``on_page`` already.
            return None

    def pair_tokens(pool_id: str) -> list[str]:
        return _bridge(loop, service.prices.pair_tokens(spec, pool_id), 30.0)

    vault: dict[str, str] = {}
    first_blocks: dict[str, int] = {}
    if service.vault.initialized:
        for record in service.vault.list():
            vault[record.address.lower()] = record.label
            created = (record.created_block or {}).get(str(spec.chain_id))
            if created and not record.imported:
                first_blocks[record.address.lower()] = int(created)
    return ChainEnv(
        spec=spec,
        client=client,
        prices=prices,
        lib=lib,
        nft_ids=nft_ids,
        pair_tokens=pair_tokens,
        vault=vault,
        first_blocks=first_blocks,
        log_client=log_client,
    )


def _as_trading_error(exc: Exception, spec: ChainSpec | None) -> TradingError:
    if isinstance(exc, TradingError):
        return exc
    if isinstance(exc, ValueError):
        return TradingError("trading.invalid", str(exc))
    where = f"{spec.name}: " if spec else ""
    return TradingError("trading.rpc", f"{where}{exc}" or exc.__class__.__name__)


async def _resolve_address(service: TradingService, spec: ChainSpec, value: str) -> str:
    """An address as given, or a ticker the engine resolves on this chain."""
    text = (value or "").strip()
    if not text:
        raise TradingError("trading.invalid", "a token or poolId is required")
    if _is_pool_id(text):
        return text.lower()
    if text.startswith("0x"):
        return normalize_address(text)
    meta = await service.resolve_token(spec, text)
    return meta.address


async def _read_pool_kind(
    service: TradingService,
    builder: Callable[[ChainEnv, str, str | None, int | None], dict[str, Any]],
    *,
    chain: ChainSpec | None,
    target: str,
    quote: str | None,
    fee: int | None = None,
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    target, quote = split_pair(target, quote)
    specs = [chain] if chain is not None else list(DEFAULT_CHAINS)
    last: TradingError | None = None
    for spec in specs:
        try:
            address = await _resolve_address(service, spec, target)
            quote_address = await _resolve_address(service, spec, quote) if quote else None
            env = _engine_env(service, spec, loop)

            def run(env: ChainEnv = env, a: str = address, q: str | None = quote_address) -> Any:
                return builder(start(env), a, q, fee)

            result: dict[str, Any] = await asyncio.to_thread(run)
            return result
        except Exception as exc:
            error = _as_trading_error(exc, spec)
            # With no chain named, a miss on one chain is not the answer yet.
            if chain is None and error.code in {
                "trading.lp.not_found",
                "trading.lp.pool_key_unknown",
                "trading.invalid",
                "trading.unknown_token",
            }:
                last = last or error
                continue
            raise error from exc
    assert last is not None
    raise last


async def lp_pool(
    service: TradingService,
    *,
    chain: ChainSpec | None,
    target: str,
    quote: str | None = None,
    fee: int | None = None,
) -> dict[str, Any]:
    """``kind: "pool"``: reserves, launcher/lock status and the biggest ranges.

    ``target`` may be a ``TOKEN/QUOTE`` pair; ``fee`` (``parse_fee``) picks a tier.
    """
    return await _read_pool_kind(
        service, build_pool, chain=chain, target=target, quote=quote, fee=fee
    )


async def lp_ranges(
    service: TradingService,
    *,
    chain: ChainSpec | None,
    target: str,
    quote: str | None = None,
    fee: int | None = None,
) -> dict[str, Any]:
    """``kind: "ranges"``: the pool's liquidity distribution, segment by segment."""
    return await _read_pool_kind(
        service, build_ranges, chain=chain, target=target, quote=quote, fee=fee
    )


async def lp_position(
    service: TradingService, *, chain: ChainSpec, token_id: int
) -> dict[str, Any]:
    """``kind: "position"``: one position NFT, wherever it is held."""
    if token_id <= 0:
        raise TradingError("trading.invalid", "tokenId must be a positive integer")
    loop = asyncio.get_running_loop()
    env = _engine_env(service, chain, loop)
    try:
        result: dict[str, Any] = await asyncio.to_thread(
            lambda: build_position(start(env), token_id)
        )
    except Exception as exc:
        raise _as_trading_error(exc, chain) from exc
    return result


async def _refuse_token_addresses(envs: list[ChainEnv], addresses: list[str]) -> None:
    """Raise ``trading.lp.not_a_wallet`` when a ``--wallet`` is a token contract."""

    async def check(env: ChainEnv) -> dict[str, str]:
        try:
            return await asyncio.to_thread(token_contracts, env, addresses)
        except Exception as exc:  # noqa: BLE001 - a failed check must not block the scan
            log.debug("trading.lp_wallet_check_failed", chain=env.spec.key, error=str(exc))
            return {}

    found = await asyncio.gather(*(check(env) for env in envs))
    for env, tokens in zip(envs, found, strict=True):
        for address, symbol in tokens.items():
            checksummed = unilp().hexutil.checksum_address(address)
            raise TradingError(
                "trading.lp.not_a_wallet",
                f"{checksummed} is a token contract ({symbol}); use "
                f"`agentos trade lp pool {checksummed} --chain {env.spec.key}` for its liquidity",
                details={
                    "address": checksummed,
                    "symbol": symbol,
                    "chainId": env.spec.chain_id,
                },
            )


def positions_budget(budget_s: float | None) -> float:
    """The ``lp positions`` budget in seconds: the default, or ``budget_s`` bounded."""
    if budget_s is None:
        return POSITIONS_BUDGET_S
    value = finite(budget_s)
    if value is None or not POSITIONS_BUDGET_MIN_S <= value <= POSITIONS_BUDGET_MAX_S:
        raise TradingError(
            "trading.invalid",
            f"budget must be {POSITIONS_BUDGET_MIN_S:g}-{POSITIONS_BUDGET_MAX_S:g} seconds",
        )
    return value


async def lp_positions(
    service: TradingService,
    *,
    chains: list[ChainSpec] | None = None,
    wallets: list[str] | None = None,
    include_closed: bool = False,
    budget_s: float | None = None,
) -> dict[str, Any]:
    """``kind: "positions"``: every V4 position of the vault's (or the named) wallets.

    Bounded by ``budget_s`` (default ``POSITIONS_BUDGET_S``) of wall clock:
    discovery stops early enough to leave time for pricing and the card, and
    what it could not reach is named in the warnings of a partial card.
    """
    started = time.monotonic()
    budget = positions_budget(budget_s)
    reserve = min(
        max(budget * POSITIONS_RESERVE_SHARE, POSITIONS_RESERVE_MIN_S), POSITIONS_RESERVE_MAX_S
    )
    loop = asyncio.get_running_loop()
    specs = list(dict.fromkeys(chains or list(CHAINS.values())))
    vault = (
        {r.address.lower(): r.label for r in service.vault.list()}
        if service.vault.initialized
        else {}
    )
    owners: list[str] = []
    for value in wallets or []:
        text = str(value).strip()
        if not text:
            continue
        if text.startswith("0x"):
            owners.append(normalize_address(text))
        else:
            owners.append(service.vault.resolve(text).address.lower())
    warnings: list[str] = []
    if not wallets:
        owners = list(vault)
        if not owners:
            warnings.append("no wallets in the vault; pass --wallet to read any address")
    owners = list(dict.fromkeys(owners))
    wallet_rows = [
        {
            "address": unilp().hexutil.checksum_address(o),
            "label": vault.get(o),
            "inApp": o in vault,
        }
        for o in owners
    ]

    envs_by_key = {spec.key: _engine_env(service, spec, loop) for spec in specs}
    for env in envs_by_key.values():
        env.deadline = started + budget - reserve
        env.budget_end = started + budget
        env.budget_s = budget
    named = [o for o in owners if o not in vault]
    if named:
        await _refuse_token_addresses(list(envs_by_key.values()), named)

    async def one(spec: ChainSpec) -> tuple[ChainEnv, list[dict[str, Any]]]:
        env = envs_by_key[spec.key]
        if not owners:
            env.fetched_at = _now_iso()
            return env, []
        work = asyncio.ensure_future(
            asyncio.to_thread(lambda: build_chain_positions(start(env), owners, include_closed))
        )
        hard = started + budget + POSITIONS_HARD_GRACE_S - time.monotonic()
        try:
            rows = await asyncio.wait_for(asyncio.shield(work), timeout=max(hard, 0.5))
        except TimeoutError:
            # The worker thread cannot be interrupted: answer with what it published.
            env.partial = True
            env.warn(
                f"{spec.name} did not answer within the {budget:g} s time budget; "
                "pass --budget-seconds to wait longer"
            )
            salvage = asyncio.ensure_future(
                asyncio.to_thread(salvage_positions, env, include_closed)
            )
            try:
                return env, await asyncio.wait_for(salvage, timeout=POSITIONS_SALVAGE_S)
            except Exception as exc:  # noqa: BLE001 - the budget warning already says why
                log.info("trading.lp_salvage_failed", chain=spec.key, error=repr(exc))
                found = env.progress.found or []
                if found:
                    env.warn(
                        f"{spec.name}: {len(found)} position(s) were found but their pools "
                        "could not be read in time"
                    )
                return env, []
        except Exception as exc:  # noqa: BLE001 - one chain down still leaves the other
            error = _as_trading_error(exc, spec)
            env.partial = True
            env.warn(f"{spec.name} could not be read: {error}")
            return env, []
        return env, rows

    done = await asyncio.gather(*(one(spec) for spec in specs))
    envs = [env for env, _ in done]
    if owners and all(env.block == 0 for env in envs):
        raise TradingError(
            "trading.rpc", "; ".join(w for env in envs for w in env.warnings) or "RPC down"
        )
    positions = [row for _, rows in done for row in rows]
    return positions_payload(envs, wallet_rows, positions, warnings)
