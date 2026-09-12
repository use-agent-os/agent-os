"""Test that subagents(action='list') delegates spawned_by filter to SQL (#1799)."""

from __future__ import annotations

import json

import pytest

from agentos.tools.builtin import agents as agents_tool
from agentos.tools.builtin import sessions as sessions_tool
from agentos.tools.types import CallerKind, ToolContext, current_tool_context


class _StubManager:
    """Session manager with >100 sessions to demonstrate truncation fix."""

    def __init__(self, n_extra: int = 120) -> None:
        self._sessions: list[dict] = []
        # Create n_extra filler sessions (no spawned_by)
        for i in range(n_extra):
            self._sessions.append(
                {
                    "session_key": f"agent:main:filler:{i:04d}",
                    "spawned_by": None,
                    "status": "completed",
                }
            )
        # The subagent we care about — placed AFTER the filler
        self._sessions.append(
            {
                "session_key": "agent:main:child_session",
                "spawned_by": "agent:main:parent_session",
                "status": "running",
            }
        )

    async def get_current_session(self):
        return None

    async def list_sessions(self, **kwargs):
        sb = kwargs.get("spawned_by")
        if sb is not None:
            return [s for s in self._sessions if s.get("spawned_by") == sb]
        # Simulate the old default limit=100 truncation
        limit = kwargs.get("limit", 100)
        return self._sessions[:limit]

    async def get_session(self, session_key: str):
        for s in self._sessions:
            if s["session_key"] == session_key:
                return s
        return None

    async def kill_session(self, session_key: str) -> None:
        pass

    async def inject_message(self, session_key: str, message: str, provenance: str) -> None:
        pass


def _ctx(session_key: str) -> ToolContext:
    return ToolContext(
        caller_kind=CallerKind.AGENT,
        session_key=session_key,
        agent_id="main",
    )


@pytest.fixture
def stub_manager():
    mgr = _StubManager(n_extra=120)
    sessions_tool.set_session_manager(mgr)
    sessions_tool.set_task_runtime(None)
    yield mgr
    sessions_tool.set_session_manager(None)
    sessions_tool.set_task_runtime(None)


@pytest.mark.asyncio
async def test_subagents_list_finds_child_beyond_100_sessions(
    stub_manager: _StubManager,
) -> None:
    """subagents(action='list') must find spawned subagents even when
    >100 total sessions exist.

    Before the fix, list_sessions() was called without spawned_by, defaulting
    to limit=100 and filtering in Python — silently dropping the child.

    Regression test for #1799.
    """
    token = current_tool_context.set(_ctx("agent:main:parent_session"))
    try:
        payload = json.loads(await agents_tool.subagents("list"))
    finally:
        current_tool_context.reset(token)

    assert payload["action"] == "list"
    keys = [s["session_key"] for s in payload["subagents"]]
    assert "agent:main:child_session" in keys


@pytest.mark.asyncio
async def test_subagents_list_empty_when_no_children(
    stub_manager: _StubManager,
) -> None:
    """A parent with no spawned children returns an empty list."""
    token = current_tool_context.set(_ctx("agent:main:no_children_here"))
    try:
        payload = json.loads(await agents_tool.subagents("list"))
    finally:
        current_tool_context.reset(token)

    assert payload == {"action": "list", "subagents": []}
