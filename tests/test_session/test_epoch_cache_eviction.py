"""A session's terminal event drops its entry from the epoch cache.

``SessionManager._epoch_cache`` is an in-process map from session key to epoch,
read by the event-emit path so it does not need a DB round-trip per event.
Nothing ever removed a key from it: ``evict_session_runtime_state()`` reaches
process-global stores, and this one belongs to the manager instance.

Two things followed. The epoch is a staleness marker, so a key that outlived its
row handed its epoch to the next session created under the same name -- and
``agent:main:main`` is the default key, so reuse is the normal case. And the map
grew one entry per session key the process had ever emitted for, which is the
shape ``delete()``'s own docstring says the eviction ordering prevents.
"""

from __future__ import annotations

import pytest

from agentos.engine.steps.agentos_router import _history_store
from agentos.gateway.session_services import get_session_epoch
from agentos.gateway.subagent_announce import _tracker
from agentos.session.manager import SessionManager
from agentos.session.models import SessionNode, SessionStatus

KEY = "agent:main:main"


class _MemoryStorage:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionNode] = {}

    async def get_session(self, session_key: str) -> SessionNode | None:
        return self._sessions.get(session_key)

    async def upsert_session(self, node: SessionNode) -> None:
        self._sessions[node.session_key] = node

    async def delete_session(self, session_key: str) -> None:
        self._sessions.pop(session_key, None)


def _node(session_key: str, *, epoch: int = 0) -> SessionNode:
    return SessionNode(
        session_key=session_key,
        session_id=f"id-{session_key}",
        agent_id="main",
        created_at=1,
        updated_at=1,
        started_at=1,
        status=SessionStatus.RUNNING,
        epoch=epoch,
    )


@pytest.fixture
def manager() -> tuple[SessionManager, _MemoryStorage]:
    storage = _MemoryStorage()
    return SessionManager(storage), storage  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_recreated_session_key_does_not_inherit_the_old_epoch(manager) -> None:
    """The reason this is more than a leak.

    The emit path stamps payloads with whatever the cache holds, so a stale
    entry labels a brand-new session with the dead one's epoch.
    """
    mgr, storage = manager
    await storage.upsert_session(_node(KEY, epoch=7))
    mgr.set_cached_epoch(KEY, 7)

    await mgr.delete(KEY)
    await storage.upsert_session(_node(KEY, epoch=0))  # same key, fresh session

    assert get_session_epoch(mgr, KEY) is None, "the dead session's epoch must not survive"
    assert mgr.get_cached_epoch(KEY) is None


@pytest.mark.asyncio
async def test_delete_drops_the_cached_epoch(manager) -> None:
    mgr, storage = manager
    await storage.upsert_session(_node(KEY))
    mgr.set_cached_epoch(KEY, 3)

    await mgr.delete(KEY)

    assert mgr._epoch_cache == {}


@pytest.mark.asyncio
async def test_finish_drops_the_cached_epoch(manager) -> None:
    mgr, storage = manager
    await storage.upsert_session(_node(KEY))
    mgr.set_cached_epoch(KEY, 3)

    await mgr.finish(KEY, status=SessionStatus.DONE)

    assert mgr._epoch_cache == {}


@pytest.mark.asyncio
async def test_kill_session_drops_the_cached_epoch(manager) -> None:
    """Kill routes through ``finish``; pinned so a future split keeps it."""
    mgr, storage = manager
    await storage.upsert_session(_node(KEY))
    mgr.set_cached_epoch(KEY, 3)

    await mgr.kill_session(KEY)

    assert mgr._epoch_cache == {}


@pytest.mark.asyncio
async def test_the_cache_does_not_outlive_the_rows_it_describes(manager) -> None:
    """The leak: one entry per session key the process ever emitted for."""
    mgr, storage = manager
    keys = [f"agent:main:s{i}" for i in range(300)]
    for key in keys:
        await storage.upsert_session(_node(key))
        mgr.set_cached_epoch(key, 7)
    assert len(mgr._epoch_cache) == 300

    for key in keys:
        await mgr.delete(key)

    assert mgr._epoch_cache == {}
    assert storage._sessions == {}


@pytest.mark.asyncio
async def test_only_the_ended_session_is_evicted(manager) -> None:
    """Guard: eviction is keyed, not a clear()."""
    mgr, storage = manager
    await storage.upsert_session(_node(KEY))
    await storage.upsert_session(_node("agent:main:other"))
    mgr.set_cached_epoch(KEY, 3)
    mgr.set_cached_epoch("agent:main:other", 9)

    await mgr.delete(KEY)

    assert mgr.get_cached_epoch("agent:main:other") == 9


@pytest.mark.asyncio
async def test_evicting_a_session_with_no_cached_epoch_is_fine(manager) -> None:
    """Guard: the two overlapping callers must stay idempotent."""
    mgr, storage = manager
    await storage.upsert_session(_node(KEY))

    await mgr.finish(KEY, status=SessionStatus.DONE)
    await mgr.delete(KEY)

    assert mgr._epoch_cache == {}


@pytest.mark.asyncio
async def test_the_rest_of_the_runtime_state_is_still_evicted(manager) -> None:
    """Guard: the epoch cache is dropped *alongside* the existing stores."""
    mgr, storage = manager
    await storage.upsert_session(_node(KEY))
    mgr.set_cached_epoch(KEY, 3)
    _tracker.mark_closed(KEY, "task-X")
    _history_store.set(KEY, [{"turn_index": 0}])

    await mgr.delete(KEY)

    assert not _tracker.is_closed(KEY, "task-X")
    assert _history_store.get(KEY) is None
    assert mgr._epoch_cache == {}


@pytest.mark.asyncio
async def test_a_live_session_keeps_its_cached_epoch(manager) -> None:
    """Guard: nothing is evicted while the session is still running."""
    mgr, storage = manager
    await storage.upsert_session(_node(KEY))
    mgr.set_cached_epoch(KEY, 4)

    assert mgr.get_cached_epoch(KEY) == 4
    assert get_session_epoch(mgr, KEY) == 4
