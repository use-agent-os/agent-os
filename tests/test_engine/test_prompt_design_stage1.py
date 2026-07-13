from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentos.engine.pipeline import TurnContext
from agentos.engine.reasoning_hint import model_family, reasoning_tag_hint
from agentos.engine.runtime import TurnRunner
from agentos.engine.steps.inject_platform_hint import inject_platform_hint
from agentos.engine.steps.prompt_cache import apply_prompt_cache
from agentos.engine.steps.reasoning_hint_observer import observe_reasoning_hint


@pytest.mark.asyncio
async def test_prompt_cache_records_dynamic_and_dual_track_fields() -> None:
    ctx = TurnContext(
        message="hi",
        session_key="agent:main:whatsapp:direct:u1",
        config=SimpleNamespace(prompt_cache=SimpleNamespace(effective_mode="auto")),
        provider=SimpleNamespace(provider_name="openai"),
        model="gpt-5.5",
        tool_defs=[],
        system_prompt=("base", "dynamic"),
        metadata={"platform_markdown_hint": "whatsapp"},
    )

    ctx = await apply_prompt_cache(ctx)

    assert ctx.metadata["cache_base_hash"]
    assert ctx.metadata["cache_dynamic_hash"]
    assert ctx.metadata["resolved_model"] == "gpt-5.5"
    assert ctx.metadata["alias_resolution_chain"] == ["gpt-5.5"]
    assert ctx.metadata["provider_after_rewrite"] == "openai"
    assert ctx.metadata["cache_legacy_hash"]
    assert ctx.metadata["cache_shadow_final_hash"]
    assert ctx.metadata["cache_key_collision"] is False


@pytest.mark.asyncio
async def test_platform_hint_appends_plain_text_channel_guidance_to_dynamic_suffix() -> None:
    ctx = TurnContext(
        message="hi",
        session_key="agent:main:whatsapp:direct:u1",
        config=SimpleNamespace(prompt=SimpleNamespace(platform_hint_enabled=True)),
        provider=None,
        model="gpt-5.5",
        tool_defs=[],
        system_prompt=("base", "existing"),
        metadata={"channel_kind": "whatsapp"},
    )

    ctx = await inject_platform_hint(ctx)

    assert isinstance(ctx.system_prompt, tuple)
    assert ctx.system_prompt[0] == "base"
    assert "existing" in ctx.system_prompt[1]
    assert "Reply in plain text" in ctx.system_prompt[1]
    assert ctx.metadata["platform_markdown_hint"] == "whatsapp"


@pytest.mark.asyncio
async def test_platform_hint_respects_kill_switch() -> None:
    ctx = TurnContext(
        message="hi",
        session_key="agent:main:sms:direct:u1",
        config=SimpleNamespace(prompt=SimpleNamespace(platform_hint_enabled=False)),
        provider=None,
        model="gpt-5.5",
        tool_defs=[],
        system_prompt="base",
        metadata={"channel_kind": "sms"},
    )

    ctx = await inject_platform_hint(ctx)

    assert ctx.system_prompt == "base"
    assert ctx.metadata["inject_platform_hint__applied"] is False


def test_reasoning_hint_model_family_detection() -> None:
    assert model_family("openai/gpt-5.5") == "gpt-5"
    assert model_family("glm-4.7-air") == "glm-4.7"
    assert model_family("deepseek/deepseek-r1") == "deepseek-r1"
    assert reasoning_tag_hint("plain-model") is None


def test_docs_prompt_hint_is_disabled_when_public_docs_are_not_shipped() -> None:
    assert TurnRunner._resolve_docs_path() is None


@pytest.mark.asyncio
async def test_reasoning_hint_observer_writes_nullable_metadata_only() -> None:
    ctx = TurnContext(
        message="hi",
        session_key="agent:main:webchat:default",
        config=SimpleNamespace(),
        provider=None,
        model="openai/gpt-5.5",
        tool_defs=[],
        system_prompt="base",
    )

    ctx = await observe_reasoning_hint(ctx)

    assert ctx.system_prompt == "base"
    assert "<final>" in ctx.metadata["reasoning_hint_resolved"]
