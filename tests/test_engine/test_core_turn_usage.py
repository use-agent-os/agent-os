"""``settle_turn_usage`` reproduces the DoneEvent usage fields (issue #3817)."""

from __future__ import annotations

from dataclasses import dataclass

from agentos.engine.core import (
    TurnTotals,
    cost_source_for_usage,
    resolve_done_model,
    settle_turn_usage,
)


@dataclass(frozen=True)
class _Price:
    input_per_m: float = 0.0
    output_per_m: float = 0.0


@dataclass(frozen=True)
class _Delta:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    billed_cost: float = 0.0


TOTALS = TurnTotals(
    input_tokens=1_000_000,
    output_tokens=500_000,
    cached_tokens=7,
    cache_write_tokens=3,
)


def test_provider_billed_cost_wins_over_the_estimate() -> None:
    totals = TurnTotals(input_tokens=10, output_tokens=5, billed_cost=0.25)

    usage = settle_turn_usage(totals, model="m", price=_Price(1.0, 2.0))

    assert (usage.cost_usd, usage.cost_source, usage.billed_cost) == (0.25, "provider_billed", 0.25)


def test_static_estimate_prices_the_turn_totals() -> None:
    usage = settle_turn_usage(TOTALS, model="m", price=_Price(2.0, 4.0))

    assert usage.cost_usd == 4.0
    assert usage.cost_source == "agentos_static_estimate"
    assert (usage.input_tokens, usage.cached_tokens, usage.cache_write_tokens) == (1_000_000, 7, 3)


def test_no_price_and_no_bill_is_unavailable() -> None:
    usage = settle_turn_usage(TOTALS, model="m", price=_Price())

    assert (usage.cost_usd, usage.cost_source) == (0.0, "unavailable")


def test_tracker_delta_with_usage_replaces_the_turn_totals() -> None:
    delta = _Delta(
        input_tokens=11,
        output_tokens=12,
        cache_read_tokens=13,
        cache_write_tokens=14,
        cost_usd=0.5,
        billed_cost=0.5,
    )

    usage = settle_turn_usage(TOTALS, model="m", price=_Price(2.0, 4.0), delta=delta)

    assert (usage.input_tokens, usage.output_tokens) == (11, 12)
    assert (usage.cached_tokens, usage.cache_write_tokens) == (13, 14)
    assert (usage.cost_usd, usage.billed_cost, usage.cost_source) == (0.5, 0.5, "provider_billed")


def test_an_empty_tracker_delta_is_ignored() -> None:
    usage = settle_turn_usage(TOTALS, model="m", price=_Price(2.0, 4.0), delta=_Delta())

    assert usage.input_tokens == 1_000_000
    assert usage.cost_source == "agentos_static_estimate"


def test_cost_source_for_tracker_costs() -> None:
    assert cost_source_for_usage(0.5, 0.5) == "provider_billed"
    assert cost_source_for_usage(0.7, 0.5) == "mixed"
    assert cost_source_for_usage(0.7, 0.0) == "agentos_estimate"
    assert cost_source_for_usage(0.0, 0.0) == "unavailable"


def test_done_model_takes_the_first_known_id() -> None:
    assert resolve_done_model("actual", "tracked", "configured") == "actual"
    assert resolve_done_model(None, "tracked", "configured") == "tracked"
    assert resolve_done_model("", None, "configured") == "configured"
    assert resolve_done_model(None, None, None) == ""
