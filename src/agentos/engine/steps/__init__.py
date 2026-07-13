"""Pre-turn pipeline steps."""

from agentos.engine.pipeline import TurnContext
from agentos.engine.steps.inject_platform_hint import inject_platform_hint
from agentos.engine.steps.inject_subagent_grounding import inject_subagent_grounding
from agentos.engine.steps.prompt_cache import apply_prompt_cache
from agentos.engine.steps.reasoning_hint_observer import observe_reasoning_hint
from agentos.engine.steps.resolve_model import resolve_model
from agentos.engine.steps.skills_filter import filter_skills

try:
    from agentos.engine.steps.agentos_router import apply_agentos_router
except ImportError:

    async def apply_agentos_router(ctx: TurnContext) -> TurnContext:
        return ctx


__all__ = [
    "apply_prompt_cache",
    "apply_agentos_router",
    "filter_skills",
    "inject_platform_hint",
    "inject_subagent_grounding",
    "observe_reasoning_hint",
    "resolve_model",
]
