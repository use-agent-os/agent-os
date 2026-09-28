"""Token metadata and USD prices.

Sources, in order of trust for the job:

* CoinGecko token lists (``tokens.coingecko.com/<platform>/all.json``) for
  symbol/name/decimals/logo and the *verified* flag. On Robinhood Chain the
  genuine Stock Tokens carry the ``• Robinhood Token`` name suffix; community
  tokens reuse the same tickers, so the flag matters.
* DexScreener for spot prices, 24h change and liquidity (``base`` and
  ``robinhood`` slugs both work), and for text search.
* CoinGecko ``market_chart/range`` for a price at a past timestamp (cost
  basis of deposits); it can 404 for young tokens, in which case the caller
  records an approximate basis from the spot price.
* GeckoTerminal OHLCV for charts (Base only; Robinhood is not indexed).

A price miss never raises into a caller: it returns ``None``.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

from agentos.trading.chains import BASE, NATIVE_ADDRESS, ChainSpec, is_native, normalize_address
from agentos.trading.evm import USER_AGENT

DEXSCREENER_BASE = "https://api.dexscreener.com"
COINGECKO_TOKENS_BASE = "https://tokens.coingecko.com"
COINGECKO_API_BASE = "https://api.coingecko.com/api/v3"
GECKOTERMINAL_BASE = "https://api.geckoterminal.com/api/v2"

ROBINHOOD_STOCK_SUFFIX = "• Robinhood Token"
TOKEN_LIST_TTL_S = 24 * 3600
# How long an empty (failed) token-list download is kept before retrying.
TOKEN_LIST_EMPTY_HOLD_S = 60.0
HISTORY_TTL_S = 6 * 3600
DEXSCREENER_BATCH = 30
#: A chunk the source did not answer (429, 5xx, timeout) is answered from that
#: miss for this long, ± ``UNAVAILABLE_JITTER_S``: long enough that the
#: portfolio poll and a burst of desk turns do not turn one 429 into a retry
#: loop, short enough that the next read gets through. Jittered so callers
#: held together do not all come back in the same instant.
UNAVAILABLE_HOLD_S = 3.0
UNAVAILABLE_JITTER_S = 1.0


@dataclass
class TokenMeta:
    chain_id: int
    address: str  # lowercase, NATIVE_ADDRESS for the gas token
    symbol: str
    name: str
    decimals: int
    logo_url: str | None = None
    native: bool = False
    verified: bool = False
    stock_token: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "chainId": self.chain_id,
            "address": self.address,
            "symbol": self.symbol,
            "name": self.name,
            "decimals": self.decimals,
            "logoUrl": self.logo_url,
            "native": self.native,
            "verified": self.verified,
            "stockToken": self.stock_token,
        }


@dataclass
class PriceInfo:
    price_usd: float | None
    change_24h_pct: float | None = None
    liquidity_usd: float | None = None
    volume_24h_usd: float | None = None
    pair_address: str | None = None
    pair_url: str | None = None
    image_url: str | None = None
    #: The three below ride along in the same DexScreener pair object the
    #: fields above are read from — parsing them costs no extra request.
    #: ``price_native`` is quoted in the *pool's* quote token, which is not
    #: always the chain's gas coin — a WETH/USDC pool quotes in USDC. The
    #: symbol travels with it so nothing has to guess.
    price_native: float | None = None
    price_native_symbol: str | None = None
    market_cap_usd: float | None = None
    #: Venue as DexScreener names it, e.g. "Uniswap v4".
    market: str | None = None
    fetched_at: float = field(default_factory=time.time)
    #: The price source did not answer (network, 5xx, bad body). Distinct
    #: from "answered and found no pair": only the latter means "worthless".
    unavailable: bool = False
    #: For an ``unavailable`` answer: seconds until the source is asked again
    #: (a retry sooner only gets this same miss back).
    retry_in_s: float = 0.0


# The gas token has no DexScreener pair of its own; use CoinGecko's own art.
NATIVE_LOGOS: dict[str, str] = {
    "ETH": "https://assets.coingecko.com/coins/images/279/thumb/ethereum.png",
}


def native_token(chain: ChainSpec) -> TokenMeta:
    return TokenMeta(
        chain_id=chain.chain_id,
        address=NATIVE_ADDRESS,
        symbol=chain.native_symbol,
        name=chain.native_name,
        decimals=18,
        logo_url=NATIVE_LOGOS.get(chain.native_symbol),
        native=True,
        verified=True,
    )


def _f(value: Any) -> float | None:
    """A number from the feed, or ``None``.

    ``float()`` parses ``"NaN"`` and ``"Infinity"`` out of a third-party body,
    and a NaN makes every comparison it takes part in false, so it travels
    silently until something downstream decides the wrong way (#3503). Not a
    finite number is not a number.
    """
    try:
        if value is None or value == "":
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _price(value: Any) -> float | None:
    """A *price* from the feed: finite and above zero, or ``None``.

    Zero or negative is not a price a position can be valued at, and the
    callers already treat ``None`` as "this token is unpriced", which is the
    honest reading of a feed that answered with one.
    """
    number = _f(value)
    return number if number is not None and number > 0 else None


def _market_label(pair: Any) -> str | None:
    """ "uniswap" + ["v4"] -> "Uniswap v4". The venue as a human reads it."""
    dex = str((pair or {}).get("dexId") or "").strip()
    if not dex:
        return None
    labels = (pair or {}).get("labels")
    tail = " ".join(str(x).strip() for x in labels if str(x).strip()) if labels else ""
    name = dex[:1].upper() + dex[1:]
    return f"{name} {tail}".strip()


class PriceService:
    def __init__(
        self,
        *,
        http: httpx.AsyncClient | None = None,
        ttl_s: float = 20.0,
        now: Callable[[], float] = time.time,
        timeout: float = 10.0,
    ) -> None:
        self._own_http = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout)
        self._timeout = timeout
        self.ttl_s = ttl_s
        self._now = now
        self._prices: dict[tuple[int, str], PriceInfo] = {}
        #: Cached ``unavailable`` misses: key -> when the source is asked again.
        self._held_until: dict[tuple[int, str], float] = {}
        self._token_lists: dict[int, tuple[float, dict[str, TokenMeta]]] = {}
        self._history: dict[tuple[int, str, int], tuple[float, float | None]] = {}
        self._list_locks: dict[int, asyncio.Lock] = {}

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()

    async def _get(self, url: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.get(
                url,
                headers={"accept": "application/json", "user-agent": USER_AGENT},
                timeout=self._timeout,
                **kwargs,
            )
        except httpx.HTTPError:
            return None
        if response.status_code >= 400:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    # ── token lists ────────────────────────────────────────────────────

    async def token_list(self, chain: ChainSpec) -> dict[str, TokenMeta]:
        cached = self._token_lists.get(chain.chain_id)
        if cached and self._now() - cached[0] < TOKEN_LIST_TTL_S:
            return cached[1]
        lock = self._list_locks.setdefault(chain.chain_id, asyncio.Lock())
        async with lock:
            cached = self._token_lists.get(chain.chain_id)
            if cached and self._now() - cached[0] < TOKEN_LIST_TTL_S:
                return cached[1]
            tokens: dict[str, TokenMeta] = {}
            if chain.coingecko_platform:
                body = await self._get(
                    f"{COINGECKO_TOKENS_BASE}/{chain.coingecko_platform}/all.json"
                )
                for raw in (body or {}).get("tokens", []) if isinstance(body, dict) else []:
                    meta = self._meta_from_list(chain, raw)
                    if meta is not None:
                        tokens[meta.address] = meta
            if tokens:
                self._token_lists[chain.chain_id] = (self._now(), tokens)
            elif cached is None:
                # A failed first download must not become a day of "unknown
                # symbol": hold the empty list only briefly, then ask again.
                stamp = self._now() - TOKEN_LIST_TTL_S + TOKEN_LIST_EMPTY_HOLD_S
                self._token_lists[chain.chain_id] = (stamp, tokens)
            return tokens or (cached[1] if cached else {})

    @staticmethod
    def _meta_from_list(chain: ChainSpec, raw: Any) -> TokenMeta | None:
        if not isinstance(raw, dict):
            return None
        try:
            if int(raw.get("chainId") or chain.chain_id) != chain.chain_id:
                return None
            address = normalize_address(str(raw["address"]))
        except (KeyError, ValueError, TypeError):
            return None
        name = str(raw.get("name") or "")
        stock = chain.key == "robinhood" and name.endswith(ROBINHOOD_STOCK_SUFFIX)
        return TokenMeta(
            chain_id=chain.chain_id,
            address=address,
            symbol=str(raw.get("symbol") or ""),
            name=name,
            decimals=int(raw.get("decimals") or 18),
            logo_url=str(raw["logoURI"]) if raw.get("logoURI") else None,
            verified=True,
            stock_token=stock,
        )

    async def known_token(self, chain: ChainSpec, address: str) -> TokenMeta | None:
        if is_native(address):
            return native_token(chain)
        tokens = await self.token_list(chain)
        return tokens.get(normalize_address(address))

    async def find_by_symbol(self, chain: ChainSpec, symbol: str) -> list[TokenMeta]:
        wanted = symbol.strip().lower()
        if not wanted:
            return []
        if wanted in {chain.native_symbol.lower(), "native"}:
            return [native_token(chain)]
        tokens = await self.token_list(chain)
        hits = [t for t in tokens.values() if t.symbol.lower() == wanted]
        # Genuine Stock Tokens first, then the rest by name.
        hits.sort(key=lambda t: (not t.stock_token, t.name.lower()))
        return hits

    # ── spot prices ────────────────────────────────────────────────────

    def _price_key(self, chain: ChainSpec, address: str) -> str:
        if is_native(address):
            return (chain.weth or "").lower() or NATIVE_ADDRESS
        return normalize_address(address)

    async def prices(self, chain: ChainSpec, addresses: Iterable[str]) -> dict[str, PriceInfo]:
        """Spot prices keyed by the *requested* address (native stays native)."""
        wanted: dict[str, str] = {}
        for address in addresses:
            requested = NATIVE_ADDRESS if is_native(address) else normalize_address(address)
            wanted[requested] = self._price_key(chain, requested)
        out: dict[str, PriceInfo] = {}
        missing: list[str] = []
        now = self._now()
        for requested, lookup in wanted.items():
            key = (chain.chain_id, lookup)
            cached = self._prices.get(key)
            if cached is not None and cached.unavailable:
                held = self._held_until.get(key, 0.0) - now
                if held > 0:
                    out[requested] = replace(cached, retry_in_s=held)
                    continue
            elif cached and now - cached.fetched_at < self.ttl_s:
                out[requested] = cached
                continue
            if lookup != NATIVE_ADDRESS:
                missing.append(lookup)
        fetched: dict[str, PriceInfo] = {}
        unanswered: dict[str, float] = {}
        unique = sorted(set(missing))
        for start in range(0, len(unique), DEXSCREENER_BATCH):
            chunk = unique[start : start + DEXSCREENER_BATCH]
            body = await self._get(
                f"{DEXSCREENER_BASE}/tokens/v1/{chain.dexscreener_slug}/{','.join(chunk)}"
            )
            if body is None:
                # One hold per chunk, so its tokens come back as one request;
                # never longer than an answer would be kept (``ttl_s``).
                hold = UNAVAILABLE_HOLD_S + random.uniform(
                    -UNAVAILABLE_JITTER_S, UNAVAILABLE_JITTER_S
                )
                unanswered.update(dict.fromkeys(chunk, min(max(hold, 0.0), self.ttl_s)))
                continue
            for lookup, info in self._best_pairs(chain, body).items():
                info.fetched_at = now
                fetched[lookup] = info
        for requested, lookup in wanted.items():
            if requested in out:
                continue
            found = fetched.get(lookup)
            key = (chain.chain_id, lookup)
            if found is not None:
                resolved = found
                self._held_until.pop(key, None)
            elif lookup in unanswered:
                # "The source did not answer" is not an answer: held for
                # seconds, not ``ttl_s`` (a 429 would blank prices for everyone
                # and no retry could get through), but held -- never cached, a
                # 429 became an immediate retry from every caller.
                hold = unanswered[lookup]
                resolved = PriceInfo(
                    price_usd=None, fetched_at=now, unavailable=True, retry_in_s=hold
                )
                self._held_until[key] = now + hold
            else:
                resolved = PriceInfo(price_usd=None, fetched_at=now)
                self._held_until.pop(key, None)
            self._prices[key] = resolved
            out[requested] = resolved
        native = out.get(NATIVE_ADDRESS)
        if native is not None and native.price_usd is None:
            borrowed = await self._native_price_fallback(chain)
            if borrowed is not None:
                out[NATIVE_ADDRESS] = borrowed
        return out

    async def _native_price_fallback(self, chain: ChainSpec) -> PriceInfo | None:
        """The gas coin of an ETH-denominated L2 is ETH: price it from Base.

        Robinhood Chain has no wrapped-ETH pool DexScreener indexes, so its
        native feed is empty; ETH is ETH, and Base's WETH price is the same
        asset. Looked up per call (Base's own cache answers within its
        TTL), never written into this chain's cache, so a feed that comes
        alive is used the moment it does.
        """
        if chain.chain_id == BASE.chain_id or chain.native_symbol != BASE.native_symbol:
            return None
        base = (await self.prices(BASE, [NATIVE_ADDRESS])).get(NATIVE_ADDRESS)
        if base is None or base.price_usd is None:
            return None
        return PriceInfo(
            price_usd=base.price_usd,
            change_24h_pct=base.change_24h_pct,
            fetched_at=base.fetched_at,
        )

    async def price(self, chain: ChainSpec, address: str) -> float | None:
        result = await self.prices(chain, [address])
        requested = NATIVE_ADDRESS if is_native(address) else normalize_address(address)
        info = result.get(requested)
        return info.price_usd if info else None

    async def pair_tokens(self, chain: ChainSpec, pair_address: str) -> list[str]:
        """The two token addresses of a DexScreener pair, lower-cased; empty when unknown.

        A Uniswap V4 pair's address there is its poolId, which is how an LP read
        recovers the tokens behind a bare poolId without an Initialize log scan.
        """
        body = await self._get(
            f"{DEXSCREENER_BASE}/latest/dex/pairs/{chain.dexscreener_slug}/{pair_address}"
        )
        pairs = (body.get("pairs") or []) if isinstance(body, dict) else []
        out: list[str] = []
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            if str(pair.get("pairAddress") or "").lower() != pair_address.lower():
                continue
            for side in ("baseToken", "quoteToken"):
                try:
                    address = normalize_address(str((pair.get(side) or {}).get("address") or ""))
                except ValueError:
                    continue
                if address not in out:
                    out.append(address)
        return out

    def _best_pairs(self, chain: ChainSpec, body: Any) -> dict[str, PriceInfo]:
        best: dict[str, tuple[float, PriceInfo]] = {}
        pairs = body if isinstance(body, list) else (body or {}).get("pairs", [])
        for pair in pairs or []:
            info = self._pair_info(chain, pair)
            if info is None:
                continue
            address, price = info
            liquidity = price.liquidity_usd or 0.0
            current = best.get(address)
            if current is None or liquidity > current[0]:
                best[address] = (liquidity, price)
        return {address: price for address, (_, price) in best.items()}

    @staticmethod
    def _pair_info(chain: ChainSpec, pair: Any) -> tuple[str, PriceInfo] | None:
        if not isinstance(pair, dict):
            return None
        if str(pair.get("chainId") or "") != chain.dexscreener_slug:
            return None
        base = pair.get("baseToken") or {}
        try:
            address = normalize_address(str(base.get("address") or ""))
        except ValueError:
            return None
        if address == NATIVE_ADDRESS:
            return None
        liquidity = pair.get("liquidity") or {}
        change = pair.get("priceChange") or {}
        volume = pair.get("volume") or {}
        info = pair.get("info") or {}
        return address, PriceInfo(
            price_usd=_price(pair.get("priceUsd")),
            change_24h_pct=_f(change.get("h24")),
            liquidity_usd=_f(liquidity.get("usd")),
            volume_24h_usd=_f(volume.get("h24")),
            pair_address=str(pair.get("pairAddress") or "") or None,
            pair_url=str(pair.get("url") or "") or None,
            image_url=str(info.get("imageUrl") or "") or None,
            price_native=_price(pair.get("priceNative")),
            price_native_symbol=str((pair.get("quoteToken") or {}).get("symbol") or "") or None,
            # marketCap is absent for tokens DexScreener cannot supply; fdv is
            # the honest stand-in, and None rather than a zero when neither is.
            market_cap_usd=_f(pair.get("marketCap")) or _f(pair.get("fdv")),
            market=_market_label(pair),
            fetched_at=time.time(),
        )

    # ── search ─────────────────────────────────────────────────────────

    async def search(
        self, chain: ChainSpec, query: str, *, limit: int = 12
    ) -> list[dict[str, Any]]:
        """Tokens matching ``query`` (symbol, name, or address) with a spot price."""
        text = query.strip()
        if not text:
            return []
        results: list[dict[str, Any]] = []
        seen: set[str] = set()

        async def add(meta: TokenMeta, price: PriceInfo | None) -> None:
            if meta.address in seen:
                return
            seen.add(meta.address)
            row = meta.to_dict()
            row["priceUsd"] = price.price_usd if price else None
            row["liquidityUsd"] = price.liquidity_usd if price else None
            row["change24hPct"] = price.change_24h_pct if price else None
            if not meta.logo_url and price and price.image_url:
                row["logoUrl"] = price.image_url
            results.append(row)

        if text.lower() in {chain.native_symbol.lower(), "native"}:
            native = native_token(chain)
            prices = await self.prices(chain, [NATIVE_ADDRESS])
            await add(native, prices.get(NATIVE_ADDRESS))
            return results

        if text.startswith("0x") and len(text) == 42:
            try:
                address = normalize_address(text)
            except ValueError:
                return []
            meta = await self.known_token(chain, address)
            prices = await self.prices(chain, [address])
            if meta is None:
                meta = TokenMeta(chain.chain_id, address, "", "", 18)
            await add(meta, prices.get(address))
            return results

        tokens = await self.token_list(chain)
        lowered = text.lower()
        exact = [t for t in tokens.values() if t.symbol.lower() == lowered]
        partial = [
            t
            for t in tokens.values()
            if t not in exact and (lowered in t.symbol.lower() or lowered in t.name.lower())
        ]
        exact.sort(key=lambda t: (not t.stock_token, t.name.lower()))
        partial.sort(key=lambda t: (not t.stock_token, len(t.symbol), t.name.lower()))
        candidates = (exact + partial)[:limit]
        prices = await self.prices(chain, [t.address for t in candidates])
        for meta in candidates:
            await add(meta, prices.get(meta.address))
        if len(results) < limit:
            body = await self._get(f"{DEXSCREENER_BASE}/latest/dex/search", params={"q": text})
            for pair in (body or {}).get("pairs", []) if isinstance(body, dict) else []:
                info = self._pair_info(chain, pair)
                if info is None:
                    continue
                address, price = info
                base = pair.get("baseToken") or {}
                meta = tokens.get(address) or TokenMeta(
                    chain.chain_id,
                    address,
                    str(base.get("symbol") or ""),
                    str(base.get("name") or ""),
                    18,
                    logo_url=price.image_url,
                )
                await add(meta, price)
                if len(results) >= limit:
                    break
        return results

    # ── history ────────────────────────────────────────────────────────

    async def price_at(self, chain: ChainSpec, address: str, ts: int) -> float | None:
        """USD price nearest to ``ts`` (unix seconds), or ``None``."""
        if not chain.coingecko_platform:
            return None
        lookup = self._price_key(chain, address)
        bucket = int(ts // 3600)
        key = (chain.chain_id, lookup, bucket)
        cached = self._history.get(key)
        if cached and self._now() - cached[0] < HISTORY_TTL_S:
            return cached[1]
        body = await self._get(
            f"{COINGECKO_API_BASE}/coins/{chain.coingecko_platform}/contract/{lookup}"
            "/market_chart/range",
            params={"vs_currency": "usd", "from": ts - 6 * 3600, "to": ts + 6 * 3600},
        )
        price: float | None = None
        points = (body or {}).get("prices") if isinstance(body, dict) else None
        if isinstance(points, list) and points:
            nearest = min(points, key=lambda p: abs(float(p[0]) / 1000 - ts))
            price = _price(nearest[1])
        self._history[key] = (self._now(), price)
        return price

    # ── charts ─────────────────────────────────────────────────────────

    async def ohlcv(
        self, chain: ChainSpec, address: str, *, timeframe: str, limit: int
    ) -> list[dict[str, float]] | None:
        """Candles from GeckoTerminal for chains it indexes; ``None`` otherwise."""
        if chain.key != "base":
            return None
        lookup = self._price_key(chain, address)
        prices = await self.prices(chain, [lookup])
        info = prices.get(lookup)
        if info is None or not info.pair_address:
            return None
        aggregate = "1"
        frame = timeframe
        if timeframe == "4h":
            frame, aggregate = "hour", "4"
        elif timeframe == "15m":
            frame, aggregate = "minute", "15"
        elif timeframe == "5m":
            frame, aggregate = "minute", "5"
        body = await self._get(
            f"{GECKOTERMINAL_BASE}/networks/base/pools/{info.pair_address}/ohlcv/{frame}",
            params={"aggregate": aggregate, "limit": limit},
        )
        rows = (((body or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list")
        if not isinstance(rows, list):
            return None
        out: list[dict[str, float]] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 5:
                continue
            try:
                out.append(
                    {
                        "t": float(row[0]),
                        "o": float(row[1]),
                        "h": float(row[2]),
                        "l": float(row[3]),
                        "c": float(row[4]),
                    }
                )
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda r: r["t"])
        return out
