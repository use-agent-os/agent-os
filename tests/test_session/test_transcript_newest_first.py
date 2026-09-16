"""Regression tests: `get_transcript` must be able to window from the newest end.

`transcript_entries` rows are only deleted when a session is deleted, so a
long-lived session eventually holds more entries than a caller's `limit`.
`SessionStorage.get_transcript` took that window from the *oldest* end
regardless -- the same defect `list_agent_tasks` had before it grew a
`newest_first` option (#1805/#1873), just in the transcript table instead of
`agent_tasks`. Two live callers acted on the stale, oldest-first slice:

- `sessions_history` (`tools/builtin/sessions.py`), described to the model as
  "Retrieve conversation history from a session's transcript", returned the
  first `limit` messages ever written instead of the most recent ones.
- `_read_child_result` (`gateway/subagent_announce.py`), which reports a
  spawned subagent's final answer back to its parent, fetched "the last 50
  entries" and scanned backward for the latest assistant message -- but the
  50 entries were actually the *first* 50, so any subagent transcript longer
  than that lost its real answer.
"""

from __future__ import annotations

import json

import pytest

from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage

KEY = "agent:main:webchat:long-lived"


async def _manager_with_messages(count: int) -> SessionManager:
    storage = SessionStorage(":memory:")
    await storage.connect()
    mgr = SessionManager(storage, inject_time_prefix=False)
    await mgr.create(KEY)
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        await mgr.append_message(KEY, role, f"msg-{index:03d}")
    return mgr


# --------------------------------------------------------------------------
# storage layer
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_newest_first_window_keeps_the_latest_entries() -> None:
    mgr = await _manager_with_messages(105)
    try:
        entries = await mgr._storage.get_transcript(
            (await mgr.get_session(KEY)).session_id, limit=50, newest_first=True
        )

        assert len(entries) == 50
        # Still oldest-first within the window, so `entries[-1]` is the latest.
        assert [e.content for e in entries] == sorted(e.content for e in entries)
        assert entries[-1].content == "msg-104"
        assert entries[0].content == "msg-055"
    finally:
        await mgr._storage.close()


@pytest.mark.asyncio
async def test_default_window_is_unchanged_for_other_callers() -> None:
    """The established constraint from #1873: other callers keep the old default."""
    mgr = await _manager_with_messages(105)
    try:
        entries = await mgr._storage.get_transcript(
            (await mgr.get_session(KEY)).session_id, limit=50
        )

        assert len(entries) == 50
        assert entries[0].content == "msg-000"
        assert entries[-1].content == "msg-049"
    finally:
        await mgr._storage.close()


@pytest.mark.asyncio
async def test_newest_first_breaks_ties_by_insertion_order() -> None:
    from agentos.session.models import TranscriptEntry

    storage = SessionStorage(":memory:")
    await storage.connect()
    mgr = SessionManager(storage, inject_time_prefix=False)
    try:
        node = await mgr.create(KEY)
        for content in ("first", "second", "third"):
            await storage.append_transcript_entry(
                TranscriptEntry(
                    session_id=node.session_id,
                    session_key=node.session_key,
                    role="user",
                    content=content,
                    created_at=12345,
                )
            )

        entries = await storage.get_transcript(node.session_id, limit=2, newest_first=True)

        assert [e.content for e in entries] == ["second", "third"]
    finally:
        await storage.close()


# --------------------------------------------------------------------------
# caller 1: sessions_history
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_history_returns_the_most_recent_messages(monkeypatch) -> None:
    from agentos.tools.builtin import sessions as sessions_tools

    mgr = await _manager_with_messages(30)
    monkeypatch.setattr(sessions_tools, "_session_manager", mgr)

    try:
        payload = json.loads(await sessions_tools.sessions_history(session_key=KEY, limit=20))
    finally:
        await mgr._storage.close()

    contents = [m["content"] for m in payload["messages"]]
    assert len(contents) == 20
    assert contents[-1] == "msg-029"
    assert contents[0] == "msg-010"


# --------------------------------------------------------------------------
# caller 2: _read_child_result
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_child_result_finds_the_actual_latest_assistant_message() -> None:
    from agentos.gateway import subagent_announce

    mgr = await _manager_with_messages(80)
    # The real answer lands after the 50-entry window that used to be taken
    # from the start of the transcript instead of the end.
    await mgr.append_message(KEY, "assistant", "the real final answer")

    try:
        result = await subagent_announce._read_child_result(KEY, session_manager=mgr)
    finally:
        await mgr._storage.close()

    assert result["text"] == "the real final answer"
