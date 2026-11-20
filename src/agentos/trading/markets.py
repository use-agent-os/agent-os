"""Every pool a token trades in, on every DEX (``application/vnd.agentos.markets+json``).

``docs/markets.md`` is the contract. On Robinhood Chain a Stock Token is also
the *quote asset* of hundreds of launchpad tokens (``AI/NVDA`` on Bankr), so
the read splits a token's pools in two: ``quote`` (the token is the pool's
quote: *priced in NVDA*) and ``base`` (the token is the base: *NVDA priced in*
USDG, WETH …).

Sources, in order:

* **GeckoTerminal** ``/networks/{net}/tokens/{address}/pools`` — the only
  listing that returns pools on both sides of a token. 20 pools a page, 30
  requests a minute per IP, so pages are read in small concurrent bursts and
  the raw pages are cached for :data:`PAGE_CACHE_TTL_S` per ``(chain, token)``.
* **DexScreener** ``/token-pairs/v1/{slug}/{address}`` — enrichment only
  (version label, liquidity and volume where GeckoTerminal left a gap). It
  lists the base side alone, which is why it cannot be the source.
* **The chain** — for a Stock Token on Robinhood Chain, the Chainlink feed
  price and ``oraclePaused()`` (ported from the ``robinhood-chain-stocks``
  skill). Any failure there is ``oracle: null``, never an error.

Prices and amounts that the sources do not give are ``null``, never ``0``.
"""

from __future__ import annotations

import asyncio
import html
import math
import re
import time
import weakref
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from agentos.trading.chains import NATIVE_ADDRESS, ChainSpec, is_native, normalize_address
from agentos.trading.evm import USER_AGENT, EvmRpcError, EvmTransportError
from agentos.trading.prices import (
    DEXSCREENER_BASE,
    GECKOTERMINAL_BASE,
    TokenMeta,
    is_stock_token_name,
)
from agentos.trading.service import TradingError

if TYPE_CHECKING:  # pragma: no cover
    from agentos.trading.service import TradingService

MARKETS_MIME = "application/vnd.agentos.markets+json"
PAYLOAD_VERSION = 1
ROBINHOOD_CHAIN_ID = 4663

#: GeckoTerminal answers 20 pools a page.
PAGE_SIZE = 20
#: Pages read by default, and with ``deep``.
PAGE_CAP = 5
DEEP_PAGE_CAP = 10
#: Pages read concurrently in one go (see ``_fill_pages``).
PAGE_BURST = 5
#: How long the raw pages of one ``(chain, token)`` are reused.
PAGE_CACHE_TTL_S = 120.0
#: Per-page timeout: a page GeckoTerminal's edge has not cached was measured
#: at up to 18.5 s, and five of them in one burst take longer still.
PAGE_TIMEOUT_S = 60.0
#: Wait before the one retry of pages that drew a 429 inside a burst.
#: GeckoTerminal's limit bites before its stated 30 / minute (a 429 after ~11
#: requests in 90 s was seen); a second 429 ends the read.
RATE_LIMIT_RETRY_S = 15.0
GECKOTERMINAL_ACCEPT = "application/json;version=20230302"
GECKOTERMINAL_WEB = "https://www.geckoterminal.com"

SIDES = ("all", "quote", "base")
DEFAULT_MIN_TVL_USD = 10_000.0
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

#: The Chainlink feed directory for Robinhood Chain mainnet.
FEEDS_URL = "https://reference-data-directory.vercel.app/feeds-robinhood-mainnet.json"
FEEDS_TTL_S = 3600.0
#: Chainlink stock feeds answer with 8 decimals.
ORACLE_DECIMALS = 8
SEL_LATEST_ROUND_DATA = "0xfeaf968c"  # latestRoundData()
SEL_ORACLE_PAUSED = "0x7706ba52"  # oraclePaused()

#: DEX id prefix -> the launchpad behind the pool (best effort).
LAUNCHERS: dict[str, str] = {
    "bankr": "Bankr",
    "pons": "Pons",
    "clanker": "Clanker",
    "long": "long.xyz",
    "virtuals": "Virtuals",
}

# The Stock Token suffix, strict and truncation-tolerant (a registry that
# caps a name at 60 characters hands over "… • Robinhood Toke"). The same regexes as the
# ``robinhood-chain-stocks`` skill, used here only to *strip* a suffix when a
# lookalike's name is compared; whether a name is a Stock Token's is
# ``prices.is_stock_token_name`` (the bullet is what makes one).
_RH_SUFFIX_RE = re.compile(r"\s*[•·|-]?\s*robinhood token\s*$", re.IGNORECASE)
_RH_SUFFIX_LOOSE_RE = re.compile(
    r"\s*[•·|-]\s*r(?:o(?:b(?:i(?:n(?:h(?:o(?:o(?:d)?)?)?)?)?)?)?)?"
    r"(?:\s+t(?:o(?:k(?:e(?:n)?)?)?)?)?\s*$",
    re.IGNORECASE,
)
_FEE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*$")
_VERSION_RE = re.compile(r"(?:^|-)v(\d)(?:-|$)", re.IGNORECASE)
#: A v4 pool is addressed by its 32-byte poolId, not a 20-byte contract.
_POOL_ID_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_DEX_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")
_DEX_VERSION_RE = re.compile(r"\s+v\d+(?:\.\d+)?\b.*$", re.IGNORECASE)


def is_stock_name(name: str | None) -> bool:
    """A Robinhood Stock Token's list name (``prices.is_stock_token_name``)."""
    return is_stock_token_name(name or "")


def _bare_name(name: str | None) -> str:
    """The company part of a name, lower-cased with whitespace collapsed."""
    text = html.unescape(name or "")
    stripped = _RH_SUFFIX_RE.sub("", text).strip()
    if stripped == text.strip():
        stripped = _RH_SUFFIX_LOOSE_RE.sub("", stripped).strip()
    return " ".join(stripped.lower().split())


def _num(value: Any) -> float | None:
    """A finite number from a source string, or ``None`` (never NaN, never a guess)."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _pos(value: Any) -> float | None:
    number = _num(value)
    return number if number is not None and number > 0 else None


def _int(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None and number >= 0 else None


def parse_fee_pct(name: str | None) -> float | None:
    """``"NVDA / USDG 0.01%"`` -> ``0.01``; ``None`` when the name carries no fee."""
    match = _FEE_RE.search(name or "")
    return _num(match.group(1)) if match else None


def launcher_for(dex_id: str | None) -> str | None:
    head = (dex_id or "").strip().lower().split("-", 1)[0]
    return LAUNCHERS.get(head)


def dex_version(
    dex_id: str | None, labels: Any = None, pool_address: str | None = None
) -> str | None:
    """DexScreener's label first; else ``v4`` for a 32-byte poolId; else the DEX id's.

    ``bankr-robinhood`` carries no version, but its pools are v4 poolIds (so
    are Pons V2's: the launchpad's "v2" is not the AMM's). A 20-byte pool
    falls back to the DEX id: ``uniswap-v3-robinhood`` -> ``v3``.
    """
    for label in labels or []:
        text = str(label).strip().lower()
        if re.fullmatch(r"v\d", text):
            return text
    if _POOL_ID_RE.match(pool_address or ""):
        return "v4"
    match = _VERSION_RE.search(dex_id or "")
    if match:
        return f"v{match.group(1)}"
    return None


def dex_label(dex_id: str, name: str | None) -> str:
    """``"Uniswap V4 (Robinhood)"`` -> ``"Uniswap"``; ``"Bankr (Robinhood)"`` -> ``"Bankr"``."""
    text = html.unescape(name or "").strip()
    text = _DEX_VERSION_RE.sub("", _DEX_SUFFIX_RE.sub("", text)).strip()
    if text:
        return text
    head = dex_id.split("-", 1)[0]
    return head[:1].upper() + head[1:] if head else dex_id


def _premium_pct(price_usd: float, oracle_usd: float) -> float:
    """``(price / oracle - 1) * 100`` to 2 decimals, never ``-0.0``."""
    premium = (price_usd / oracle_usd - 1) * 100
    return 0.0 if abs(premium) < 0.005 else round(premium, 2)


def _iso(ts: float | int) -> str:
    return datetime.fromtimestamp(float(ts), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _d(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _address_from_id(raw_id: Any) -> str | None:
    """``"robinhood_0xabc…"`` -> ``"0xabc…"`` (lower-case), or ``None``."""
    text = str(raw_id or "")
    tail = text.rsplit("_", 1)[-1]
    try:
        return normalize_address(tail.lower())
    except ValueError:
        return None


# ── page cache ──────────────────────────────────────────────────────────


@dataclass
class _Pages:
    """The raw GeckoTerminal pages of one ``(chain, token)``, oldest first."""

    at: float
    pages: list[dict[str, Any]] = field(default_factory=list)
    #: A short page was read: there is nothing past ``pages``.
    complete: bool = False
    #: DexScreener's pairs, keyed by lower-case pair address (``None``: not read yet).
    dexscreener: dict[str, dict[str, Any]] | None = None
    dexscreener_failed: bool = False


_CACHES: weakref.WeakKeyDictionary[Any, dict[tuple[int, str], _Pages]] = weakref.WeakKeyDictionary()
_FEEDS: weakref.WeakKeyDictionary[Any, tuple[float, list[dict[str, Any]]]] = (
    weakref.WeakKeyDictionary()
)


def _now(service: Any) -> float:
    clock = getattr(service, "_now", None)
    return float(clock()) if callable(clock) else time.time()


def _cache(service: Any) -> dict[tuple[int, str], _Pages]:
    try:
        return _CACHES.setdefault(service, {})
    except TypeError:  # pragma: no cover - an unhashable test double
        return {}


def clear_cache() -> None:
    """Forget every cached page (tests)."""
    _CACHES.clear()
    _FEEDS.clear()


class _RateLimitedError(Exception):
    pass


class _UnavailableError(Exception):
    pass


class _TimedOutError(_UnavailableError):
    """The page did not answer within :data:`PAGE_TIMEOUT_S` (retried once)."""


async def _fetch_page(
    service: Any, chain: ChainSpec, address: str, page: int
) -> dict[str, Any] | None:
    """One page; ``None`` for a token GeckoTerminal does not index (404)."""
    http: httpx.AsyncClient = service.prices._http
    url = f"{GECKOTERMINAL_BASE}/networks/{chain.coingecko_platform}/tokens/{address}/pools"
    params: dict[str, str | int] = {
        "page": page,
        "include": "base_token,quote_token,dex",
        "sort": "h24_volume_usd_desc",
    }
    try:
        response = await http.get(
            url,
            params=params,
            headers={"accept": GECKOTERMINAL_ACCEPT, "user-agent": USER_AGENT},
            timeout=PAGE_TIMEOUT_S,
        )
    except httpx.TimeoutException as exc:
        detail = str(exc) or exc.__class__.__name__
        raise _TimedOutError(f"GeckoTerminal did not answer: {detail}") from exc
    except httpx.HTTPError as exc:
        detail = str(exc) or exc.__class__.__name__
        raise _UnavailableError(f"GeckoTerminal did not answer: {detail}") from exc
    if response.status_code == 429:
        raise _RateLimitedError
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise _UnavailableError(f"GeckoTerminal answered HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise _UnavailableError("GeckoTerminal answered with a body that is not JSON") from exc
    if not isinstance(body, dict):
        raise _UnavailableError("GeckoTerminal answered with an unexpected body")
    return body


def _entry(service: Any, chain: ChainSpec, address: str) -> _Pages:
    """The cached pages of ``(chain, address)``, or a fresh empty entry."""
    cache = _cache(service)
    key = (chain.chain_id, address)
    now = _now(service)
    entry = cache.get(key)
    if entry is None or now - entry.at >= PAGE_CACHE_TTL_S:
        entry = _Pages(at=now)
        cache[key] = entry
    return entry


async def _fill_pages(
    service: Any, chain: ChainSpec, address: str, entry: _Pages, upto: int
) -> tuple[bool, str | None]:
    """Read every page after ``entry.pages`` up to ``upto``, in one concurrent burst.

    A page GeckoTerminal has not cached at its edge takes 11-13 s (measured
    2026-10-06: ``cf-cache-status: EXPIRED`` 13 s, ``HIT`` 0.2 s), so reading
    one page at a time cost 40 s for a token; a burst of five costs one page's
    wait and stays well inside the 30 requests / minute limit. An uncached page
    can still take 18.5 s, so a page that timed out inside the burst is asked
    once more after it (only when an earlier page did not already end the
    prefix). Pages that drew a 429 are asked once more after
    :data:`RATE_LIMIT_RETRY_S` (concurrently with the timeout retry). Pages
    are kept in order and only as a contiguous prefix: a second 429 or an
    outage drops that page and every later one. Returns
    ``(rate_limited, failure)``.
    """
    first = len(entry.pages) + 1
    wanted = list(range(first, upto + 1))
    if entry.complete or not wanted:
        return False, None
    results: list[Any] = list(
        await asyncio.gather(
            *(_fetch_page(service, chain, address, page) for page in wanted),
            return_exceptions=True,
        )
    )
    timed_out: list[int] = []
    limited: list[int] = []
    for index, body in enumerate(results):
        if isinstance(body, _TimedOutError):
            timed_out.append(index)
            continue
        if isinstance(body, _RateLimitedError):
            limited.append(index)
            continue
        if isinstance(body, BaseException) or len(_rows_of(body)) < PAGE_SIZE:
            break  # the prefix ends here: a later page is never kept

    async def again(indices: list[int], wait: float) -> list[Any]:
        if not indices:
            return []
        if wait > 0:
            await asyncio.sleep(wait)
        return list(
            await asyncio.gather(
                *(_fetch_page(service, chain, address, wanted[i]) for i in indices),
                return_exceptions=True,
            )
        )

    if timed_out or limited:
        retried, waited = await asyncio.gather(
            again(timed_out, 0.0), again(limited, RATE_LIMIT_RETRY_S)
        )
        for index, body in zip(timed_out + limited, retried + waited, strict=True):
            results[index] = body
    for body in results:
        if isinstance(body, _RateLimitedError):
            return True, None
        if isinstance(body, _UnavailableError):
            return False, str(body)
        if isinstance(body, BaseException):
            raise body
        entry.pages.append(body or {"data": [], "included": []})
        if len(_rows_of(body)) < PAGE_SIZE:
            entry.complete = True
            break
    return False, None


def _rows_of(body: dict[str, Any] | None) -> list[Any]:
    data = (body or {}).get("data")
    return data if isinstance(data, list) else []


async def _dexscreener(service: Any, chain: ChainSpec, address: str, entry: _Pages) -> bool:
    """Fill ``entry.dexscreener`` once per cache life; ``False`` when it failed."""
    if entry.dexscreener is not None:
        return not entry.dexscreener_failed
    body = await service.prices._get(
        f"{DEXSCREENER_BASE}/token-pairs/v1/{chain.dexscreener_slug}/{address}"
    )
    pairs = body if isinstance(body, list) else (body or {}).get("pairs") if body else None
    if not isinstance(pairs, list):
        entry.dexscreener = {}
        entry.dexscreener_failed = True
        return False
    out: dict[str, dict[str, Any]] = {}
    for pair in pairs:
        if isinstance(pair, dict) and pair.get("pairAddress"):
            out[str(pair["pairAddress"]).lower()] = pair
    entry.dexscreener = out
    return True


# ── oracle ──────────────────────────────────────────────────────────────


def _feed_ticker(feed: dict[str, Any]) -> str:
    docs = feed.get("docs")
    if isinstance(docs, dict):
        base = docs.get("baseAsset")
        if isinstance(base, str) and base.strip():
            return base.strip().upper()
    name = str(feed.get("name", ""))
    if not name.lower().startswith("robinhood"):
        return ""
    rest = name[len("robinhood") :].strip()
    return re.split(r"\s*[/-]\s*", rest, maxsplit=1)[0].strip().upper()


async def _feeds(service: Any) -> list[dict[str, Any]]:
    now = _now(service)
    try:
        cached = _FEEDS.get(service)
    except TypeError:  # pragma: no cover
        cached = None
    if cached is not None and now - cached[0] < FEEDS_TTL_S:
        return cached[1]
    body = await service.prices._get(FEEDS_URL)
    feeds = [f for f in body if isinstance(f, dict)] if isinstance(body, list) else []
    if feeds:
        try:
            _FEEDS[service] = (now, feeds)
        except TypeError:  # pragma: no cover
            pass
    return feeds


def _word(raw: str, index: int, *, signed: bool = False) -> int:
    body = raw.removeprefix("0x")
    chunk = body[index * 64 : (index + 1) * 64]
    if len(chunk) != 64:
        raise ValueError("short ABI answer")
    value = int(chunk, 16)
    if signed and value >= 2**255:
        value -= 2**256
    return value


async def read_oracle(service: Any, chain: ChainSpec, token: TokenMeta) -> dict[str, Any] | None:
    """The Chainlink price of a Stock Token on Robinhood Chain; ``None`` on any failure."""
    if chain.chain_id != ROBINHOOD_CHAIN_ID or not token.stock_token:
        return None
    try:
        feeds = await _feeds(service)
        symbol = token.symbol.strip().upper()
        feed = next((f for f in feeds if _feed_ticker(f) == symbol and f.get("proxyAddress")), None)
        if feed is None:
            return None
        evm = service.evm(chain)
        raw = await evm.eth_call(str(feed["proxyAddress"]), SEL_LATEST_ROUND_DATA)
        answer = _word(raw, 1, signed=True)
        updated_at = _word(raw, 3)
        if answer <= 0 or updated_at <= 0:
            return None
        paused: bool | None = None
        try:
            paused = bool(_word(await evm.eth_call(token.address, SEL_ORACLE_PAUSED), 0))
        except (EvmRpcError, EvmTransportError, ValueError):
            paused = None
        age = max(0, int(_now(service) - updated_at))
        heartbeat = _int(feed.get("heartbeat"))
        stale = bool((heartbeat is not None and age > heartbeat) or paused)
        return {
            "usd": answer / 10**ORACLE_DECIMALS,
            "updatedAt": _iso(updated_at),
            "ageSeconds": age,
            "stale": stale,
            "paused": bool(paused),
        }
    except (EvmRpcError, EvmTransportError, ValueError, TypeError, KeyError, httpx.HTTPError):
        return None


# ── rows ────────────────────────────────────────────────────────────────


@dataclass
class _Included:
    tokens: dict[str, dict[str, Any]]  # address -> attributes
    dexes: dict[str, str]  # dex id -> name


def _included(pages: list[dict[str, Any]]) -> _Included:
    tokens: dict[str, dict[str, Any]] = {}
    dexes: dict[str, str] = {}
    for page in pages:
        for item in page.get("included") or []:
            if not isinstance(item, dict):
                continue
            attrs = _d(item.get("attributes"))
            if item.get("type") == "token":
                address = _address_from_id(attrs.get("address") or item.get("id"))
                if address:
                    tokens[address] = attrs
            elif item.get("type") == "dex":
                dexes[str(item.get("id") or "")] = str(attrs.get("name") or "")
    return _Included(tokens, dexes)


def _rel_id(pool: dict[str, Any], name: str) -> str:
    data = _d(_d(_d(pool.get("relationships")).get(name)).get("data"))
    return str(data.get("id") or "")


async def _stock_tokens(service: Any, chain: ChainSpec) -> dict[str, TokenMeta]:
    """The chain's Stock Tokens from its token list, by address."""
    try:
        listing = await service.prices.token_list(chain)
    except Exception:  # noqa: BLE001 - a missing list only means no lookalike check
        return {}
    if chain.key != "robinhood":
        return {}
    return {a: m for a, m in listing.items() if m.stock_token or is_stock_name(m.name)}


async def _counterparty(
    service: Any,
    chain: ChainSpec,
    address: str,
    included: _Included,
    stocks: dict[str, TokenMeta],
    stock_symbols: set[str],
    stock_names: set[str],
    known_cache: dict[str, TokenMeta | None],
) -> dict[str, Any]:
    attrs = included.tokens.get(address, {})
    if address not in known_cache:
        try:
            known_cache[address] = await service.prices.known_token(chain, address)
        except Exception:  # noqa: BLE001 - metadata is best effort
            known_cache[address] = None
    known = known_cache[address]
    native = is_native(address)
    symbol = html.unescape(str(attrs.get("symbol") or (known.symbol if known else "") or ""))
    name = html.unescape(str(attrs.get("name") or (known.name if known else "") or ""))
    decimals = _int(attrs.get("decimals"))
    if decimals is None and known is not None:
        decimals = known.decimals
    if native:
        # GeckoTerminal labels the zero address "WETH"; it is the chain's coin.
        symbol, name, decimals = "ETH", "Ether", 18
    logo = str(attrs.get("image_url") or "") or (known.logo_url if known else None) or None
    if logo and "missing" in logo.rsplit("/", 1)[-1]:
        logo = known.logo_url if known else None
    stock = address in stocks or bool(
        known and chain.key == "robinhood" and (known.stock_token or is_stock_name(known.name))
    )
    lookalike = (
        not stock
        and not native
        and (
            (symbol.strip().lower() in stock_symbols and bool(symbol.strip()))
            or (bool(_bare_name(name)) and _bare_name(name) in stock_names)
        )
    )
    return {
        "address": address,
        "symbol": symbol or None,
        "name": name or None,
        "decimals": decimals,
        "logoUrl": logo,
        "verified": bool(known is not None and known.verified) or native,
        "stockToken": stock,
        "lookalike": lookalike,
        "native": native,
    }


def _ds_number(pair: dict[str, Any] | None, *path: str) -> float | None:
    node: Any = pair
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return _num(node)


async def markets(
    service: TradingService,
    *,
    chain: ChainSpec,
    target: str,
    side: str = "all",
    min_tvl_usd: float = DEFAULT_MIN_TVL_USD,
    limit: int = DEFAULT_LIMIT,
    lookalikes: bool = False,
    deep: bool = False,
) -> dict[str, Any]:
    """Every pool ``target`` trades in on ``chain``, split by side (``docs/markets.md``)."""
    side = (side or "all").strip().lower()
    if side not in SIDES:
        raise TradingError("trading.invalid", "side must be all, quote or base")
    min_tvl = _num(min_tvl_usd)
    if min_tvl is None or min_tvl < 0:
        raise TradingError("trading.invalid", "minTvlUsd must be a number of dollars, 0 or more")
    if isinstance(limit, bool) or not 1 <= int(limit) <= MAX_LIMIT:
        raise TradingError("trading.invalid", f"limit must be between 1 and {MAX_LIMIT}")
    limit = int(limit)
    if not chain.coingecko_platform:
        raise TradingError(
            "trading.unsupported_chain", f"{chain.name} has no market listing source"
        )

    try:
        token = await service.resolve_token(chain, target)
    except TradingError as exc:
        if exc.code == "trading.unknown_token" or (
            exc.code == "trading.invalid" and str(exc).startswith("Unknown token symbol")
        ):
            raise TradingError("trading.not_found", str(exc), details=exc.details) from exc
        raise

    lookup = token.address
    if token.native:
        weth = (chain.weth or "").lower()
        if not weth:
            hits = await service.prices.find_by_symbol(chain, "WETH")
            weth = hits[0].address if hits else ""
        lookup = weth or NATIVE_ADDRESS

    stocks = await _stock_tokens(service, chain)
    stock_symbols = {m.symbol.strip().lower() for m in stocks.values() if m.symbol.strip()}
    stock_names = {_bare_name(m.name) for m in stocks.values() if _bare_name(m.name)}
    is_stock = token.stock_token or (chain.key == "robinhood" and is_stock_name(token.name))
    if is_stock and not token.stock_token:
        token = replace(token, stock_token=True)

    cap = DEEP_PAGE_CAP if deep else PAGE_CAP
    oracle = await read_oracle(service, chain, token)
    known_cache: dict[str, TokenMeta | None] = {}
    state: dict[str, int] = {"seen": 0, "below": 0, "hidden": 0}

    async def survivors(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        included = _included(pages)
        rows: list[dict[str, Any]] = []
        below = hidden = scanned = 0
        for page in pages:
            for pool in page.get("data") or []:
                if not isinstance(pool, dict):
                    continue
                scanned += 1
                row = await _row(pool, included)
                if row is None:
                    continue
                if row == "below":
                    below += 1
                    continue
                if row == "lookalike":
                    hidden += 1
                    continue
                assert isinstance(row, dict)
                rows.append(row)
        state.update(seen=scanned, below=below, hidden=hidden)
        return rows

    ds_pairs: dict[str, dict[str, Any]] = {}

    async def _row(pool: dict[str, Any], included: _Included) -> dict[str, Any] | str | None:
        attrs = _d(pool.get("attributes"))
        base = _address_from_id(_rel_id(pool, "base_token"))
        quote = _address_from_id(_rel_id(pool, "quote_token"))
        if base == lookup:
            row_side, other = "base", quote
        elif quote == lookup:
            row_side, other = "quote", base
        else:
            return None
        if other is None or (side != "all" and side != row_side):
            return None
        pool_address = str(attrs.get("address") or "").lower() or None
        ds = ds_pairs.get(pool_address or "")
        tvl = _num(attrs.get("reserve_in_usd"))
        if tvl is None:
            tvl = _ds_number(ds, "liquidity", "usd")
        if (tvl is None and min_tvl > 0) or (tvl is not None and tvl < min_tvl):
            return "below"
        counterparty = await _counterparty(
            service, chain, other, included, stocks, stock_symbols, stock_names, known_cache
        )
        if counterparty["lookalike"] and not lookalikes:
            return "lookalike"
        dex_id = _rel_id(pool, "dex")
        volume = _num(_d(attrs.get("volume_usd")).get("h24"))
        if volume is None:
            volume = _ds_number(ds, "volume", "h24")
        txns = _d(attrs.get("transactions")).get("h24")
        buys = _int(txns.get("buys")) if isinstance(txns, dict) else None
        sells = _int(txns.get("sells")) if isinstance(txns, dict) else None
        # The pool's base token is the counterparty on quote rows and the
        # token on base rows; either way the row's price is the base's, and
        # ``priceInToken`` is the base priced in the quote (AI in NVDA on a
        # quote row, NVDA in USDG on a base row). It is the ratio of the two
        # USD prices: GeckoTerminal's own ``base_token_price_quote_token`` was
        # 45-77 % off real quotes on launchpad pools (2026-10-06), so it is
        # only the fallback when a USD price is missing.
        price_usd = _pos(attrs.get("base_token_price_usd"))
        quote_usd = _pos(attrs.get("quote_token_price_usd"))
        price_in = price_usd / quote_usd if price_usd and quote_usd else None
        if price_in is None:
            price_in = _pos(attrs.get("base_token_price_quote_token"))
        if price_in is None:
            inverse = _pos(attrs.get("quote_token_price_base_token"))
            price_in = 1 / inverse if inverse else None
        premium = None
        oracle_usd = (oracle or {}).get("usd")
        if row_side == "base" and price_usd is not None and oracle_usd:
            premium = _premium_pct(price_usd, float(oracle_usd))
        token_symbol = token.symbol or "?"
        other_symbol = counterparty["symbol"] or "?"
        pair = (
            f"{other_symbol}/{token_symbol}"
            if row_side == "quote"
            else f"{token_symbol}/{other_symbol}"
        )
        return {
            "poolAddress": pool_address,
            "pair": pair,
            "side": row_side,
            "dex": {
                "id": dex_id or None,
                "label": dex_label(dex_id, included.dexes.get(dex_id)) if dex_id else None,
                "version": dex_version(dex_id, (ds or {}).get("labels"), pool_address),
            },
            "launcher": launcher_for(dex_id),
            "viaUniswap": dex_id.lower().startswith("uniswap-"),
            "feePct": parse_fee_pct(str(attrs.get("name") or "")),
            "counterparty": counterparty,
            "tvlUsd": tvl,
            "volume24hUsd": volume,
            "txns24h": (
                {"buys": buys, "sells": sells} if buys is not None and sells is not None else None
            ),
            "priceUsd": price_usd,
            "priceInToken": price_in,
            "change24hPct": _num(_d(attrs.get("price_change_percentage")).get("h24")),
            "premiumPct": premium,
            "createdAt": str(attrs.get("pool_created_at") or "") or None,
            "url": (
                f"{GECKOTERMINAL_WEB}/{chain.coingecko_platform}/pools/{pool_address}"
                if pool_address
                else None
            ),
            "swap": {
                "chainId": chain.chain_id,
                "tokenIn": token.address,
                "tokenOut": counterparty["address"],
            },
        }

    warnings: list[str] = []
    entry = _entry(service, chain, lookup)
    rate_limited = False
    failure: str | None = None
    rows: list[dict[str, Any]] = []
    while True:
        pages = entry.pages[:cap]
        rows = await survivors(pages)
        if len(rows) >= limit or len(pages) >= cap or entry.complete:
            break
        rate_limited, failure = await _fill_pages(
            service, chain, lookup, entry, min(cap, len(entry.pages) + PAGE_BURST)
        )
        if rate_limited or failure is not None:
            break
    pages = entry.pages[:cap]
    if not pages:
        _cache(service).pop((chain.chain_id, lookup), None)
        reason = (
            "GeckoTerminal rate limit: no pools could be read; try again in a minute"
            if rate_limited
            else f"Could not read the pools of {token.symbol or lookup} on {chain.name}: {failure}"
        )
        raise TradingError(
            "trading.markets.unavailable",
            reason,
            details={
                "chainId": chain.chain_id,
                "token": token.address,
                "rateLimited": rate_limited,
            },
        )

    if not await _dexscreener(service, chain, lookup, entry):
        warnings.append("DexScreener did not answer: versions and gaps not filled from it")
    ds_pairs.update(entry.dexscreener or {})
    rows = await survivors(pages)

    scanned = int(state["seen"])
    partial = False
    # The page cap, not ``limit``, ended a read GeckoTerminal had more for.
    page_cap_hit = (
        not rate_limited
        and failure is None
        and len(rows) < limit
        and len(pages) >= cap
        and not (entry.complete and len(entry.pages) <= cap)
    )
    if rate_limited or failure is not None:
        partial = True
        source = "rate limit" if rate_limited else "stopped answering"
        warnings.insert(0, f"GeckoTerminal {source}: showing the first {scanned} pools")
    elif page_cap_hit:
        partial = True
        more = "" if deep else "; deep reads up to 200"
        warnings.insert(0, f"Read the first {scanned} pools (page cap){more}")

    def tvl_key(row: dict[str, Any]) -> float:
        value = row.get("tvlUsd")
        return float(value) if isinstance(value, int | float) else -1.0

    rows.sort(key=tvl_key, reverse=True)
    limited = max(0, len(rows) - limit)
    rows = rows[:limit]
    quote_rows = [r for r in rows if r["side"] == "quote"]
    base_rows = [r for r in rows if r["side"] == "base"]

    included = _included(pages)
    own = included.tokens.get(lookup, {})
    token_logo = token.logo_url or (str(own.get("image_url") or "") or None)
    try:
        token_price = await service.prices.price(chain, token.address)
    except Exception:  # noqa: BLE001 - a price miss is null, never an error
        token_price = None

    return {
        "version": PAYLOAD_VERSION,
        "kind": "markets",
        "chain": {
            "id": chain.chain_id,
            "key": chain.key,
            "name": chain.name,
            "explorer": chain.explorer_url,
        },
        "fetchedAt": _iso(entry.at),
        "partial": partial,
        "warnings": warnings,
        "token": {
            "address": token.address,
            "symbol": token.symbol,
            "name": token.name,
            "decimals": token.decimals,
            "logoUrl": token_logo,
            "verified": token.verified,
            "stockToken": token.stock_token,
            "priceUsd": token_price,
            "oracle": oracle,
        },
        "counts": {
            "scanned": scanned,
            "shown": len(quote_rows) + len(base_rows),
            "belowMinTvl": int(state["below"]),
            "hiddenLookalikes": int(state["hidden"]),
            "limited": limited,
            "pages": len(pages),
            "pageCap": cap,
            "pageCapHit": page_cap_hit,
            "rateLimited": rate_limited,
        },
        "sections": {"quote": quote_rows, "base": base_rows},
        "request": {
            "kind": "markets",
            "params": {
                "target": token.address,
                "chainId": chain.chain_id,
                "side": side,
                "minTvlUsd": min_tvl,
                "limit": limit,
                "lookalikes": lookalikes,
                "deep": deep,
            },
        },
    }
