from __future__ import annotations

from decimal import Decimal

import pytest

from agentos.trading import chains, guardrails
from agentos.trading.pnl import (
    Lot,
    format_amount,
    format_ratio,
    holding_from_lots,
    per_raw,
    per_token,
    sell_fifo,
    to_human,
    to_raw,
)


class TestChains:
    def test_resolve_accepts_ids_keys_and_aliases(self) -> None:
        assert chains.resolve_chain(8453) is chains.BASE
        assert chains.resolve_chain("base") is chains.BASE
        assert chains.resolve_chain("4663") is chains.ROBINHOOD
        assert chains.resolve_chain("Robinhood Chain") is chains.ROBINHOOD
        assert chains.resolve_chain("hood") is chains.ROBINHOOD

    def test_unknown_chain_rejected(self) -> None:
        with pytest.raises(chains.UnsupportedChainError):
            chains.resolve_chain(1)
        with pytest.raises(chains.UnsupportedChainError):
            chains.resolve_chain("solana")
        with pytest.raises(chains.UnsupportedChainError):
            chains.resolve_chain(None)

    def test_rpc_override_by_id_or_key(self) -> None:
        assert chains.rpc_url_for(chains.BASE, None) == chains.BASE.rpc_url
        assert chains.rpc_url_for(chains.BASE, {"8453": "https://x"}) == "https://x"
        assert chains.rpc_url_for(chains.BASE, {"base": "https://y"}) == "https://y"
        assert chains.rpc_url_for(chains.BASE, {"4663": "https://z"}) == chains.BASE.rpc_url

    def test_address_helpers(self) -> None:
        assert chains.normalize_address("ETH") == chains.NATIVE_ADDRESS
        assert chains.normalize_address("0xABCDEF0000000000000000000000000000000001").endswith(
            "0001"
        )
        with pytest.raises(ValueError):
            chains.normalize_address("0x123")
        with pytest.raises(ValueError):
            chains.normalize_address("0xzz00000000000000000000000000000000000001")
        checksummed = chains.checksum_address("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913")
        assert checksummed == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    def test_mixed_case_addresses_must_pass_the_eip55_checksum(self) -> None:
        """One wrong letter in a pasted recipient is a different, valid-looking address."""
        good = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        assert chains.normalize_address(good) == good.lower()
        # Same hex, a checksum that does not match: refused with a message a
        # user can act on, as the same ValueError the callers already map.
        bad = "0x833589fcd6edb6E08f4c7C32D4f71b54bdA02913"
        with pytest.raises(ValueError, match="checksum does not match"):
            chains.normalize_address(bad)
        # Lower- and upper-case make no checksum claim and are accepted as before.
        assert chains.normalize_address(good.lower()) == good.lower()
        assert chains.normalize_address("0x" + good[2:].upper()) == good.lower()
        # Well-known constants stay valid through the same path.
        from agentos.trading.aggregator import ALLOWANCE_HOLDER, NATIVE_SENTINEL
        from agentos.trading.providers import PERMIT2
        from agentos.trading.uniswap import PROXY_SPENDER

        for address in (NATIVE_SENTINEL, PERMIT2, PROXY_SPENDER, ALLOWANCE_HOLDER):
            assert chains.normalize_address(address) == address.lower()

    def test_explorer_urls(self) -> None:
        assert chains.ROBINHOOD.tx_url("0xabc") == "https://robin.etherscan.io/tx/0xabc"
        assert chains.BASE.token_url("0x1") == "https://basescan.org/token/0x1"


class TestAmounts:
    def test_round_trip(self) -> None:
        assert to_raw("1.5", 6) == 1_500_000
        assert to_raw("0.000001", 6) == 1
        assert to_raw("1", 18) == 10**18
        assert to_human(1_500_000, 6) == Decimal("1.5")
        assert format_amount(1_500_000, 6) == "1.5"
        assert format_amount(10**18, 18) == "1"
        assert format_amount(123456789012345678, 18) == "0.12345678"
        assert format_amount(0, 18) == "0"

    def test_amounts_wider_than_28_digits_are_exact(self) -> None:
        """The default Decimal context keeps 28 significant digits.

        Ten billion tokens at 18 decimals is already 29 digits of base units;
        a 100B-supply token (Bankr and Clanker launches) is 30. Rounding there
        must not move a base unit, and must never round up.
        """
        supply = 10**29  # 100,000,000,000 tokens at 18 decimals
        assert to_human(supply - 1, 18) == Decimal("99999999999.999999999999999999")
        assert to_raw("99999999999.999999999999999999", 18) == supply - 1
        assert to_raw("12345678901.123456789012345678", 18) == 12345678901123456789012345678
        assert format_amount(supply - 1, 18) == "99999999999.99999999"
        assert format_amount(supply - 1, 18, max_places=18) == "99999999999.999999999999999999"
        assert format_amount(2**256 - 1, 18).startswith(
            "115792089237316195423570985008687907853269"
        )
        assert format_amount(-(supply - 1), 18) == "-99999999999.99999999"

    def test_ratio_wider_than_28_digits(self) -> None:
        assert format_ratio(Decimal(10**17), Decimal(1)) == str(10**17)
        assert format_ratio(Decimal("2"), Decimal("3")) == "0.666666666666"

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValueError):
            to_raw("-1", 6)

    @pytest.mark.parametrize("text", ["NaN", "nan", "Infinity", "-Infinity", "inf", "abc", ""])
    def test_non_numbers_are_a_value_error(self, text: str) -> None:
        """``Decimal`` accepts NaN and Infinity; the callers only map ValueError."""
        with pytest.raises(ValueError):
            to_raw(text, 6)

    def test_per_raw_per_token(self) -> None:
        assert per_raw(2000.0, 18) == pytest.approx(2e-15)
        assert per_token(per_raw(2000.0, 18), 18) == pytest.approx(2000.0)


class TestFifo:
    def test_partial_sells_consume_oldest_first(self) -> None:
        lots = [
            Lot(1, 100, per_raw(1.0, 0), acquired_at=1),
            Lot(2, 100, per_raw(2.0, 0), acquired_at=2),
        ]
        result = sell_fifo(lots, 150, proceeds_usd=450.0)
        assert [c.lot_id for c in result.consumed] == [1, 2]
        assert result.cost_usd == pytest.approx(100 * 1.0 + 50 * 2.0)
        assert result.proceeds_usd == pytest.approx(450.0)
        assert result.pnl_usd == pytest.approx(250.0)
        assert result.unmatched_raw == 0
        assert lots[0].amount_raw == 0
        assert lots[1].amount_raw == 50

    def test_oversell_reports_unmatched_and_scales_proceeds(self) -> None:
        lots = [Lot(1, 100, 1.0, acquired_at=1)]
        result = sell_fifo(lots, 200, proceeds_usd=400.0)
        assert result.unmatched_raw == 100
        assert result.proceeds_usd == pytest.approx(200.0)
        assert result.cost_usd == pytest.approx(100.0)

    def test_zero_or_no_proceeds(self) -> None:
        lots = [Lot(1, 10, 1.0, acquired_at=1)]
        assert sell_fifo(lots, 0, 10.0).consumed == []
        result = sell_fifo(lots, 5, None)
        assert result.proceeds_usd == 0.0
        assert result.cost_usd == pytest.approx(5.0)

    def test_holding_math(self) -> None:
        lots = [Lot(1, 2 * 10**18, per_raw(1000.0, 18), 1), Lot(2, 10**18, per_raw(3000.0, 18), 2)]
        holding = holding_from_lots(lots, decimals=18, price_usd=2000.0, realized_usd=10.0)
        assert holding.amount == pytest.approx(3.0)
        assert holding.cost_usd == pytest.approx(5000.0)
        assert holding.value_usd == pytest.approx(6000.0)
        assert holding.avg_cost_usd == pytest.approx(5000.0 / 3)
        assert holding.unrealized_usd == pytest.approx(1000.0)
        assert holding.unrealized_pct == pytest.approx(20.0)
        unknown = holding_from_lots(lots, decimals=18, price_usd=None, realized_usd=0.0)
        assert unknown.value_usd is None and unknown.unrealized_pct is None
        assert unknown.unrealized_usd is None

    def test_free_lots_are_all_unrealized_gain(self) -> None:
        """An airdrop (cost 0) with a price is pure gain; its percentage is undefined."""
        free = holding_from_lots(
            [Lot(1, 10**18, 0.0, 1)], decimals=18, price_usd=3.0, realized_usd=0.0
        )
        assert free.cost_usd == 0.0
        assert free.value_usd == pytest.approx(3.0)
        assert free.unrealized_usd == pytest.approx(3.0)
        assert free.unrealized_pct is None
        assert free.avg_cost_usd is None


class TestGuardrails:
    def _eval(self, **kw: object) -> guardrails.GuardVerdict:
        base = {
            "initiator": "agent",
            "value_usd": 50.0,
            "threshold_usd": 100.0,
            "daily_cap_usd": 1000.0,
            "spent_today_usd": 0.0,
            # A known, small impact: these cases are about value, not impact.
            "price_impact_pct": 0.5,
        }
        base.update(kw)
        return guardrails.evaluate(**base)  # type: ignore[arg-type]

    def test_manual_always_allowed(self) -> None:
        assert self._eval(initiator="manual", value_usd=1e9).decision == "allow"
        assert self._eval(initiator="manual", value_usd=None).decision == "allow"

    def test_unknown_value_fails_closed(self) -> None:
        verdict = self._eval(value_usd=None)
        assert verdict.decision == "needs_approval"
        assert "unknown" in verdict.reason

    def test_daily_cap_blocks_before_threshold(self) -> None:
        verdict = self._eval(value_usd=500.0, spent_today_usd=600.0)
        assert verdict.decision == "blocked_daily_cap"
        assert self._eval(value_usd=50.0, spent_today_usd=950.0).decision == "allow"
        assert self._eval(value_usd=50.01, spent_today_usd=950.0).decision == "blocked_daily_cap"

    def test_threshold(self) -> None:
        assert self._eval(value_usd=100.0).decision == "allow"
        assert self._eval(value_usd=100.01).decision == "needs_approval"

    def test_zero_cap_switches_agent_swaps_off(self) -> None:
        # 0 is "stop", not "unlimited": a user who types 0 means no agent trades.
        verdict = self._eval(value_usd=1.0, daily_cap_usd=0.0, spent_today_usd=0.0)
        assert verdict.decision == "blocked_daily_cap"
        assert "switched off" in verdict.reason
        assert self._eval(initiator="manual", daily_cap_usd=0.0).decision == "allow"

    def test_price_impact_above_ceiling_needs_approval(self) -> None:
        assert self._eval(value_usd=10.0, price_impact_pct=4.9).decision == "allow"
        verdict = self._eval(value_usd=10.0, price_impact_pct=5.1)
        assert verdict.decision == "needs_approval" and "impact" in verdict.reason
        assert (
            self._eval(value_usd=10.0, price_impact_pct=5.1, max_price_impact_pct=10).decision
            == "allow"
        )
        # The cap and the USD threshold are judged first; impact never lowers them.
        assert (
            self._eval(value_usd=500.0, spent_today_usd=600.0, price_impact_pct=0.1).decision
            == "blocked_daily_cap"
        )

    def test_dict_shape(self) -> None:
        payload = self._eval().to_dict()
        assert set(payload) == {
            "decision",
            "valueUsd",
            "spentTodayUsd",
            "dailyCapUsd",
            "thresholdUsd",
            "reason",
        }
