"""Junk tokens: decide which of a wallet's tokens are worth showing.

Anyone can airdrop a token to any address, and the ones that arrive
uninvited are almost all junk — "visit site to claim" lures, dead
forks, dust. Left alone they bury the real holdings, swell the sync's scan
set and cost a price lookup each. Deleting them is wrong (the balance is
real and the ledger must explain it), so they are *hidden*: kept in the
ledger, kept out of what the user and the agent see.

A token is hidden automatically when all three hold:

1. Nobody lists it: it is not in the chain's token lists (``verified``).
2. Nobody trades it: the price source answered and found no pool with at
   least :data:`MIN_LIQUIDITY_USD` behind it. Unreachable is not "no pool".
3. The wallet never acted on it: never sold, sent, swapped or quoted it
   (``touched`` / spent). Junk only ever arrives.

Every unlisted, untouched token is re-checked daily, whichever way the last
verdict went: a hidden one resurfaces when a real launch gains a pool, and a
shown one is hidden once its pool drains — an airdrop that kept a shallow
pool for a day and then pulled it would otherwise stay visible forever, at
whatever price the empty pool still quotes. A user's hide/show is final: the
classifier never overrides ``hidden_by = 'user'``, and any deliberate act on
a token (a quote, a swap) shows it again.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable

import structlog

from agentos.trading.chains import ChainSpec, is_native, normalize_address
from agentos.trading.ledger import Ledger
from agentos.trading.prices import PriceInfo, PriceService

log = structlog.get_logger(__name__)

# A pool shallower than this cannot be sold into; for trading it is no pool.
MIN_LIQUIDITY_USD = 1_000.0
# How often an unlisted, untouched token is looked at again (hidden or shown).
RECLASSIFY_S = 24 * 3600.0


class TokenCurator:
    def __init__(
        self,
        ledger: Ledger,
        prices: PriceService,
        *,
        now: Callable[[], float] = time.time,
        min_liquidity_usd: float = MIN_LIQUIDITY_USD,
        reclassify_s: float = RECLASSIFY_S,
    ) -> None:
        self.ledger = ledger
        self.prices = prices
        self._now = now
        self.min_liquidity_usd = min_liquidity_usd
        self.reclassify_s = reclassify_s
        self._locks: dict[int, asyncio.Lock] = {}

    async def review(self, chain: ChainSpec, address: str, *, force: bool = False) -> bool:
        """Classify the token if it is due, and say whether it is hidden now.

        Cheap when nothing is due: two ledger reads. The price lookup runs
        once when a token is first met and then daily for as long as the
        token is neither listed nor touched — shown tokens included, so a
        pool that drained after the first look does not keep the token (and
        its fictional price) on the screen.
        """
        if is_native(address):
            return False
        verdicts = await self.review_many(chain, [address], force=force)
        return verdicts.get(normalize_address(address), False)

    async def review_many(
        self, chain: ChainSpec, addresses: Iterable[str], *, force: bool = False
    ) -> dict[str, bool]:
        """:meth:`review` for many tokens of one chain, with one price lookup.

        A wallet full of airdrops holds hundreds of unlisted tokens, and every
        one of them falls due on the same day. Asked one by one that is one
        DexScreener round trip per token, in a row, inside whatever read
        needed them judged (the portfolio, the balances), and it held the
        desk on "loading" for minutes. Asked together it is one request per
        :data:`~agentos.trading.prices.DEXSCREENER_BATCH` tokens.

        Calls for the same chain run one at a time: the desk asks for the
        all-wallet portfolio, a wallet's own and its balances at once, and
        the second caller then finds the tokens judged rather than pricing
        them all again. Returns ``{address: hidden}`` for the tokens the
        ledger knows (native and unknown tokens are never hidden, and absent).
        """
        keys = sorted({normalize_address(a) for a in addresses if not is_native(a)})
        if not keys:
            return {}
        out, due = self._scan(chain, keys, force=force)
        if not due:
            return out  # the usual case, and it never waits on a lock
        async with self._locks.setdefault(chain.chain_id, asyncio.Lock()):
            # Another read may have judged these while this one waited.
            out, due = self._scan(chain, keys, force=force)
            if not due:
                return out
            spent = {k for k in due if self.ledger.token_was_spent(chain.chain_id, k)}
            priced = [k for k in due if k not in spent]
            infos = await self.prices.prices(chain, priced) if priced else {}
            for key in due:
                verdict = False if key in spent else self._verdict(infos.get(key))
                if verdict is None:
                    # No answer from the price source: leave the token as it is
                    # and ask again next time, rather than hiding something real.
                    continue
                self._record(chain, key, verdict, was_hidden=out[key])
                out[key] = verdict
            return out

    def _scan(
        self, chain: ChainSpec, keys: list[str], *, force: bool
    ) -> tuple[dict[str, bool], list[str]]:
        """Each known token's hidden flag now, and which of them are due a verdict."""
        out: dict[str, bool] = {}
        due: list[str] = []
        for key in keys:
            row = self.ledger.get_token(chain.chain_id, key)
            if row is None or row["is_native"]:
                continue
            hidden = bool(row["hidden"])
            out[key] = hidden
            if row.get("hidden_by") == "user":
                continue
            if row["verified"] or row["touched"]:
                if hidden:
                    self.ledger.set_token_hidden(
                        chain.chain_id, key, False, by="auto", classified_at=self._now()
                    )
                    out[key] = False
                continue
            classified_at = row.get("classified_at")
            if (
                force
                or classified_at is None
                or self._now() - float(classified_at) >= self.reclassify_s
            ):
                due.append(key)
        return out, due

    def _record(self, chain: ChainSpec, key: str, verdict: bool, *, was_hidden: bool) -> None:
        self.ledger.set_token_hidden(
            chain.chain_id, key, verdict, by="auto", classified_at=self._now()
        )
        if verdict != was_hidden:
            log.info(
                "trading.token_hidden" if verdict else "trading.token_shown",
                chain=chain.key,
                token=key,
            )

    def _verdict(self, info: PriceInfo | None) -> bool | None:
        if info is None or info.unavailable:
            return None
        liquidity = info.liquidity_usd
        if liquidity is None:
            if info.price_usd is None:
                # Answered and found no pair at all: nobody trades it, which is
                # the junk this module hides.
                return True
            # A pair prices the token but reports no depth: ``liquidity.usd``
            # can be absent, and ``_f`` reports a non-finite one as None too
            # (#3503). That is a pool nobody measured, not a token with no
            # pool, so it is no more a verdict than an unreachable source is --
            # the token stays as it is and is asked about again tomorrow.
            # ``TradingService._visible_price`` reads the field the same way.
            return None
        return liquidity < self.min_liquidity_usd
