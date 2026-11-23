"""The price-impact ceiling does not park an agent order too small to move a pool.

On a $0.05 DCA buy, ``1 - out_usd / in_usd`` is the route's fee plus how far
the reference feed lags the pool, not price impact: the order cannot move the
pool. Those buys parked for approval at 5-9% "impact" and expired unattended.
Under ``agent_price_impact_floor_usd`` the impact checks are waived; the most
such an order can lose to its route is its own value, and the daily cap still
bounds the day.
"""

from __future__ import annotations

import pytest

from agentos.trading import guardrails


def _eval(**kw: object) -> guardrails.GuardVerdict:
    base: dict[str, object] = {
        "initiator": "agent",
        "value_usd": 0.05,
        "threshold_usd": 100.0,
        "daily_cap_usd": 1000.0,
        "spent_today_usd": 0.0,
        "price_impact_pct": 9.44,
    }
    base.update(kw)
    return guardrails.evaluate(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "impact",
    [
        pytest.param(9.44, id="above_ceiling"),
        pytest.param(None, id="unknown"),
        pytest.param(float("nan"), id="not_finite"),
    ],
)
def test_an_order_under_the_floor_skips_the_impact_checks(impact: float | None) -> None:
    verdict = _eval(price_impact_pct=impact)
    assert verdict.decision == "allow"
    assert "price-impact floor" in verdict.reason


def test_the_floor_is_inclusive_and_defaults_to_one_dollar() -> None:
    assert guardrails.DEFAULT_AGENT_PRICE_IMPACT_FLOOR_USD == 1.0
    assert _eval(value_usd=1.0).decision == "allow"
    assert _eval(value_usd=1.01).decision == "needs_approval"


@pytest.mark.parametrize(
    "impact",
    [pytest.param(9.44, id="above_ceiling"), pytest.param(None, id="unknown")],
)
def test_an_order_over_the_floor_still_parks(impact: float | None) -> None:
    verdict = _eval(value_usd=10.0, price_impact_pct=impact)
    assert verdict.decision == "needs_approval"
    assert "impact" in verdict.reason


def test_a_zero_floor_restores_the_ceiling_for_every_order() -> None:
    verdict = _eval(price_impact_floor_usd=0.0)
    assert verdict.decision == "needs_approval"
    assert "impact" in verdict.reason


def test_the_floor_never_waives_the_cap_the_threshold_or_a_missing_price() -> None:
    assert _eval(daily_cap_usd=0.0).decision == "blocked_daily_cap"
    assert _eval(spent_today_usd=999.99).decision == "blocked_daily_cap"
    assert _eval(threshold_usd=0.01).decision == "needs_approval"
    assert _eval(value_usd=None).reason == "value unknown (no price)"
    assert _eval(spent_today_usd=float("nan")).reason == "spend so far today is unknown"


def test_a_floor_set_above_the_threshold_does_not_lift_the_threshold() -> None:
    verdict = _eval(value_usd=150.0, price_impact_floor_usd=1000.0)
    assert verdict.decision == "needs_approval"
    assert verdict.reason.endswith("threshold")
