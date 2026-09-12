from __future__ import annotations

import json

import pytest
import structlog.testing

from agentos.tools.builtin import agents as agents_tool
from agentos.tools.builtin import sessions as sessions_tool
from agentos.tools.types import CallerKind, ToolContext, ToolError, current_tool_context


class _StubSessionManager:
    def __init__(self) -> None:
        self.killed: list[str] = []
        self.injected: list[tuple[str, str]] = []
        self.get_session_calls: list[str] = []

    async def get_current_session(self):
        return None

    async def list_sessions(self):
        return [
            {"session_key": "sub-legacy", "spawned_by": None, "status": "running"},
            {"session_key": "sub-owned", "spawned_by": "agent:main:parent", "status": "running"},
        ]

    async def get_session(self, session_key: str):
        self.get_session_calls.append(session_key)
        return {"session_key": session_key, "spawned_by": None, "status": "running"}

    async def kill_session(self, session_key: str) -> None:
        self.killed.append(session_key)

    async def inject_message(self, session_key: str, message: str, provenance: str) -> None:
        self.injected.append((session_key, message))


def _ctx(session_key: str | None) -> ToolContext:
    return ToolContext(
        caller_kind=CallerKind.AGENT,
        session_key=session_key,
        agent_id="main",
    )


@pytest.fixture
def stub_manager():
    mgr = _StubSessionManager()
    sessions_tool.set_session_manager(mgr)
    sessions_tool.set_task_runtime(None)
    yield mgr
    sessions_tool.set_session_manager(None)
    sessions_tool.set_task_runtime(None)


@pytest.mark.asyncio
async def test_subagents_list_without_session_context_returns_empty_with_warning(
    stub_manager: _StubSessionManager,
) -> None:
    token = current_tool_context.set(_ctx(None))
    try:
        with structlog.testing.capture_logs() as captured:
            payload = json.loads(await agents_tool.subagents("list"))
    finally:
        current_tool_context.reset(token)

    assert payload == {"action": "list", "subagents": []}
    assert any(event["event"] == "subagents.list_no_session_context" for event in captured)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["kill", "steer"])
async def test_subagents_mutating_action_without_session_context_raises(
    action: str,
    stub_manager: _StubSessionManager,
) -> None:
    token = current_tool_context.set(_ctx(None))
    kwargs = {"message": "new task"} if action == "steer" else {}
    try:
        with pytest.raises(ToolError, match="session context required"):
            await agents_tool.subagents(action, session_key="sub-legacy", **kwargs)
    finally:
        current_tool_context.reset(token)

    assert stub_manager.killed == []
    assert stub_manager.injected == []
    assert stub_manager.get_session_calls == []


@pytest.mark.asyncio
async def test_subagents_list_filters_by_current_session(
    stub_manager: _StubSessionManager,
) -> None:
    token = current_tool_context.set(_ctx("agent:main:parent"))
    try:
        payload = json.loads(await agents_tool.subagents("list"))
    finally:
        current_tool_context.reset(token)

    assert payload["action"] == "list"
    assert len(payload["subagents"]) == 1
    assert payload["subagents"][0]["session_key"] == "sub-owned"


@pytest.mark.asyncio
async def test_subagents_list_retrieves_beyond_100_sessions(tmp_path) -> None:
    from agentos.session.manager import SessionManager
    from agentos.session.storage import SessionStorage

    db_path = tmp_path / "test_subagents_100.db"
    storage = SessionStorage(str(db_path))
    await storage.connect()
    manager = SessionManager(storage=storage)
    sessions_tool.set_session_manager(manager)

    try:
        # Create parent session
        parent = await manager.create("agent:main:parent", agent_id="main")

        # Create child subagent session spawned by parent
        child = await manager.create(
            "agent:main:child_sub", agent_id="main", spawned_by=parent.session_key
        )

        # Create 105 newer unrelated sessions that push child out of the top 100
        for i in range(105):
            await manager.create(f"agent:main:other_{i:03d}", agent_id="main")

        token = current_tool_context.set(_ctx("agent:main:parent"))
        try:
            payload = json.loads(await agents_tool.subagents("list"))
        finally:
            current_tool_context.reset(token)

        assert payload["action"] == "list"
        found_keys = [s["session_key"] for s in payload["subagents"]]
        assert child.session_key in found_keys
    finally:
        sessions_tool.set_session_manager(None)
        await storage.close()
