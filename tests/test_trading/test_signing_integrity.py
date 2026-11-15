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
    FakeAggregator,
    FakeChain,
)


def _events(service: TradingService, name: str) -> list[dict[str, Any]]:
    return [payload for event, payload in service.events if event == name]  # type: ignore[attr-defined]


async def _agent_swap(service: TradingService, **extra: Any) -> dict[str, Any]:
    orders = await service.swap(
        chain=BASE,
        wallets=None,
        token_in=extra.pop("token_in", "USDC"),
        token_out=extra.pop("token_out", "WETH"),
        amount_in=extra.pop("amount_in", "10"),
        amount_pct=None,
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
