"""Issue #3503: a NaN or negative value walked straight through the guard.

``float()`` parses ``"NaN"`` and ``"Infinity"`` out of the price feed's body,
and a NaN makes every comparison it takes part in false: ``spent + value >
cap``, ``value > threshold`` and the price-impact test all said no, so
``evaluate`` fell through to its final ``allow``. An agent swap of unknown
size executed with no approval and no cap check.

Both ends are pinned here: the feed cannot produce such a number any more,
and the guard refuses to decide on one whoever hands it over.
"""

from __future__ import annotations

import pytest

from agentos.trading import guardrails
from agentos.trading.prices import _f, _price

#: Values that are not a USD amount anyone can decide on.
UNUSABLE = [
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="inf"),
    pytest.param(float("-inf"), id="minus_inf"),
    pytest.param(-1e9, id="large_negative"),
    pytest.param(-0.01, id="small_negative"),
]


def _swap(**kw: object) -> guardrails.GuardVerdict:
    base: dict[str, object] = {
        "initiator": "agent",
        "value_usd": 50.0,
        "threshold_usd": 25.0,
        "daily_cap_usd": 100.0,
        "spent_today_usd": 0.0,
        "price_impact_pct": 0.1,
    }
    base.update(kw)
    return guardrails.evaluate(**base)  # type: ignore[arg-type]


# ── the report ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", UNUSABLE)
def test_an_unusable_value_never_reaches_allow(value: float) -> None:
    """The heart of it: a swap the guard cannot price waits for a human."""
    verdict = _swap(value_usd=value)

    assert verdict.decision == "needs_approval"
    assert verdict.reason == "value unusable (price is not a finite positive number)"


@pytest.mark.parametrize("value", UNUSABLE)
def test_an_unusable_value_is_not_reported_as_the_orders_worth(value: float) -> None:
    """The verdict is the audit record; it must not carry a NaN as a price."""
    assert _swap(value_usd=value).value_usd is None


@pytest.mark.parametrize("value", UNUSABLE)
def test_the_transfer_and_lp_guards_treat_it_the_same_way(value: float) -> None:
    """Both already ended in ``needs_approval``, but on the value they were
    handed rather than on knowing it was unusable."""
    transfer = guardrails.evaluate_transfer(
        initiator="agent", value_usd=value, daily_cap_usd=100.0, spent_today_usd=0.0
    )
    add = guardrails.evaluate_lp_write(
        op="add", initiator="agent", value_usd=value, daily_cap_usd=100.0, spent_today_usd=0.0
    )

    assert transfer.decision == "needs_approval"
    assert transfer.value_usd is None
    assert add.decision == "needs_approval"
    assert add.value_usd is None


@pytest.mark.parametrize("value", UNUSABLE)
def test_an_unusable_spend_to_date_stops_an_agent_order(value: float) -> None:
    """The cap cannot be applied against a number nobody can read, and the
    old ``max(0.0, nan)`` quietly answered "nothing spent today"."""
    verdict = _swap(value_usd=10.0, spent_today_usd=value)

    assert verdict.decision == "needs_approval"
    assert verdict.reason == "spend so far today is unknown"


# ── the feed cannot produce one ────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["NaN", "nan", "Infinity", "inf", "-inf"])
def test_a_non_finite_field_from_the_feed_is_not_a_number(raw: str) -> None:
    assert _f(raw) is None
    assert _price(raw) is None


@pytest.mark.parametrize("raw", ["-5", "-0.0001", "0", "0.0"])
def test_a_price_must_be_above_zero(raw: str) -> None:
    """Zero or below is not a price a position can be valued at; the callers
    already read ``None`` as "unpriced", which is what it is."""
    assert _price(raw) is None


def test_a_negative_number_is_still_fine_where_one_makes_sense() -> None:
    """``_f`` also carries the 24h change, which is negative half the time,
    so only the finiteness check belongs there."""
    assert _f("-12.5") == -12.5
    assert _f("0") == 0.0


@pytest.mark.parametrize("raw", ["1.25", 1.25, "0.000001"])
def test_an_ordinary_price_is_unchanged(raw: object) -> None:
    assert _price(raw) == float(raw)


# ── what must not change ────────────────────────────────────────────────────


def test_a_priced_order_still_decides_the_way_it_did() -> None:
    assert _swap(value_usd=10.0).decision == "allow"
    assert _swap(value_usd=50.0).decision == "needs_approval"
    assert _swap(value_usd=1e9).decision == "blocked_daily_cap"
    assert _swap(value_usd=None).reason == "value unknown (no price)"


def test_a_manual_order_is_still_the_users_own_decision() -> None:
    """Manual is checked before anything is measured, and stays that way."""
    assert _swap(initiator="manual", value_usd=float("nan")).decision == "allow"
    assert _swap(initiator="manual", value_usd=1e9).decision == "allow"


def test_a_zero_cap_still_switches_the_agent_off_before_pricing() -> None:
    assert _swap(value_usd=float("nan"), daily_cap_usd=0.0).decision == "blocked_daily_cap"


def test_the_spend_floor_still_holds_for_an_ordinary_negative() -> None:
    """A negative spend is unusable, not silently clamped to zero."""
    assert _swap(spent_today_usd=-5.0).reason == "spend so far today is unknown"
