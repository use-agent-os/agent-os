"""``PromptStepRegistry``: the ordered pre-turn prompt steps (#3819)."""

from __future__ import annotations

import pytest

import agentos.engine.steps as steps_module
from agentos.engine.pipeline import DEFAULT_PROMPT_STEPS, PromptStepRegistry, TurnContext


def test_the_default_order_is_the_historical_one() -> None:
    assert DEFAULT_PROMPT_STEPS.names == (
        "resolve_model",
        "apply_agentos_router",
        "observe_reasoning_hint",
        "filter_skills",
        "inject_subagent_grounding",
        "inject_env_probe",
        "inject_platform_hint",
        "inject_plan_mode",
        "apply_prompt_cache",
    )


def test_steps_are_looked_up_when_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    async def replaced(ctx: TurnContext) -> TurnContext:
        return ctx

    monkeypatch.setattr(steps_module, "filter_skills", replaced)

    resolved = DEFAULT_PROMPT_STEPS.resolve()

    assert resolved[DEFAULT_PROMPT_STEPS.names.index("filter_skills")] is replaced
    assert resolved[0] is steps_module.resolve_model


def test_an_override_replaces_one_named_step() -> None:
    async def bounded(ctx: TurnContext) -> TurnContext:
        return ctx

    resolved = DEFAULT_PROMPT_STEPS.resolve({"apply_agentos_router": bounded})

    assert resolved[1] is bounded
    assert len(resolved) == len(DEFAULT_PROMPT_STEPS.names)


def test_a_duplicate_step_name_is_refused() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        PromptStepRegistry(("a", "b", "a"))
