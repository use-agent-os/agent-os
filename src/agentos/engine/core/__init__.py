"""Pure pieces of the agent loop, split out of ``Agent._turn_generator``.

Issue #3817 moved the request/stream/decide loop out of
``Agent._turn_generator``: ``loop.run_agent_loop`` runs a turn's iterations
over the steps of a ``LoopConfig``, ``stream.stream_attempt`` consumes one
provider stream, and ``turn_usage.settle_turn_usage`` settles the turn's
usage. Nothing here calls a provider, touches storage or knows the gateway.
``tests/test_engine/test_core_boundary.py`` enforces that
``agentos.engine.core`` never imports from ``agentos.gateway``.
"""

from __future__ import annotations

from agentos.engine.core.loop import LoopConfig, LoopState, run_agent_loop
from agentos.engine.core.stream import (
    AttemptResult,
    IterationStreamTimeoutError,
    StreamAccumulator,
    stream_attempt,
)
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
    "AttemptResult",
    "IterationStreamTimeoutError",
    "LoopConfig",
    "LoopState",
    "StreamAccumulator",
    "Price",
    "TurnTotals",
    "TurnUsage",
    "UsageDelta",
    "cost_source_for_usage",
    "resolve_done_model",
    "run_agent_loop",
    "settle_turn_usage",
    "stream_attempt",
]
