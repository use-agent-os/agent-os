"""Regression tests for session_status against the real SessionManager surface.

session_status used to call ``mgr.get_current_session()``, a method that exists
only on test fakes — the production SessionManager
(``agentos.session.manager.SessionManager``) has never defined it. In a live
gateway the attribute access raised AttributeError, which session_status
converted into a hard ToolError, so the tool failed 100% of the time.

The fakes here deliberately expose ONLY the production method surface
(``get_session``), so a reintroduction of the ``get_current_session()`` call
fails these tests instead of passing against a fake that flatters it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from agentos.tools.builtin import sessions as sessions_tool
from agentos.tools.types import ToolContext, ToolError, current_tool_context


@dataclass
class _StubSession:
    session_key: str = "agent:main:webchat:abc123"
    session_id: str = "sess-1"
    status: str = "running"
    model: str = "claude-opus-4-8"
    model_provider: str = "anthropic"
    input_tokens: int = 120
    output_tokens: int = 30
    estimated_cost_usd: float = 0.42
    cache_read: int = 7
    cache_write: int = 3
    compaction_count: int = 1
    context_tokens: int = 900
    spawn_depth: int = 0
    started_at: int = 1_700_000_000_000
    runtime_ms: int = 5_000


class _ProductionSurfaceManager:
    """Mirrors the real SessionManager: get_session, no get_current_session."""

    def __init__(self, sessions: dict[str, _StubSession]) -> None:
        self._sessions = sessions
        self.requested_keys: list[str] = []

    async def get_session(self, session_key: str) -> _StubSession | None:
        self.requested_keys.append(session_key)
        return self._sessions.get(session_key)


@pytest.fixture
def _restore_manager():
    original = sessions_tool._session_manager
    yield
    sessions_tool.set_session_manager(original)


def _set_ctx(session_key: str | None) -> object:
    return current_tool_context.set(ToolContext(session_key=session_key))


@pytest.mark.asyncio
async def test_session_status_works_without_get_current_session(_restore_manager):
    """The bug: this raised ToolError because the real mgr lacks the method."""
    session = _StubSession()
    mgr = _ProductionSurfaceManager({session.session_key: session})
    sessions_tool.set_session_manager(mgr)
    token = _set_ctx(session.session_key)
    try:
        raw = await sessions_tool.session_status()
    finally:
        current_tool_context.reset(token)

    data = json.loads(raw)
    assert data["session_key"] == session.session_key
    assert data["session_id"] == "sess-1"
    assert data["model"] == "claude-opus-4-8"
    assert data["model_provider"] == "anthropic"
    assert data["input_tokens"] == 120
    assert data["output_tokens"] == 30
    assert data["total_tokens"] == 150
    assert data["estimated_cost_usd"] == 0.42
    assert data["compaction_count"] == 1
    assert data["spawn_depth"] == 0
    # Resolution must go through the ContextVar session_key, not a global.
    assert mgr.requested_keys == [session.session_key]


@pytest.mark.asyncio
async def test_session_status_resolves_the_calling_sessions_key(_restore_manager):
    """With several live sessions, it must report the caller's, not an arbitrary one."""
    mine = _StubSession(session_key="agent:main:webchat:mine", session_id="sess-mine")
    other = _StubSession(session_key="agent:main:webchat:other", session_id="sess-other")
    mgr = _ProductionSurfaceManager({mine.session_key: mine, other.session_key: other})
    sessions_tool.set_session_manager(mgr)
    token = _set_ctx(mine.session_key)
    try:
        data = json.loads(await sessions_tool.session_status())
    finally:
        current_tool_context.reset(token)

    assert data["session_id"] == "sess-mine"


@pytest.mark.asyncio
async def test_session_status_reports_no_active_session_without_ctx(_restore_manager):
    sessions_tool.set_session_manager(_ProductionSurfaceManager({}))
    token = _set_ctx(None)
    try:
        with pytest.raises(ToolError, match="No active session"):
            await sessions_tool.session_status()
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_session_status_reports_no_active_session_when_key_is_unknown(
    _restore_manager,
):
    sessions_tool.set_session_manager(_ProductionSurfaceManager({}))
    token = _set_ctx("agent:main:webchat:vanished")
    try:
        with pytest.raises(ToolError, match="No active session"):
            await sessions_tool.session_status()
    finally:
        current_tool_context.reset(token)
