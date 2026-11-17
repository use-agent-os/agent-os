from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agentos.trading.chains import BASE, NATIVE_ADDRESS
from agentos.trading.ledger import Ledger
from agentos.trading.prices import PriceService
from agentos.trading.spam import TokenCurator
from tests.test_trading.fakes import USDC, WALLET, FakePrices

#: Sentinel for "the pair object has no ``liquidity`` key at all".
_OMIT = object()

JUNK = "0x9999000000000000000000000000000000000009"
NEW = "0x9999000000000000000000000000000000000010"


@pytest.fixture
def prices_fake() -> FakePrices:
    prices = FakePrices()
    prices.spot[("base", USDC)] = 1.0  # a real pool ($1M liquidity in the fake)
    return prices


@pytest.fixture
async def stack(ledger: Ledger, prices_fake: FakePrices):
    state = {"now": 1_000_000.0, "outage": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["outage"] and request.url.host == "api.dexscreener.com":
            return httpx.Response(503, text="down")
        return prices_fake.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        prices = PriceService(http=http, ttl_s=0, now=lambda: state["now"])
        curator = TokenCurator(ledger, prices, now=lambda: state["now"])
        state["prices"] = prices_fake
        yield curator, state


def _add(ledger: Ledger, address: str, **kw) -> None:
    ledger.upsert_token(8453, address, symbol="X", name="X", decimals=18, **kw)


class TestTokenCurator:
    async def test_unlisted_unpriced_untouched_is_hidden(self, ledger: Ledger, stack) -> None:
        curator, _ = stack
        _add(ledger, JUNK)
        assert await curator.review(BASE, JUNK) is True
        row = ledger.get_token(8453, JUNK)
        assert row and row["hidden"] == 1 and row["hidden_by"] == "auto"
        assert ledger.hidden_tokens(8453) == {JUNK}

    async def test_a_pool_or_a_listing_keeps_it_shown(self, ledger: Ledger, stack) -> None:
        curator, _ = stack
        _add(ledger, USDC)  # unverified in the ledger, but DexScreener has a deep pool
        assert await curator.review(BASE, USDC) is False
        _add(ledger, NEW, verified=True)  # listed: never even priced
        assert await curator.review(BASE, NEW) is False
        assert ledger.hidden_tokens(8453) == set()

    async def test_native_and_unknown_are_never_hidden(self, ledger: Ledger, stack) -> None:
        curator, _ = stack
        assert await curator.review(BASE, NATIVE_ADDRESS) is False
        assert await curator.review(BASE, NEW) is False  # not in the ledger at all

    async def test_price_outage_is_not_a_verdict(self, ledger: Ledger, stack) -> None:
        curator, state = stack
        _add(ledger, JUNK)
        state["outage"] = True
        assert await curator.review(BASE, JUNK) is False
        row = ledger.get_token(8453, JUNK)
        assert row and row["hidden"] == 0 and row["classified_at"] is None
        state["outage"] = False
        assert await curator.review(BASE, JUNK) is True

    async def test_spent_or_touched_is_shown(self, ledger: Ledger, stack) -> None:
        curator, _ = stack
        _add(ledger, JUNK)
        ledger.insert_entry(
            ts=1.0,
            chain_id=8453,
            wallet=WALLET,
            kind="withdraw",
            tx_hash="0x" + "1" * 64,
            log_index=0,
            token_in=JUNK,
            amount_in_raw=1,
        )
        assert await curator.review(BASE, JUNK) is False
        _add(ledger, NEW)
        assert await curator.review(BASE, NEW) is True
        ledger.touch_token(8453, NEW)  # a quote or swap named it on purpose
        assert await curator.review(BASE, NEW) is False
        assert ledger.hidden_tokens(8453) == set()

    async def test_daily_recheck_resurfaces_a_launch(self, ledger: Ledger, stack) -> None:
        curator, state = stack
        _add(ledger, JUNK)
        assert await curator.review(BASE, JUNK) is True
        # It gained a pool an hour later: not looked at yet.
        state["now"] += 3600
        state["prices"].spot[("base", JUNK)] = 0.5
        assert await curator.review(BASE, JUNK) is True
        # A day later it is.
        state["now"] += 24 * 3600
        assert await curator.review(BASE, JUNK) is False

    async def test_daily_recheck_hides_a_shown_token_whose_pool_drained(
        self, ledger: Ledger, stack
    ) -> None:
        """Seen live on Base: a SEED lookalike airdrop kept a pool for its first
        look, then drained it to $0.05 — and stayed on the screen at a made-up
        price, because only hidden tokens were ever looked at again."""
        curator, state = stack
        _add(ledger, JUNK)
        state["prices"].spot[("base", JUNK)] = 0.678  # a real pool, for now
        assert await curator.review(BASE, JUNK) is False
        # The pool is pulled an hour later: not due yet, still shown.
        del state["prices"].spot[("base", JUNK)]
        state["now"] += 3600
        assert await curator.review(BASE, JUNK) is False
        # A day after the first verdict it is looked at again, and hidden.
        state["now"] += 24 * 3600
        assert await curator.review(BASE, JUNK) is True
        row = ledger.get_token(8453, JUNK)
        assert row and row["hidden"] == 1 and row["hidden_by"] == "auto"
        assert row["classified_at"] == state["now"]

    async def test_user_choice_is_final(self, ledger: Ledger, stack) -> None:
        curator, state = stack
        _add(ledger, USDC)
        ledger.set_token_hidden(8453, USDC, True, by="user", classified_at=state["now"])
        assert await curator.review(BASE, USDC, force=True) is True
        ledger.set_token_hidden(8453, JUNK, False, by="user", classified_at=state["now"])
        _add(ledger, JUNK)
        ledger.set_token_hidden(8453, JUNK, False, by="user", classified_at=state["now"])
        assert await curator.review(BASE, JUNK, force=True) is False
        # The classifier's own verdicts never touch a user row.
        ledger.set_token_hidden(8453, JUNK, True, by="auto", classified_at=state["now"])
        row = ledger.get_token(8453, JUNK)
        assert row and row["hidden"] == 0 and row["hidden_by"] == "user"


class TestReviewMany:
    """A day of due airdrops is judged in one price request per batch, not one each.

    Seen live: 15 wallets held ~370 unlisted tokens, all due on the same day;
    asked one by one the first portfolio read of the day took over three
    minutes and held the desk (and its chat) on "loading".
    """

    async def test_many_due_tokens_cost_one_request(
        self, ledger: Ledger, stack, prices_fake: FakePrices
    ) -> None:
        curator, _ = stack
        junk = [f"0x{i:040x}" for i in range(1, 26)]
        for address in junk:
            _add(ledger, address)
        _add(ledger, USDC)
        before = len(prices_fake.requests)
        verdicts = await curator.review_many(BASE, [*junk, USDC, NATIVE_ADDRESS])
        dex = [r for r in prices_fake.requests[before:] if r.url.host == "api.dexscreener.com"]
        assert len(dex) == 1
        assert verdicts == {**dict.fromkeys(junk, True), USDC: False}
        assert ledger.hidden_tokens(8453) == set(junk)

    async def test_nothing_due_asks_nothing(
        self, ledger: Ledger, stack, prices_fake: FakePrices
    ) -> None:
        curator, _ = stack
        _add(ledger, JUNK)
        await curator.review_many(BASE, [JUNK])
        before = len(prices_fake.requests)
        assert await curator.review_many(BASE, [JUNK]) == {JUNK: True}
        assert len(prices_fake.requests) == before

    async def test_concurrent_callers_price_once(
        self, ledger: Ledger, stack, prices_fake: FakePrices
    ) -> None:
        curator, _ = stack
        junk = [f"0x{i:040x}" for i in range(1, 11)]
        for address in junk:
            _add(ledger, address)
        before = len(prices_fake.requests)
        await asyncio.gather(*(curator.review_many(BASE, junk) for _ in range(3)))
        dex = [r for r in prices_fake.requests[before:] if r.url.host == "api.dexscreener.com"]
        assert len(dex) == 1

    async def test_outage_leaves_the_batch_due(self, ledger: Ledger, stack) -> None:
        curator, state = stack
        _add(ledger, JUNK)
        _add(ledger, NEW)
        state["outage"] = True
        assert await curator.review_many(BASE, [JUNK, NEW]) == {JUNK: False, NEW: False}
        assert ledger.get_token(8453, JUNK)["classified_at"] is None  # type: ignore[index]
        state["outage"] = False
        state["now"] += 10  # past the unavailable hold
        assert await curator.review_many(BASE, [JUNK, NEW]) == {JUNK: True, NEW: True}


class TestUnreportedLiquidity:
    """A pair that prices a token but carries no liquidity figure is no verdict.

    ``PriceInfo.liquidity_usd`` is ``None`` when the pair object has no
    ``liquidity.usd`` and when the feed sent a non-finite one (``_f`` reports
    both as None, #3503). Reading that as 0.0 hid a token whose pool was never
    measured -- not one that has no pool, which is what this module hides.
    ``TradingService._visible_price`` reads the same field the careful way.
    """

    @pytest.fixture
    async def stack(self, ledger: Ledger, prices_fake: FakePrices):
        """Like the module stack, with the pair's ``liquidity`` under test control.

        ``JUNK`` is given a spot price, so DexScreener answers with a real pair
        for it: the question here is only what its ``liquidity`` field says.
        """
        prices_fake.spot[("base", JUNK)] = 0.5
        state: dict[str, object] = {"now": 1_000_000.0, "liquidity": _OMIT}

        def handler(request: httpx.Request) -> httpx.Response:
            response = prices_fake.handle(request)
            if request.url.host != "api.dexscreener.com":
                return response
            payload = json.loads(response.content)
            pairs = payload if isinstance(payload, list) else payload.get("pairs", [])
            for pair in pairs:
                if state["liquidity"] is _OMIT:
                    pair.pop("liquidity", None)
                else:
                    pair["liquidity"] = state["liquidity"]
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            prices = PriceService(http=http, ttl_s=0, now=lambda: state["now"])
            yield TokenCurator(ledger, prices, now=lambda: state["now"]), state

    @pytest.mark.parametrize(
        "liquidity",
        [_OMIT, {}, {"usd": None}, {"usd": ""}, {"usd": "NaN"}],
        ids=["no-liquidity-key", "no-usd-key", "usd-null", "usd-empty", "usd-nan"],
    )
    async def test_a_missing_figure_is_not_a_verdict(
        self, ledger: Ledger, stack, liquidity: object
    ) -> None:
        curator, state = stack
        state["liquidity"] = liquidity
        _add(ledger, JUNK)
        # The token is priced -- only the liquidity figure is missing.
        assert await curator.review(BASE, JUNK) is False
        row = ledger.get_token(8453, JUNK)
        assert row and row["hidden"] == 0
        assert row["classified_at"] is None, "no verdict was recorded, so it is asked again"

    async def test_a_reported_figure_below_the_floor_still_hides(
        self, ledger: Ledger, stack
    ) -> None:
        curator, state = stack
        state["liquidity"] = {"usd": 5}
        _add(ledger, JUNK)
        assert await curator.review(BASE, JUNK) is True

    async def test_a_reported_deep_pool_still_shows(self, ledger: Ledger, stack) -> None:
        curator, state = stack
        state["liquidity"] = {"usd": 1_000_000}
        _add(ledger, JUNK)
        assert await curator.review(BASE, JUNK) is False
