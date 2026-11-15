"""Guardrails and approvals bind to exactly what gets signed (WAL-06/07/12/23).

Each class ports one audit PoC onto the repository fakes: no chain RPC, no
network, no real keys. On the base commit every test here fails the way the
audit observed; after the fix it passes.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentos.trading.chains import BASE
from agentos.trading.service import TradingService
from tests.test_trading.fakes import (
    USDC,
    WETH,
    FakeAggregator,
    FakeChain,
    FakePrices,
    FakeUniswap,
)
from tests.test_trading.test_service import _wire_swap_effects


def _events(service: TradingService, name: str) -> list[dict[str, Any]]:
    return [payload for event, payload in service.events if event == name]  # type: ignore[attr-defined]


async def _agent_swap(service: TradingService, **extra: Any) -> dict[str, Any]:
    orders = await service.swap(
        chain=BASE,
        wallets=None,
        token_in=extra.pop("token_in", "USDC"),
        token_out=extra.pop("token_out", "WETH"),
        amount_in=extra.pop("amount_in", "10"),
        amount_pct=extra.pop("amount_pct", None),
        slippage_pct=None,
        initiator=extra.pop("initiator", "agent"),
        session_key=None,
        note=None,
        wait=True,
        **extra,
    )
    return orders[0]


class TestCapBeforeClientQuotePark:
    """WAL-23: a client-declared ``expectedOutRaw`` must not turn a cap refusal into a park."""

    @pytest.mark.parametrize("cap", [0.0, 5.0])
    async def test_over_cap_agent_swap_with_a_stale_client_quote_is_rejected(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
        cap: float,
    ) -> None:
        service = funded_service
        service.config.daily_cap_usd = cap
        # A little over 2x the default 0.5 % slippage above the fresh quote.
        declared = fake_aggregator.amount_out * 102 // 100
        order = await _agent_swap(service, expected_out_raw=declared)
        assert order["status"] == "rejected"
        assert "daily cap" in str(order["reason"])
        assert base_chain.sent == []
        assert _events(service, "trading.approval.requested") == []

    async def test_under_cap_stale_client_quote_still_parks_and_names_every_reason(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
    ) -> None:
        service = funded_service
        service.config.approval_threshold_usd = 5.0  # the $10 order is also above it
        declared = fake_aggregator.amount_out * 102 // 100
        order = await _agent_swap(service, expected_out_raw=declared)
        assert order["status"] == "awaiting_approval"
        assert "since you confirmed" in order["reason"]
        assert "above the 5.00 USD threshold" in order["reason"]
        assert base_chain.sent == []


class TestUnpricedSellSideFailsClosed:
    """WAL-06: an agent swap is never valued from the buy side alone, on any provider."""

    @pytest.mark.parametrize("usdc_price", [None, 0.0])
    async def test_uniswap_swap_of_an_unpriced_holding_waits_for_a_human(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_prices: FakePrices,
        fake_uniswap: FakeUniswap,
        usdc_price: float | None,
    ) -> None:
        # The v3-08 PoC: the sell side (here the whole USDC balance) has no
        # feed price; the buy side is a token whose own pool says it is
        # worth next to nothing; Uniswap reports a benign 0.12 % impact.
        service = funded_service
        service.config.provider = "uniswap"
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        if usdc_price is None:
            fake_prices.spot.pop(("base", USDC))
        else:
            fake_prices.spot[("base", USDC)] = usdc_price
        fake_prices.spot[("base", WETH)] = 0.0001
        order = await _agent_swap(service, amount_in=None, amount_pct=100.0)
        assert order["status"] == "awaiting_approval"
        assert order["reason"] == "value unknown (no price)"
        # Unknown, not "$0": nothing on the row reads as a tiny order.
        assert order["valueUsd"] is None
        assert base_chain.sent == []
        assert base_chain.get_erc20(USDC, wallet) == 1_000 * 10**6
        assert service.limits(wallet)["spentTodayUsd"] == 0.0

    async def test_uniswap_impact_is_cross_checked_against_the_feed(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_uniswap: FakeUniswap,
    ) -> None:
        # Both sides priced: $10 of USDC for 0.0005 WETH ($1 at the fake's
        # $2,000) is a 90 % loss whatever Uniswap's own priceImpact says.
        service = funded_service
        service.config.provider = "uniswap"
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        assert fake_uniswap.price_impact == pytest.approx(0.12)
        order = await _agent_swap(service)
        assert order["status"] == "awaiting_approval"
        assert order["reason"].startswith("price impact 90.00% is above")
        assert order["priceImpactPct"] == pytest.approx(90.0)
        assert base_chain.sent == []

    async def test_uniswap_impact_with_an_unpriced_buy_side_is_unknown(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_prices: FakePrices,
        fake_uniswap: FakeUniswap,
    ) -> None:
        service = funded_service
        service.config.provider = "uniswap"
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        fake_prices.spot.pop(("base", WETH))
        order = await _agent_swap(service)
        # Sized by what leaves the wallet; the provider's impact alone decides nothing.
        assert order["valueUsd"] == pytest.approx(10.0)
        assert order["status"] == "awaiting_approval"
        assert order["reason"] == "price impact unknown (no reference price for one side)"
        assert base_chain.sent == []

    async def test_a_consistent_uniswap_quote_still_runs(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_uniswap: FakeUniswap,
    ) -> None:
        service = funded_service
        service.config.provider = "uniswap"
        wallet = service.test_wallet  # type: ignore[attr-defined]
        fake_uniswap.amount_out = 5 * 10**15  # $10 for $10
        _wire_swap_effects(base_chain, wallet, out_amount=5 * 10**15)
        order = await _agent_swap(service)
        assert order["status"] == "confirmed"
        assert service.limits(wallet)["spentTodayUsd"] == pytest.approx(10.0)

    async def test_aggregator_still_parks_and_reports_the_value_as_unknown(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_prices: FakePrices,
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        fake_prices.spot.pop(("base", USDC))
        order = await _agent_swap(service)
        assert order["status"] == "awaiting_approval"
        assert order["valueUsd"] is None
        assert base_chain.sent == []
