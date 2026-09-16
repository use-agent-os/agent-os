"""Regression tests for #2521: transcript reads must window from the newest
end, not the oldest.

``SessionStorage.get_transcript``'s ``limit`` (with the default ``offset=0``
every real caller uses) is ``ORDER BY created_at ASC LIMIT n`` -- the oldest
n rows of the whole transcript. ``transcript_entries`` rows are only deleted
when a session is deleted, so any session whose transcript outgrows the
limit is affected. This is the same shape of bug #1805/#1873 already fixed
for ``agent_tasks``/``list_agent_tasks``; ``SessionStorage`` already had the
correct newest-first window one method away, in ``get_recent_transcript``,
which #1873 didn't touch since it fixes a different table.

Two real callers acted on the stale, oldest-first slice: ``sessions_history``
(default limit 20) and ``_read_child_result`` (limit 50), the function that
reports a spawned subagent's final answer back to its parent.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage

KEY = "agent:main:webchat:long-lived"
TOTAL = 80
FINAL_ANSWER = "the real final answer"


@pytest_asyncio.fixture
async def storage():
    store = SessionStorage(":memory:")
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def manager(storage):
    return SessionManager(storage, inject_time_prefix=False)


async def _long_lived_session(manager: SessionManager) -> None:
    await manager.create(KEY, agent_id="main")
    for i in range(TOTAL):
        role = "user" if i % 2 == 0 else "assistant"
        await manager.append_message(KEY, role, f"msg-{i:03d}")
    await manager.append_message(KEY, "assistant", FINAL_ANSWER)


# --------------------------------------------------------------------------
# storage / manager layer
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_recent_transcript_windows_from_the_newest_end(manager) -> None:
    await _long_lived_session(manager)

    rows = await manager.get_recent_transcript(KEY, 20)

    assert len(rows) == 20
    assert rows[-1].content == FINAL_ANSWER
    # Still oldest-first within the window.
    assert rows[0].content == f"msg-{TOTAL - 19:03d}"


@pytest.mark.asyncio
async def test_get_transcript_default_window_is_unchanged() -> None:
    """The reviewer's constraint from #1873: other callers of get_transcript
    (full-history replay, compaction, export) keep the oldest-first default
    -- this fix must not touch that method at all."""
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    try:
        await _long_lived_session(manager)

        rows = await manager.get_transcript(KEY, limit=20)

        assert len(rows) == 20
        assert rows[0].content == "msg-000"
        assert rows[-1].content == "msg-019"
    finally:
        await storage.close()


# --------------------------------------------------------------------------
# caller 1: sessions_history
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_history_returns_the_recent_tail(manager, monkeypatch) -> None:
    from agentos.tools.builtin import sessions as sessions_tools

    await _long_lived_session(manager)
    monkeypatch.setattr(sessions_tools, "_session_manager", manager)

    payload = json.loads(await sessions_tools.sessions_history(session_key=KEY, limit=20))

    contents = [m["content"] for m in payload["messages"]]
    assert contents[-1] == FINAL_ANSWER
    assert len(contents) == 20


# --------------------------------------------------------------------------
# caller 2: subagent result reporting
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_child_result_finds_the_real_final_answer(manager) -> None:
    from agentos.gateway.subagent_announce import _read_child_result

    await _long_lived_session(manager)

    result = await _read_child_result(KEY, session_manager=manager)

    assert result["text"] == FINAL_ANSWER
    assert result["source_role"] == "assistant"


@pytest.mark.asyncio
async def test_read_child_result_falls_back_for_a_manager_without_read_recent() -> None:
    """Guard: a session_manager stand-in that only implements read_transcript
    (an older test double, or a lightweight caller) must not crash -- it
    keeps working via the old path, just without the fix's benefit."""
    from agentos.gateway.subagent_announce import _read_child_result

    class _LegacyManager:
        async def read_transcript(self, session_key: str, limit: int = 50):
            return [{"role": "assistant", "content": "only message"}]

    result = await _read_child_result(KEY, session_manager=_LegacyManager())

    assert result["text"] == "only message"
