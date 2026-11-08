"""Pure pieces of the agent loop, split out of ``Agent._turn_generator``.

Issue #3817 moves the request/stream/decide loop into this package one piece
at a time. Everything here is deterministic and side-effect free: no
provider calls, no storage, no gateway. ``tests/test_engine/test_core_boundary.py``
enforces that ``agentos.engine.core`` never imports from ``agentos.gateway``.
"""

from __future__ import annotations

from agentos.engine.core.turn_usage import (
    Price,
    TurnTotals,
    TurnUsage,
    UsageDelta,
    cost_source_for_usage,
    resolve_done_model,
    settle_turn_usage,
)

__all__ = [
    "Price",
    "TurnTotals",
    "TurnUsage",
    "UsageDelta",
    "cost_source_for_usage",
    "resolve_done_model",
    "settle_turn_usage",
]
