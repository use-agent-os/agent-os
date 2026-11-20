"""Tokenized stocks route through the AgentOS DEX's Uniswap source.

0x refuses the tokenized stocks (``token_not_tradeable``); the AgentOS DEX's
second source, Uniswap, trades them. The engine names that source
(``source=uniswap``) for a pair with a stock on either side, and asks it once
more for a refusal the stock flag missed. Every other pair is asked the way it
always was: no ``source``, 0x. The AgentOS DEX is the only swap provider, so
there is nothing to fall back to: what the DEX answers is what the user sees.
"""

from __future__ import annotations

import pytest

from agentos.trading.chains import BASE, ROBINHOOD
from agentos.trading.prices import PriceService, is_stock_token_name
from agentos.trading.service import TradingError, TradingService
from agentos.trading.token_lists import ListToken
from tests.test_trading.fakes import (
    AAPL,
    USDC,
    WETH,
    FakeAggregator,
    FakeChain,
    FakePrices,
)
from tests.test_trading.test_service import _wire_swap_effects

WETH_RH = "0xaaaa000000000000000000000000000000000003"
#: A Coinbase tokenized stock on Base: 8 decimals, no name suffix, only the list's tag.
AAPLC = "0xb200000000000000000000000000000000000001"
NOT_TRADEABLE = (400, {"error": {"code": "TOKEN_NOT_TRADEABLE", "message": "Not tradeable"}})
SOURCE_DOWN = (
    503,
    {
        "error": {
            "code": "SOURCE_UNAVAILABLE",
            "message": "The Uniswap source cannot serve this request",
            "details": {"source": "uniswap", "reason": "router_unavailable"},
        }
    },
)
NO_ROUTE = (
    200,
    {"chainId": 8453, "liquidityAvailable": False, "explanation": "No route right now."},
)


class TestStockTokenName:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("NVIDIA • Robinhood Token", True),
            ("Apple • Robinhood Token", True),
            ("International Business Machines • Robinhood Toke", True),
            ("SPDR Portfolio S&P 500 High Dividend ETF • Robinhood T", True),
            ("Tesla •Robinhood Token ", True),
            (" NVIDIA Robinhood Token ", False),
            ("memestock TSLA", False),
            ("NVIDIA - Robinhood Token", False),
            ("Robinhood Token", False),
            ("", False),
        ],
    )
    def test_cases(self, name: str, expected: bool) -> None:
        assert is_stock_token_name(name) is expected

    def test_a_truncated_list_entry_is_flagged(self) -> None:
        address = "0xbbbb000000000000000000000000000000000001"
        name = "International Business Machines • Robinhood Toke"
        meta = PriceService._meta_from_list(ROBINHOOD, ListToken(4663, address, "IBM", name, 18))
        assert meta.stock_token and meta.verified
        # The suffix only means something on Robinhood Chain.
        base = PriceService._meta_from_list(BASE, ListToken(8453, address, "IBM", name, 18))
        assert not base.stock_token

    def test_a_stock_lists_tag_flags_a_token_on_any_chain(self) -> None:
        tagged = ListToken(8453, AAPLC, "AAPLc", "Apple Inc.", 8, None, ("stock",))
        meta = PriceService._meta_from_list(BASE, tagged)
        assert meta.stock_token and meta.decimals == 8
        plain = ListToken(8453, AAPLC, "AAPLc", "Apple Inc.", 8)
        assert not PriceService._meta_from_list(BASE, plain).stock_token


async def _quote(service: TradingService, **overrides: object) -> dict:
    params: dict = {
        "chain": ROBINHOOD,
        "wallet": None,
        "token_in": "AAPL",
        "token_out": WETH_RH,
        "amount_in": "0.5",
    }
    params.update(overrides)
    return await service.quote(**params)


def _list_base_stock(fake_prices: FakePrices) -> None:
    fake_prices.stock_lists["base"] = [
        {
            "chainId": 8453,
            "address": AAPLC,
            "symbol": "AAPLc",
            "name": "Apple Inc.",
            "decimals": 8,
            "tags": ["stock"],
        }
    ]
    fake_prices.spot[("base", AAPLC)] = 150.0


class TestQuoteRouting:
    async def test_a_stock_token_quotes_through_the_dex_uniswap_source(
        self,
        funded_service: TradingService,
        fake_aggregator: FakeAggregator,
    ) -> None:
        assert funded_service.provider_id() == "aggregator"
        # Either side: selling the Stock Token and buying it.
        sell = await _quote(funded_service)
        buy = await _quote(funded_service, token_in=WETH_RH, token_out="AAPL", amount_in="0.01")
        assert sell["provider"] == "aggregator" and buy["provider"] == "aggregator"
        assert sell["tokenIn"]["stockToken"] is True
        assert fake_aggregator.sources == ["uniswap", "uniswap"]
        assert dict(fake_aggregator.requests[-1].url.params)["source"] == "uniswap"

    async def test_a_base_stock_is_known_by_its_list_tag_even_once_the_ledger_has_it(
        self,
        funded_service: TradingService,
        fake_aggregator: FakeAggregator,
        fake_prices: FakePrices,
    ) -> None:
        _list_base_stock(fake_prices)
        first = await _quote(
            funded_service, chain=BASE, token_in="USDC", token_out="AAPLc", amount_in="10"
        )
        assert first["tokenOut"]["decimals"] == 8
        # The second quote reads the token back from the ledger, which keeps
        # no tag and whose name carries no suffix: the list is still asked.
        assert funded_service.ledger.get_token(8453, AAPLC) is not None
        await _quote(funded_service, chain=BASE, token_in=AAPLC, token_out="USDC", amount_in="1")
        assert fake_aggregator.sources == ["uniswap", "uniswap"]

    async def test_a_pair_without_a_stock_token_is_asked_with_no_source(
        self,
        funded_service: TradingService,
        fake_aggregator: FakeAggregator,
    ) -> None:
        quote = await _quote(
            funded_service, chain=BASE, token_in="USDC", token_out="WETH", amount_in="10"
        )
        assert quote["provider"] == "aggregator"
        assert fake_aggregator.quotes == 1 and fake_aggregator.sources == ["0x"]
        # 0x is asked the way it always was: the parameter is left out.
        assert "source" not in dict(fake_aggregator.requests[-1].url.params)
        # Other refusals are not retried, from either source.
        fake_aggregator.quote_error = (400, {"error": {"code": "UNKNOWN_TOKEN"}})
        with pytest.raises(TradingError) as caught:
            await _quote(
                funded_service, chain=BASE, token_in="USDC", token_out="WETH", amount_in="10"
            )
        assert caught.value.code == "trading.unknown_token"
        assert fake_aggregator.sources == ["0x", "0x"]

    async def test_a_refusal_the_flag_missed_is_retried_once_from_the_uniswap_source(
        self,
        funded_service: TradingService,
        fake_aggregator: FakeAggregator,
    ) -> None:
        fake_aggregator.source_errors["0x"] = NOT_TRADEABLE
        quote = await _quote(
            funded_service, chain=BASE, token_in="USDC", token_out="WETH", amount_in="10"
        )
        assert quote["provider"] == "aggregator"
        assert fake_aggregator.sources == ["0x", "uniswap"]

    async def test_the_uniswap_sources_own_answer_is_the_one_reported(
        self,
        funded_service: TradingService,
        fake_aggregator: FakeAggregator,
    ) -> None:
        # 0x will not trade it and Uniswap has no route: "no route" is the
        # truthful answer, not "not tradeable".
        fake_aggregator.source_errors["0x"] = NOT_TRADEABLE
        fake_aggregator.source_errors["uniswap"] = NO_ROUTE
        with pytest.raises(TradingError) as caught:
            await _quote(
                funded_service, chain=BASE, token_in="USDC", token_out="WETH", amount_in="10"
            )
        assert caught.value.code == "trading.no_route"
        assert fake_aggregator.sources == ["0x", "uniswap"]

    async def test_a_stock_the_uniswap_source_cannot_take_says_what_the_dex_said(
        self,
        funded_service: TradingService,
        fake_aggregator: FakeAggregator,
    ) -> None:
        # There is no other provider to try: the DEX's own words reach the
        # caller as they are, with no advice about a key that no longer exists.
        fake_aggregator.source_errors["uniswap"] = SOURCE_DOWN
        with pytest.raises(TradingError) as caught:
            await _quote(funded_service)
        assert caught.value.code == "trading.provider"
        assert str(caught.value) == "The Uniswap source cannot serve this request"
        assert caught.value.details["aggregatorCode"] == "SOURCE_UNAVAILABLE"
        assert "API key" not in str(caught.value)
        assert fake_aggregator.sources == ["uniswap"]

    async def test_a_refusal_neither_source_takes_keeps_the_refusal_of_0x(
        self,
        funded_service: TradingService,
        fake_aggregator: FakeAggregator,
    ) -> None:
        # The Uniswap source is switched off, or a gateway that predates it
        # answers as 0x again: "not tradeable" is what is true of this token.
        for uniswap_answer in (SOURCE_DOWN, NOT_TRADEABLE):
            fake_aggregator.sources.clear()
            fake_aggregator.source_errors["0x"] = NOT_TRADEABLE
            fake_aggregator.source_errors["uniswap"] = uniswap_answer
            with pytest.raises(TradingError) as caught:
                await _quote(
                    funded_service, chain=BASE, token_in="USDC", token_out="WETH", amount_in="10"
                )
            assert caught.value.code == "trading.token_not_tradeable"
            assert str(caught.value) == "Not tradeable"
            assert fake_aggregator.sources == ["0x", "uniswap"]


def _stock_swap(service: TradingService, **overrides: object):
    params: dict = {
        "chain": ROBINHOOD,
        "wallets": None,
        "token_in": "AAPL",
        "token_out": WETH_RH,
        "amount_in": "0.5",
        "amount_pct": None,
        "slippage_pct": None,
        "initiator": "manual",
        "session_key": None,
        "note": None,
        "wait": True,
    }
    params.update(overrides)
    return service.swap(**params)


def _fund_robinhood(service: TradingService, chain: FakeChain) -> str:
    wallet = service.test_wallet  # type: ignore[attr-defined]
    chain.tokens[WETH_RH] = ("WETH", "Wrapped Ether", 18)
    chain.set_native(wallet, 10**18)
    chain.set_erc20(AAPL, wallet, 100 * 10**18)
    return wallet


class TestOrderRouting:
    async def test_a_stock_token_swap_executes_through_the_dex_uniswap_source(
        self,
        funded_service: TradingService,
        robinhood_chain: FakeChain,
        fake_aggregator: FakeAggregator,
    ) -> None:
        service = funded_service
        wallet = _fund_robinhood(service, robinhood_chain)
        _wire_swap_effects(robinhood_chain, wallet, out_token=WETH_RH)
        # Every quote lapses at once, so execution has to price the order again.
        fake_aggregator.expires_in_s = 0.0
        orders = await _stock_swap(service)
        order = orders[0]
        assert order["provider"] == "aggregator", order
        assert order["status"] == "confirmed", order
        # The row is what DCA runs, trigger fires and approvals read back.
        row = service.ledger.get_order(order["orderId"])
        assert row is not None and row["provider"] == "aggregator"
        # The order was priced more than once, and never by 0x.
        assert len(fake_aggregator.sources) > 1
        assert set(fake_aggregator.sources) == {"uniswap"}
        assert len(robinhood_chain.sent) == 1

    async def test_an_order_0x_refuses_executes_through_the_dex_uniswap_source(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet, out_token=WETH)
        fake_aggregator.source_errors["0x"] = NOT_TRADEABLE
        fake_aggregator.expires_in_s = 0.0
        orders = await _stock_swap(
            service, chain=BASE, token_in=USDC, token_out="WETH", amount_in="10"
        )
        order = orders[0]
        assert order["provider"] == "aggregator", order
        assert order["status"] == "confirmed", order
        # Each pricing asks 0x, is refused, and asks the Uniswap source.
        assert fake_aggregator.sources[:2] == ["0x", "uniswap"]
        assert fake_aggregator.sources[-1] == "uniswap"

    async def test_an_order_the_source_cannot_take_fails_with_the_dex_answer(
        self,
        funded_service: TradingService,
        robinhood_chain: FakeChain,
        fake_aggregator: FakeAggregator,
    ) -> None:
        service = funded_service
        _fund_robinhood(service, robinhood_chain)
        fake_aggregator.source_errors["uniswap"] = SOURCE_DOWN
        orders = await _stock_swap(service)
        order = orders[0]
        assert order["provider"] == "aggregator", order
        assert order["status"] == "failed", order
        assert "The Uniswap source cannot serve this request" in order["reason"]
        assert "API key" not in order["reason"]
        assert robinhood_chain.sent == []

    async def test_an_order_row_from_the_uniswap_provider_runs_through_the_dex(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
    ) -> None:
        # A row parked for approval while the Uniswap Trading API provider
        # existed still names it. There is one provider now: the row is priced
        # and built by the AgentOS DEX, and says so afterwards.
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet, out_amount=5 * 10**16)
        fake_aggregator.amount_out = 5 * 10**16  # 0.05 WETH: $100 for $100
        base_chain.set_erc20(USDC, wallet, 1_000 * 10**6)
        parked = (
            await _stock_swap(
                service,
                chain=BASE,
                token_in=USDC,
                token_out="WETH",
                amount_in="250",
                initiator="agent",
                wait=False,
            )
        )[0]
        assert parked["status"] == "awaiting_approval", parked
        service.ledger.update_order(parked["orderId"], provider="uniswap")
        service.ledger.seal_row("order", parked["orderId"])
        assert service.provider("uniswap").id == "aggregator"
        row = service.ledger.get_order(parked["orderId"])
        assert row is not None and row["provider"] == "uniswap"
        await service._process_order(dict(row), wait=False)
        after = service.ledger.get_order(parked["orderId"])
        assert after is not None and after["provider"] == "aggregator"
