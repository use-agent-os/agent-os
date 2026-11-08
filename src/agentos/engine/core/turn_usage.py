"""Settle the token and cost figures a turn reports on its ``DoneEvent``.

A turn accumulates provider-reported totals while it runs. When it ends,
those totals are reconciled with the session usage tracker (whose per-turn
delta wins when it saw any usage, because it also covers side-task calls
accounted to the session) and priced. This module holds that reconciliation
as a pure function so the loop only has to gather its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class Price(Protocol):
    """Per-million-token prices (``agentos.engine.pricing.PriceEntry``)."""

    @property
    def input_per_m(self) -> float: ...

    @property
    def output_per_m(self) -> float: ...


class UsageDelta(Protocol):
    """The slice of a usage-tracker delta snapshot this module reads."""

    @property
    def input_tokens(self) -> int: ...

    @property
    def output_tokens(self) -> int: ...

    @property
    def cache_read_tokens(self) -> int: ...

    @property
    def cache_write_tokens(self) -> int: ...

    @property
    def cost_usd(self) -> float: ...

    @property
    def billed_cost(self) -> float: ...


@dataclass(frozen=True)
class TurnTotals:
    """Provider-reported usage summed over every attempt of one turn."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    billed_cost: float = 0.0


@dataclass(frozen=True)
class TurnUsage:
    """The usage and cost fields of a turn's ``DoneEvent``."""

    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cache_write_tokens: int
    cost_usd: float
    billed_cost: float
    cost_source: str


def cost_source_for_usage(cost_usd: float, billed_cost: float) -> str:
    """Where a tracker-derived cost came from."""

    if billed_cost > 0.0 and abs(cost_usd - billed_cost) <= 1e-9:
        return "provider_billed"
    if billed_cost > 0.0:
        return "mixed"
    if cost_usd > 0.0:
        return "agentos_estimate"
    return "unavailable"


def resolve_done_model(*candidates: str | None) -> str:
    """The first non-empty model id, in the caller's order of preference."""

    for candidate in candidates:
        if candidate:
            return candidate
    return ""


def _delta_has_usage(delta: UsageDelta | None) -> bool:
    return bool(
        delta
        and (
            delta.input_tokens
            or delta.output_tokens
            or delta.cache_read_tokens
            or delta.cache_write_tokens
            or delta.cost_usd
            or delta.billed_cost
        )
    )


def settle_turn_usage(
    totals: TurnTotals,
    *,
    model: str,
    price: Price,
    delta: UsageDelta | None = None,
) -> TurnUsage:
    """Reconcile *totals* with the tracker *delta* and price the turn.

    *price* is looked up by the caller (``lookup_price`` may consult a live
    catalog), which keeps this function free of I/O.

    Without a delta that saw usage, the turn's own totals are reported: a
    provider-billed cost wins over the static pricing-table estimate. With
    one, the delta's figures are reported as they are.
    """

    if _delta_has_usage(delta):
        assert delta is not None
        return TurnUsage(
            model=model,
            input_tokens=delta.input_tokens,
            output_tokens=delta.output_tokens,
            cached_tokens=delta.cache_read_tokens,
            cache_write_tokens=delta.cache_write_tokens,
            cost_usd=delta.cost_usd,
            billed_cost=delta.billed_cost,
            cost_source=cost_source_for_usage(delta.cost_usd, delta.billed_cost),
        )

    estimated = (
        totals.input_tokens * price.input_per_m + totals.output_tokens * price.output_per_m
    ) / 1_000_000
    if totals.billed_cost > 0.0:
        cost, source = totals.billed_cost, "provider_billed"
    elif estimated > 0.0:
        cost, source = estimated, "agentos_static_estimate"
    else:
        cost, source = 0.0, "unavailable"
    return TurnUsage(
        model=model,
        input_tokens=totals.input_tokens,
        output_tokens=totals.output_tokens,
        cached_tokens=totals.cached_tokens,
        cache_write_tokens=totals.cache_write_tokens,
        cost_usd=cost,
        billed_cost=totals.billed_cost,
        cost_source=source,
    )
