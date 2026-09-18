"""A session's terminal event clears its warm marker.

``warm_session`` is "Trigger 1: sync on first session access", and it remembers
which sessions it has already warmed so the sync runs once per session. Nothing
ever removed a key from that record, so membership in it decided whether the
*next* session under the same name was warmed -- and session keys are reused as
a matter of course, ``agent:main:main`` being the default one. A brand-new
session under a reused key silently skipped its session-start sync, and the set
grew one entry per session key the process had ever warmed.

``_warmed_sessions`` is now the shared ``BoundedRegistry`` keyed by session, so
``drop_session_state()`` -- the single terminal-event hook -- reaches it with no
new wiring, and the size ceiling backs up any session that never reaches it.
"""

from __future__ import annotations

import pytest

from agentos.memory.sync_manager import MemorySyncManager
from agentos.session.runtime_state import evict_session_runtime_state
from agentos.util.bounded_registry import BoundedRegistry

KEY = "agent:main:main"


class _NoopStore:
    async def index_file(self, **kwargs: object) -> int:
        return 0

    async def remove_file(self, path: str) -> None:
        return None


@pytest.fixture
def manager(tmp_path) -> tuple[MemorySyncManager, list[str]]:
    mgr = MemorySyncManager(_NoopStore(), tmp_path, tmp_path)  # type: ignore[arg-type]
    reasons: list[str] = []

    async def _record(reason: str = "") -> None:
        reasons.append(reason)

    mgr.sync = _record  # type: ignore[method-assign]
    return mgr, reasons


@pytest.mark.asyncio
async def test_a_reused_session_key_is_warmed_again(manager) -> None:
    """The bug: the second session under the key got no session-start sync."""
    mgr, reasons = manager

    await mgr.warm_session(KEY)
    evict_session_runtime_state(KEY)  # the first session ends
    await mgr.warm_session(KEY)  # a NEW session reusing the key

    assert reasons == ["session-start", "session-start"]


@pytest.mark.asyncio
async def test_a_live_session_is_warmed_only_once(manager) -> None:
    """Guard: the record exists to stop re-syncing on every access."""
    mgr, reasons = manager

    await mgr.warm_session(KEY)
    await mgr.warm_session(KEY)
    await mgr.warm_session(KEY)

    assert reasons == ["session-start"]


@pytest.mark.asyncio
async def test_first_access_still_warms(manager) -> None:
    """Guard: Trigger 1 itself."""
    mgr, reasons = manager

    await mgr.warm_session(KEY)

    assert reasons == ["session-start"]
    assert KEY in mgr._warmed_sessions


@pytest.mark.asyncio
async def test_evicting_one_session_does_not_rewarm_another(manager) -> None:
    """Guard: eviction is keyed, not a clear()."""
    mgr, reasons = manager
    await mgr.warm_session(KEY)
    await mgr.warm_session("agent:main:other")
    assert reasons == ["session-start", "session-start"]

    evict_session_runtime_state(KEY)
    await mgr.warm_session("agent:main:other")

    assert reasons == ["session-start", "session-start"]
    assert "agent:main:other" in mgr._warmed_sessions
    assert KEY not in mgr._warmed_sessions


@pytest.mark.asyncio
async def test_the_record_does_not_grow_one_entry_per_session(manager) -> None:
    """The leak: 300 sessions, each warmed and then ended."""
    mgr, _ = manager

    for i in range(300):
        key = f"agent:main:s{i}"
        await mgr.warm_session(key)
        evict_session_runtime_state(key)

    assert len(mgr._warmed_sessions) == 0


@pytest.mark.asyncio
async def test_the_ceiling_backs_up_a_session_that_never_ends(manager) -> None:
    """A session that never emits a terminal event must still be bounded."""
    mgr, _ = manager
    ceiling = mgr._warmed_sessions.max_entries

    for i in range(ceiling + 200):
        await mgr.warm_session(f"agent:main:never-ends-{i}")

    assert len(mgr._warmed_sessions) <= ceiling


@pytest.mark.asyncio
async def test_evicting_a_session_that_was_never_warmed_is_fine(manager) -> None:
    """Guard: the hook runs on every session, warmed or not."""
    mgr, reasons = manager

    evict_session_runtime_state("agent:main:never-warmed")
    await mgr.warm_session(KEY)

    assert reasons == ["session-start"]


def test_the_record_is_the_shared_bounded_primitive(tmp_path) -> None:
    """Guard on the #1131 invariant that makes the terminal hook reach it."""
    mgr = MemorySyncManager(_NoopStore(), tmp_path, tmp_path)  # type: ignore[arg-type]

    assert isinstance(mgr._warmed_sessions, BoundedRegistry)
