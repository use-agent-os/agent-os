"""Observe model-family reasoning prompt hints without changing the prompt."""

from __future__ import annotations

from agentos.engine.pipeline import TurnContext
from agentos.engine.reasoning_hint import reasoning_tag_hint


async def observe_reasoning_hint(ctx: TurnContext) -> TurnContext:
    """Record nullable reasoning-hint telemetry without changing the prompt."""

    hint = reasoning_tag_hint(ctx.model)
    if hint is not None:
        ctx.metadata["reasoning_hint_resolved"] = hint
    return ctx
