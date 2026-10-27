"""chat.history shows a steered turn in the order it happened.

A message steered into a running turn is its own user row, written when the
turn took it, so it sorts before the turn's one assistant row (which also
holds the work done before the message). The row's display segments mark the
point with a ``steer`` segment; chat.history cuts the assistant message there
and puts the user message between the parts. Fixtures are synthetic.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentos.gateway.rpc import RpcContext
from agentos.gateway.rpc_chat import _handle_chat_history, _split_steered_turns
from agentos.gateway.session_streams import SessionStreamRegistry
from agentos.session.models import TranscriptEntry

KEY = "agent:main:webchat:steer-history"


class _Manager:
    def __init__(self, entries: list[TranscriptEntry]) -> None:
        self._entries = entries

    async def get_transcript(self, session_key: str) -> list[TranscriptEntry]:
        return self._entries

    async def get_canonical_transcript(self, session_key: str) -> list[TranscriptEntry]:
        raise RuntimeError("canonical unavailable")

    async def get_summaries(self, session_key: str) -> list[Any]:
        return []


def _user(idx: int, text: str) -> TranscriptEntry:
    return TranscriptEntry(
        id=idx,
        session_id="s",
        session_key=KEY,
        role="user",
        content=text,
        created_at=idx,
        message_id=f"u-{idx}",
    )


_STEERED_SEGMENTS: list[dict[str, Any]] = [
    {"type": "text", "text": "Looking at the red one. "},
    {"type": "tool_use", "tool_use_id": "c1", "name": "act", "input": {"n": 1}},
    {"type": "tool_result", "tool_use_id": "c1", "name": "act", "result": "ok"},
    {"type": "steer", "message_id": "u-2"},
    {"type": "text", "text": "Switched to the blue one."},
]


def _assistant(
    idx: int,
    segments: list[dict[str, Any]],
    *,
    content: str,
    progress: bool = False,
) -> TranscriptEntry:
    record: dict[str, Any] = {
        "kind": "agentos.provider_messages",
        "v": 1,
        "mode": "append",
        "input_entry_id": "u-1",
        "steered_entry_ids": ["u-2"],
        "messages": [],
    }
    if progress:
        record["turn_progress"] = {"state": "running", "round": 1}
    return TranscriptEntry(
        id=idx,
        session_id="s",
        session_key=KEY,
        role="assistant",
        content=content,
        tool_calls=segments,
        provider_messages=record,
        turn_usage=None if progress else {"model": "test/model", "input_tokens": 9},
        created_at=idx,
        message_id=f"a-{idx}",
    )


def _steered_transcript() -> list[TranscriptEntry]:
    return [
        _user(1, "do it"),
        _user(2, "use the blue one"),
        _assistant(
            3,
            [dict(segment) for segment in _STEERED_SEGMENTS],
            content="Looking at the red one. Switched to the blue one.",
        ),
    ]


def _ctx(entries: list[TranscriptEntry]) -> RpcContext:
    return RpcContext(conn_id="test", session_manager=_Manager(entries))


def _shape(messages: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    return [(m["role"], str(m.get("message_id")), m["text"]) for m in messages]


@pytest.mark.asyncio
async def test_history_puts_the_steered_message_between_the_work_before_and_after() -> None:
    result = await _handle_chat_history({"sessionKey": KEY}, _ctx(_steered_transcript()))
    messages = result["messages"]
    assert _shape(messages) == [
        ("user", "u-1", "do it"),
        ("assistant", "a-3#part1", "Looking at the red one."),
        ("user", "u-2", "use the blue one"),
        ("assistant", "a-3", "Switched to the blue one."),
    ]
    before, after = messages[1], messages[3]
    assert [s["type"] for s in before["tool_calls"]] == ["text", "tool_use", "tool_result"]
    assert [s["type"] for s in after["tool_calls"]] == ["text"]
    assert before["split_from"] == "a-3"
    # The turn as a whole (usage, model, row id) stays on the last part.
    assert "usage" not in before and "transcript_id" not in before
    assert after["usage"]["input_tokens"] == 9
    assert after["transcript_id"] == 3
    # The first part reads as happening when the message came in.
    assert before["timestamp"] == messages[2]["timestamp"]
    # Cursors still name transcript rows.
    assert result["newest_cursor"] == "3|3"
    assert result["loaded_count"] == 3


@pytest.mark.asyncio
async def test_steer_at_an_answer_splits_the_text() -> None:
    entries = [
        _user(1, "hi"),
        _user(2, "and also this"),
        _assistant(
            3,
            [
                {"type": "text", "text": "first answer"},
                {"type": "steer", "message_id": "u-2"},
                {"type": "text", "text": "second answer"},
            ],
            content="first answersecond answer",
        ),
    ]
    result = await _handle_chat_history({"sessionKey": KEY}, _ctx(entries))
    assert [(m["role"], m["text"]) for m in result["messages"]] == [
        ("user", "hi"),
        ("assistant", "first answer"),
        ("user", "and also this"),
        ("assistant", "second answer"),
    ]


def test_rows_without_a_marker_are_returned_unchanged() -> None:
    messages = [
        {"role": "user", "message_id": "u-1", "text": "hi"},
        {
            "role": "assistant",
            "message_id": "a-2",
            "text": "hello",
            "tool_calls": [{"type": "text", "text": "hello"}],
        },
    ]
    assert _split_steered_turns(messages) is messages


def test_a_marker_whose_message_is_not_on_the_page_still_splits() -> None:
    messages = [
        {
            "role": "assistant",
            "message_id": "a-3",
            "text": "beforeafter",
            "tool_calls": [
                {"type": "text", "text": "before"},
                {"type": "steer", "message_id": "elsewhere"},
                {"type": "text", "text": "after"},
            ],
        }
    ]
    assert [(m["message_id"], m["text"]) for m in _split_steered_turns(messages)] == [
        ("a-3#part1", "before"),
        ("a-3", "after"),
    ]


def test_placeholders_are_left_alone() -> None:
    message = {
        "role": "assistant",
        "message_id": "a-3",
        "text": "...",
        "placeholder": True,
        "tool_calls": [{"type": "steer", "message_id": "u-2"}],
    }
    messages = [{"role": "user", "message_id": "u-2", "text": "x"}, message]
    assert _split_steered_turns(messages) is messages


@pytest.mark.asyncio
async def test_delta_with_a_steered_message_already_sent_answers_reset() -> None:
    entries = _steered_transcript()
    # The client saw rows up to the steered one while the turn ran.
    result = await _handle_chat_history({"sessionKey": KEY, "delta_cursor": "2|2"}, _ctx(entries))
    assert result["kind"] == "reset"
    assert [m["message_id"] for m in result["messages"]] == ["u-1", "a-3#part1", "u-2", "a-3"]

    # A delta that holds the whole turn can be split in place.
    later = [_user(4, "next"), _user(5, "use the green one")]
    segments = [dict(segment) for segment in _STEERED_SEGMENTS]
    segments[3] = {"type": "steer", "message_id": "u-5"}
    later.append(_assistant(6, segments, content="Looking at the red one. Switched."))
    result = await _handle_chat_history(
        {"sessionKey": KEY, "delta_cursor": "3|3"}, _ctx([*entries, *later])
    )
    assert result["kind"] == "delta"
    assert [m["message_id"] for m in result["messages"]] == ["u-4", "a-6#part1", "u-5", "a-6"]


@pytest.mark.asyncio
async def test_live_turn_lists_its_settled_part_before_the_steered_message(monkeypatch) -> None:
    import agentos.gateway.rpc_sessions as rpc_sessions

    live_segments = [dict(segment) for segment in _STEERED_SEGMENTS[:4]]
    live_segments.append({"type": "tool_use", "tool_use_id": "c2", "name": "act", "input": {}})
    entries = [
        _user(1, "do it"),
        _user(2, "use the blue one"),
        _assistant(3, live_segments, content="", progress=True),
    ]

    async def _running(_ctx: Any, _key: str) -> dict[str, Any]:
        return {"run_id": "t", "task_id": "t", "status": "running", "started_at": 1}

    monkeypatch.setattr(rpc_sessions, "session_in_flight_run", _running)
    result = await _handle_chat_history({"sessionKey": KEY}, _ctx(entries))
    messages = result["messages"]
    # The work before the message is settled and listed; the part after it
    # is live and shown from in_flight_run, not here.
    assert [(m["role"], m["message_id"]) for m in messages] == [
        ("user", "u-1"),
        ("assistant", "a-3"),
        ("user", "u-2"),
    ]
    assert [s.get("tool_use_id") for s in messages[1]["tool_calls"]] == [None, "c1", "c1"]


def test_in_flight_text_restarts_at_a_steer() -> None:
    streams = SessionStreamRegistry()
    key = "agent:main:webchat:live"
    streams.record(key, "task.running", {"task_id": "t1"})
    streams.record(key, "session.event.text_delta", {"text": "before. "})
    streams.record(key, "session.event.steering", {"text": "change", "message_id": "u-2"})
    streams.record(key, "session.event.text_delta", {"text": "after"})
    snapshot = streams.in_flight_snapshot(key, active_task={"task_id": "t1", "status": "running"})
    assert snapshot is not None
    assert snapshot["text"] == "after"
    assert snapshot["events"][-1]["kind"] == "steering"

    # The aborted frame still carries everything the run said.
    aborted = streams.record(key, "session.event.done", {"task_id": "t1", "state": "aborted"})
    assert aborted["partial_text"] == "before. after"
