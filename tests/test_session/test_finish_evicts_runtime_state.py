"""SessionManager.finish drops module-level subagent + routing bookkeeping."""

from __future__ import annotations

import pytest

from agentos.engine.steps.agentos_router import _history_store
from agentos.gateway.subagent_announce import _tracker
from agentos.session.manager import SessionManager
from agentos.session.models import SessionStatus


class _MemoryStorage:
    def __init__(self) -> None:
        self._sessions: dict[str, object] = {}

    async def get_session(self, session_key: str):
        return self._sessions.get(session_key)

    async def upsert_session(self, node) -> None:
        self._sessions[node.session_key] = node


@pytest.mark.asyncio
async def test_finish_evicts_spawn_group_tracker_and_routing_history() -> None:
    from agentos.session.models import SessionNode

    storage = _MemoryStorage()
    node = SessionNode(
        session_key="agent:main:main",
        session_id="abc",
        agent_id="main",
        created_at=1,
        updated_at=1,
        started_at=1,
        status=SessionStatus.RUNNING,
    )
    await storage.upsert_session(node)

    _tracker.mark_closed("agent:main:main", "task-X")
    _history_store.set("agent:main:main", [{"turn_index": 0}])
    assert _tracker.is_closed("agent:main:main", "task-X")
    assert _history_store.get("agent:main:main") is not None

    mgr = SessionManager(storage)  # type: ignore[arg-type]
    await mgr.finish("agent:main:main", status=SessionStatus.DONE)

    assert not _tracker.is_closed("agent:main:main", "task-X")
    assert _history_store.get("agent:main:main") is None
