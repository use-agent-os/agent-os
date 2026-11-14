"""exit_plan_mode tool: dispatch behavior, end-turn contract, and gating."""

from __future__ import annotations

import json

import pytest

from agentos.plan_mode import PLAN_MODE_TOOL_ALLOW, get_plan_mode_store, reset_plan_mode_store
from agentos.tool_boundary import ToolCall
from agentos.tools import get_default_registry
from agentos.tools.dispatch import build_tool_handler
from agentos.tools.types import CallerKind, InteractionMode, ToolContext


@pytest.fixture(autouse=True)
def _fresh_store() -> None:
    reset_plan_mode_store()
    yield
    reset_plan_mode_store()


# Plan mode narrows the turn to this allowlist (gateway/routing.py); that is
# what offers the otherwise hidden exit_plan_mode tool.
_PLAN_SURFACE = set(PLAN_MODE_TOOL_ALLOW)


def _call(plan: str = "## Plan\n1. Do X") -> ToolCall:
    return ToolCall(
        tool_use_id="plan-1",
        tool_name="exit_plan_mode",
        arguments={"plan": plan},
    )


@pytest.mark.asyncio
async def test_exit_plan_mode_presents_and_terminates_turn() -> None:
    key = "agent:main:t-plan"
    get_plan_mode_store().enable(key)
    ctx = ToolContext(caller_kind=CallerKind.AGENT, session_key=key, allowed_tools=_PLAN_SURFACE)
    handler = build_tool_handler(get_default_registry(), ctx)

    result = await handler(_call())

    assert result.is_error is False
    # Same end-turn-and-resume contract as ask_user: presenting ends the turn.
    assert result.terminates_turn is True
    payload = json.loads(result.content)
    assert payload["status"] == "plan_presented"
    assert payload["plan"] == "## Plan\n1. Do X"


@pytest.mark.asyncio
async def test_exit_plan_mode_refused_outside_plan_mode() -> None:
    ctx = ToolContext(
        caller_kind=CallerKind.AGENT,
        session_key="agent:main:t-noplan",
        allowed_tools=_PLAN_SURFACE,
    )
    handler = build_tool_handler(get_default_registry(), ctx)

    result = await handler(_call())

    assert result.is_error is True
    assert result.terminates_turn is False
    assert "not active" in result.content


@pytest.mark.asyncio
async def test_exit_plan_mode_is_not_dispatchable_on_the_default_surface() -> None:
    """Hidden by default: without plan mode's surface the call never reaches it."""
    ctx = ToolContext(caller_kind=CallerKind.AGENT, session_key="agent:main:t-noplan")
    handler = build_tool_handler(get_default_registry(), ctx)

    result = await handler(_call())

    assert result.is_error is True
    assert json.loads(result.content)["error_class"] == "PolicyDenied"


@pytest.mark.asyncio
async def test_exit_plan_mode_rejects_an_empty_plan() -> None:
    key = "agent:main:t-plan-empty"
    get_plan_mode_store().enable(key)
    ctx = ToolContext(caller_kind=CallerKind.AGENT, session_key=key, allowed_tools=_PLAN_SURFACE)
    handler = build_tool_handler(get_default_registry(), ctx)

    result = await handler(_call(plan="   "))

    assert result.is_error is True
    assert result.terminates_turn is False
    assert "non-empty" in result.content


@pytest.mark.asyncio
async def test_exit_plan_mode_refuses_unattended_non_channel_surfaces() -> None:
    key = "agent:main:t-plan-unattended"
    get_plan_mode_store().enable(key)
    ctx = ToolContext(
        caller_kind=CallerKind.CLI,
        interaction_mode=InteractionMode.UNATTENDED,
        session_key=key,
        allowed_tools=_PLAN_SURFACE,
    )
    handler = build_tool_handler(get_default_registry(), ctx)

    result = await handler(_call())

    assert result.is_error is True
    assert "unattended" in result.content


@pytest.mark.asyncio
async def test_exit_plan_mode_allows_channel_turns_despite_unattended_mark() -> None:
    # Channel turns carry UNATTENDED (no approval operator) but a human
    # responder is on the other end — the ask_user exception applies here too.
    key = "telegram:dm:u1"
    get_plan_mode_store().enable(key)
    ctx = ToolContext(
        caller_kind=CallerKind.CHANNEL,
        interaction_mode=InteractionMode.UNATTENDED,
        session_key=key,
        allowed_tools=_PLAN_SURFACE,
    )
    handler = build_tool_handler(get_default_registry(), ctx)

    result = await handler(_call())

    assert result.is_error is False
    assert result.terminates_turn is True
