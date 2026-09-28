"""Code-enforced limits on agent-initiated swaps.

Pure functions: the service feeds them the numbers, they answer with a
decision. Nothing here reads prompt text, and — since the gateway now decides
who is an agent (``gateway.agent_surface``) — nothing the prompt writes can
change which branch runs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

Decision = Literal["allow", "needs_approval", "blocked_daily_cap"]
Initiator = Literal["manual", "agent"]

# Agent orders above this price impact wait for a human even when they are
# under the USD threshold: a thin pool is where a sandwich lives.
DEFAULT_AGENT_MAX_PRICE_IMPACT_PCT = 5.0
# An agent may not set slippage above this; the order is refused, not queued.
DEFAULT_AGENT_MAX_SLIPPAGE_PCT = 5.0


def _usd(value: float | None) -> float | None:
    """A USD amount the comparisons below can trust, or ``None``.

    A NaN makes ``spent + value > cap``, ``value > threshold`` and every other
    test here false at once, so an order carrying one fell through to the
    final ``allow`` -- past the approval threshold and the daily cap both
    (#3503). A negative amount is nonsense in the same way. Neither is a
    number to decide on, so both become "unpriced", which every caller
    already routes to a human.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


@dataclass(frozen=True)
class GuardVerdict:
    decision: Decision
    value_usd: float | None
    spent_today_usd: float
    daily_cap_usd: float
    threshold_usd: float
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "valueUsd": self.value_usd,
            "spentTodayUsd": round(self.spent_today_usd, 2),
            "dailyCapUsd": self.daily_cap_usd,
            "thresholdUsd": self.threshold_usd,
            "reason": self.reason,
        }


def evaluate(
    *,
    initiator: str,
    value_usd: float | None,
    threshold_usd: float,
    daily_cap_usd: float,
    spent_today_usd: float,
    price_impact_pct: float | None = None,
    max_price_impact_pct: float = DEFAULT_AGENT_MAX_PRICE_IMPACT_PCT,
) -> GuardVerdict:
    """Decide what happens to a swap worth ``value_usd``.

    * Manual swaps are the user's own decision: always allowed.
    * A daily cap of zero means the agent may not swap at all. "No cap" is
      not a number this function knows; a user who types 0 means stop.
    * An agent swap whose value cannot be priced fails closed into approval.
    * An agent swap that would push the wallet over its daily cap is refused
      outright (not queued: a queue would let the agent keep piling up asks).
      ``spent_today_usd`` includes orders still in flight, so a burst cannot
      slip under the cap by racing its own confirmations.
    * Above the per-order threshold, or above the price-impact ceiling, the
      swap waits for a human — and so does one whose price impact could not
      be computed at all.
    """
    priced = _usd(value_usd)
    spent_known = _usd(spent_today_usd)
    spent = spent_known if spent_known is not None else 0.0
    cap = float(daily_cap_usd)
    threshold = float(threshold_usd)

    def verdict(decision: Decision, reason: str) -> GuardVerdict:
        return GuardVerdict(
            decision=decision,
            value_usd=priced,
            spent_today_usd=spent,
            daily_cap_usd=cap,
            threshold_usd=threshold,
            reason=reason,
        )

    if initiator == "manual":
        return verdict("allow", "manual")
    if cap <= 0:
        return verdict("blocked_daily_cap", "daily cap is 0 USD: agent swaps are switched off")
    if spent_known is None:
        # Without today's spend the cap cannot be applied, and an agent order
        # does not get to run on a number nobody could read.
        return verdict("needs_approval", "spend so far today is unknown")
    if priced is None:
        return verdict(
            "needs_approval",
            "value unknown (no price)"
            if value_usd is None
            else "value unusable (price is not a finite positive number)",
        )
    value = priced
    if spent + value > cap:
        return verdict(
            "blocked_daily_cap",
            f"daily cap {cap:.2f} USD would be exceeded ({spent:.2f} spent + {value:.2f})",
        )
    if threshold >= 0 and value > threshold:
        return verdict(
            "needs_approval",
            f"order {value:.2f} USD is above the {threshold:.2f} USD threshold",
        )
    if price_impact_pct is not None and float(price_impact_pct) > float(max_price_impact_pct):
        return verdict(
            "needs_approval",
            f"price impact {float(price_impact_pct):.2f}% is above "
            f"{float(max_price_impact_pct):.2f}%",
        )
    if price_impact_pct is None:
        # No reference price for one side means the impact ceiling could not
        # be checked at all; an agent does not get to trade blind.
        return verdict("needs_approval", "price impact unknown (no reference price for one side)")
    return verdict("allow", "within limits")


def evaluate_transfer(
    *,
    initiator: str,
    value_usd: float | None,
    daily_cap_usd: float,
    spent_today_usd: float,
) -> GuardVerdict:
    """Decide what happens to a transfer (a send, or a batch of sends) worth ``value_usd``.

    A swap keeps the money in the wallet as something else; a transfer is
    gone the moment it mines. So the per-order threshold does not apply here:

    * Manual transfers are the user's own decision: always allowed.
    * A daily cap of zero switches the agent's transfers off with its swaps.
    * An agent transfer that would push the wallet over its daily cap is
      refused outright, like a swap. ``spent_today_usd`` includes orders
      still in flight; ``value_usd`` is the **whole batch** — a multisend is
      judged once, on its total, so splitting it changes nothing.
    * Every other agent transfer waits for a human, priced or not.
    """
    priced = _usd(value_usd)
    spent = _usd(spent_today_usd) or 0.0
    cap = float(daily_cap_usd)

    def verdict(decision: Decision, reason: str) -> GuardVerdict:
        return GuardVerdict(
            decision=decision,
            value_usd=priced,
            spent_today_usd=spent,
            daily_cap_usd=cap,
            threshold_usd=0.0,
            reason=reason,
        )

    if initiator == "manual":
        return verdict("allow", "manual")
    if cap <= 0:
        return verdict("blocked_daily_cap", "daily cap is 0 USD: agent transfers are switched off")
    if priced is None:
        return verdict("needs_approval", "value unknown (no price); a transfer always waits")
    value = priced
    if spent + value > cap:
        return verdict(
            "blocked_daily_cap",
            f"daily cap {cap:.2f} USD would be exceeded ({spent:.2f} spent + {value:.2f})",
        )
    return verdict("needs_approval", "a transfer leaves the wallet for good; it waits for you")


def evaluate_revoke(*, initiator: str) -> GuardVerdict:
    """A revoke spends only gas, but it is still the agent writing to chain."""
    decision: Decision = "allow" if initiator == "manual" else "needs_approval"
    return GuardVerdict(
        decision=decision,
        value_usd=0.0,
        spent_today_usd=0.0,
        daily_cap_usd=0.0,
        threshold_usd=0.0,
        reason="manual" if initiator == "manual" else "the agent may not revoke on its own",
    )


LpOp = Literal["collect", "remove", "add"]


def evaluate_lp_write(
    *,
    op: str,
    initiator: str,
    value_usd: float | None,
    daily_cap_usd: float,
    spent_today_usd: float,
) -> GuardVerdict:
    """Decide what happens to a Uniswap V4 LP write (``docs/lp-write.md``).

    Every LP write waits for a human, whoever asked and whatever it is worth:
    it signs a call whose effect is a pool's, not a transfer the user can read
    off a line. ``collect`` and ``remove`` bring tokens *in* and never touch
    the daily cap. ``add`` deposits: from the agent it counts toward the cap
    like a swap -- a cap of zero switches it off, and one that would push the
    wallet past the cap is refused outright (``spent_today_usd`` includes
    orders still in flight). A person's own add is not capped, like a
    person's swap; it still parks.
    """
    priced = _usd(value_usd)
    spent = _usd(spent_today_usd) or 0.0
    cap = float(daily_cap_usd)

    def verdict(decision: Decision, reason: str) -> GuardVerdict:
        return GuardVerdict(
            decision=decision,
            value_usd=priced,
            spent_today_usd=spent,
            daily_cap_usd=cap,
            threshold_usd=0.0,
            reason=reason,
        )

    if op not in ("collect", "remove", "add"):
        raise ValueError(f"unknown LP op {op!r}")
    if op == "add" and initiator != "manual":
        if cap <= 0:
            return verdict(
                "blocked_daily_cap", "daily cap is 0 USD: agent deposits are switched off"
            )
        if priced is not None and spent + priced > cap:
            return verdict(
                "blocked_daily_cap",
                f"daily cap {cap:.2f} USD would be exceeded ({spent:.2f} spent + {priced:.2f})",
            )
    return verdict("needs_approval", "an LP write always waits for you")
