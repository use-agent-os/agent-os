"""inject_subagent_grounding pipeline step survives compaction.

Idempotently re-injects the subagent system-prompt grounding for any
recognized subagent session key. No-op for main sessions and when the
grounding text is already present.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agentos.engine.steps.inject_subagent_grounding import (
    _SUBAGENT_GROUNDING,
    inject_subagent_grounding,
)
from agentos.tools.builtin.sessions import _SUBAGENT_SYSTEM_PROMPT


@dataclass
class _MiniContext:
    session_key: str
    system_prompt: object
    metadata: dict = field(default_factory=dict)


def _ctx(session_key: str, system_prompt: object) -> _MiniContext:
    return _MiniContext(session_key=session_key, system_prompt=system_prompt)


@pytest.mark.asyncio
async def test_main_session_is_a_noop() -> None:
    ctx = _ctx("agent:main:main", "You are a helpful assistant.")
    out = await inject_subagent_grounding(ctx)
    assert out.system_prompt == "You are a helpful assistant."
    assert out.metadata.get("inject_subagent_grounding__applied") is False


@pytest.mark.asyncio
async def test_subagent_session_re_injects_when_missing() -> None:
    ctx = _ctx("agent:main:subagent:abcd1234", "Be helpful.")
    out = await inject_subagent_grounding(ctx)
    assert _SUBAGENT_GROUNDING in out.system_prompt
    assert "Be helpful." in out.system_prompt
    assert out.metadata.get("inject_subagent_grounding__applied") is True


@pytest.mark.asyncio
async def test_bare_subagent_session_key_re_injects_when_missing() -> None:
    ctx = _ctx("subagent:abcd1234", "Be helpful.")
    out = await inject_subagent_grounding(ctx)
    assert _SUBAGENT_GROUNDING in out.system_prompt
    assert "Be helpful." in out.system_prompt
    assert out.metadata.get("inject_subagent_grounding__applied") is True


def test_fallback_grounding_matches_spawn_prompt() -> None:
    assert _SUBAGENT_GROUNDING == _SUBAGENT_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_subagent_session_idempotent_when_present() -> None:
    existing = f"{_SUBAGENT_GROUNDING}\n\nDo the task."
    ctx = _ctx("agent:main:subagent:abcd1234", existing)
    out = await inject_subagent_grounding(ctx)
    assert out.system_prompt == existing
    assert out.metadata.get("inject_subagent_grounding__applied") is False


@pytest.mark.asyncio
async def test_tuple_system_prompt_preserves_cacheable_prefix() -> None:
    ctx = _ctx("agent:main:subagent:abcd1234", ("STATIC PREFIX", "Dynamic part."))
    out = await inject_subagent_grounding(ctx)
    cacheable, dynamic = out.system_prompt
    # Cacheable prefix is untouched so prompt-cache hit is preserved.
    assert cacheable == "STATIC PREFIX"
    assert _SUBAGENT_GROUNDING in dynamic
    assert "Dynamic part." in dynamic


@pytest.mark.asyncio
async def test_empty_system_prompt_gets_grounding() -> None:
    ctx = _ctx("agent:main:subagent:abcd1234", "")
    out = await inject_subagent_grounding(ctx)
    assert out.system_prompt == _SUBAGENT_GROUNDING
