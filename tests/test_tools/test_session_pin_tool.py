"""Tests for the ``session_pin`` tool (prompt-driven pin / unpin).

Run against the real ``SessionManager`` + ``SessionStorage``: a fake could
grow a method production lacks, and the assertion that matters is the stored
column the session lists read, not the tool's own JSON.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage
from agentos.tools.builtin import sessions as sessions_tool
from agentos.tools.types import ToolContext, ToolError, current_tool_context

MINE = "agent:main:webchat:mine"
OTHER = "agent:main:webchat:other"


@pytest_asyncio.fixture
async def manager():
    storage = SessionStorage(":memory:")
    await storage.connect()
    mgr = SessionManager(storage, inject_time_prefix=False)
    await mgr.create(MINE)
    await mgr.create(OTHER)
    original = sessions_tool._session_manager
    sessions_tool.set_session_manager(mgr)
    token = current_tool_context.set(ToolContext(session_key=MINE))
    yield mgr
    current_tool_context.reset(token)
    sessions_tool.set_session_manager(original)
    await storage.close()


async def _pin(**kwargs) -> dict:
    return json.loads(await sessions_tool.session_pin(**kwargs))


async def _pinned(mgr: SessionManager, key: str) -> bool:
    node = await mgr.get_session(key)
    assert node is not None
    return node.pinned_at is not None


@pytest.mark.asyncio
async def test_pins_the_calling_session_by_default(manager):
    data = await _pin()

    assert data == {
        "session_key": MINE,
        "name": data["name"],
        "pinned": True,
        "changed": True,
    }
    assert await _pinned(manager, MINE) is True
    assert await _pinned(manager, OTHER) is False


@pytest.mark.asyncio
async def test_unpins_the_calling_session(manager):
    await _pin()

    data = await _pin(pinned=False)

    assert data["pinned"] is False
    assert data["changed"] is True
    assert await _pinned(manager, MINE) is False


@pytest.mark.asyncio
async def test_repeating_a_pin_reports_no_change(manager):
    await _pin()

    assert (await _pin())["changed"] is False


@pytest.mark.asyncio
async def test_pins_another_session_by_name_case_insensitively(manager):
    await manager.update(OTHER, display_name="Draft AgentOS Quote Post")

    data = await _pin(session="draft agentos quote post")

    assert data["session_key"] == OTHER
    assert data["name"] == "Draft AgentOS Quote Post"
    assert await _pinned(manager, OTHER) is True
    assert await _pinned(manager, MINE) is False


@pytest.mark.asyncio
async def test_pins_another_session_by_key_and_by_unique_prefix(manager):
    await _pin(session=OTHER)
    assert await _pinned(manager, OTHER) is True

    await manager.update(MINE, display_name="Portfolio review")
    await _pin(session="Portfolio")
    assert await _pinned(manager, MINE) is True


@pytest.mark.asyncio
async def test_an_exact_name_wins_over_prefix_matches(manager):
    await manager.update(MINE, display_name="acp")
    await manager.update(OTHER, display_name="acp:codex")

    data = await _pin(session="acp")

    assert data["session_key"] == MINE


@pytest.mark.asyncio
async def test_an_ambiguous_target_pins_nothing(manager):
    await manager.update(MINE, display_name="acp:codex one")
    await manager.update(OTHER, display_name="acp:codex two")

    with pytest.raises(ToolError, match="more than one session"):
        await _pin(session="acp:codex")

    assert await _pinned(manager, MINE) is False
    assert await _pinned(manager, OTHER) is False


@pytest.mark.asyncio
async def test_an_unknown_target_is_an_error(manager):
    with pytest.raises(ToolError, match="No session matches"):
        await _pin(session="no such session")


@pytest.mark.asyncio
async def test_no_active_session_is_an_error(manager):
    token = current_tool_context.set(ToolContext(session_key=None))
    try:
        with pytest.raises(ToolError, match="No active session"):
            await _pin()
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_a_non_boolean_pinned_is_refused(manager):
    with pytest.raises(ToolError, match="true or false"):
        await _pin(pinned="yes")  # type: ignore[arg-type]
    assert await _pinned(manager, MINE) is False


def test_the_tool_is_registered_deferred_and_on_the_chat_layer():
    from agentos.tools.deferred import DEFERRED_BUILTIN_TOOLS  # noqa: PLC0415
    from agentos.tools.policy_config import _TOOL_GROUPS  # noqa: PLC0415

    assert "session_pin" in DEFERRED_BUILTIN_TOOLS
    assert "session_pin" in _TOOL_GROUPS["channel:chat"]
