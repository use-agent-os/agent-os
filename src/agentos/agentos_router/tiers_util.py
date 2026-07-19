"""Shared tier-resolution helpers for router strategies.

``_find_valid_tier`` walks ``TIER_ORDER`` upward to honour a ``valid_tiers``
allowlist: given a desired starting tier, it returns the nearest configured
tier at or above it, falling back to any configured tier (in canonical order)
when the desired tier sits above every valid one. Extracted here so
``V4Phase3Strategy`` and ``PilotStrategy`` share one implementation rather than
each carrying a private copy.
"""

from __future__ import annotations

from agentos.agentos_router.controller import TIER_ORDER
from agentos.router_tiers import DEFAULT_TEXT_TIER


def _find_valid_tier(start_tier: str, valid_tiers: list[str]) -> str:
    if not valid_tiers:
        return DEFAULT_TEXT_TIER
    start_idx = TIER_ORDER.index(start_tier) if start_tier in TIER_ORDER else 1
    for idx in range(start_idx, len(TIER_ORDER)):
        if TIER_ORDER[idx] in valid_tiers:
            return TIER_ORDER[idx]
    for tier in TIER_ORDER:
        if tier in valid_tiers:
            return tier
    return valid_tiers[0]
