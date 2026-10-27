"""Mid-turn steering meets per-round persistence.

A steered user message is written as its own transcript row when the running
turn takes it, and the turn keeps one replaceable in-progress assistant row.
These tests run a real ``TurnRunner`` on a SQLite ``SessionManager`` with a
steering source that writes its rows the way the gateway does, and check what
a restarted process (or a viewer) reads back: the steered message once, in the
place the model met it.

Fixtures are synthetic.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agentos.chat.history import transcript_entries_to_chat_messages
from agentos.engine.history import repair_tool_pairing
from agentos.engine.steering import STEER_SEGMENT_TYPE, SteeringMessage, bind_steering
from agentos.engine.transcript import build_history_from_transcript, decode_transcript_record
from agentos.engine.turn_progress import (
    TURN_PROGRESS_KEY,
    classify_transcript_tail,
    is_turn_progress_entry,
)
from agentos.engine.types import DoneEvent, SteeringEvent
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import Message
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.provider import ToolUseEndEvent as ProviderToolUseEnd
from agentos.provider import ToolUseStartEvent as ProviderToolUseStart
from agentos.session.manager import SessionManager
from agentos.session.models import TranscriptEntry
from tests.test_engine.test_turn_progress import (
    KEY,
    _blocks,
    _Gate,
    _open,
    _runner,
    _snapshot,
)


class _WritingSteering:
    """A steering queue whose ``take`` writes the user row, as the gateway does."""

    def __init__(self, manager: SessionManager) -> None:
        self._manager = manager
        self.queue: list[str] = []
        self.written: list[str] = []

    @property
    def session_key(self) -> str:
        return KEY

    def has_pending(self) -> bool:
        return bool(self.queue)

    async def take(self) -> SteeringMessage | None:
        if not self.queue:
            return None
        text = self.queue.pop(0)
        entry = await self._manager.append_message(KEY, "user", text)
        self.written.append(entry.message_id)
        return SteeringMessage(text=text, message_id=entry.message_id, task_id="t-steer")


class _ScriptProvider:
    """Plays one scripted response per model call.

    Each step is ``("text", str)`` or ``("tool", n)`` (text "step n. " then an
    ``act`` call with that n). A ``steer`` hook queues a message at the start
    of the model call it names (1-based).
    """

    provider_name = "test"

    def __init__(self, steps: list[tuple[str, Any]]) -> None:
        self.model = "test/model"
        self.steps = steps
        self.requests: list[list[Message]] = []
        self.on_call: dict[int, Any] = {}

    def chat(self, messages: list[Message], tools=None, config=None) -> AsyncIterator[Any]:
        self.requests.append(list(messages))
        return self._stream(len(self.requests))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        hook = self.on_call.get(call_number)
        if hook is not None:
            result = hook()
            if asyncio.iscoroutine(result):
                await result
        kind, value = self.steps[min(call_number, len(self.steps)) - 1]
        if kind == "tool":
            tool_use_id = f"act-{value}"
            yield ProviderText(text=f"step {value}. ")
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="act")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id, tool_name="act", arguments={"n": value}
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=10, output_tokens=2)
            return
        yield ProviderText(text=str(value))
        yield ProviderDone(stop_reason="end_turn", input_tokens=10, output_tokens=3)

    async def list_models(self) -> list[Any]:
        return []


async def _run_steered(runner, manager, steering, text: str, tmp_path: Path) -> list[Any]:
    from agentos.tools.types import CallerKind, ToolContext

    await manager.append_message(KEY, "user", text)
    with bind_steering(steering):
        return [
            event
            async for event in runner.run(
                text,
                KEY,
                tool_context=ToolContext(caller_kind=CallerKind.WEB, workspace_dir=str(tmp_path)),
                history_has_persisted_user=True,
                no_memory_capture=True,
            )
        ]


def _shown(rows: list[Any]) -> list[tuple[str, str]]:
    return [(m["role"], m["text"]) for m in transcript_entries_to_chat_messages(rows)]


# ---------------------------------------------------------------------------
# Crash after a steer: history has the steered message once, in place
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crash_after_a_steer_during_tools_replays_it_once_after_the_results(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sessions.db"
    storage, manager = await _open(db_path, tmp_path)
    await manager.create(KEY)
    steering = _WritingSteering(manager)
    gate = _Gate(hold=2)
    provider = _ScriptProvider([("tool", 1), ("tool", 2), ("text", "done")])
    # The user sends a message while act-1 runs (during the first call).
    provider.on_call[1] = lambda: steering.queue.append("use the blue one")
    runner = _runner(provider, manager, gate, tmp_path)
    turn = asyncio.create_task(_run_steered(runner, manager, steering, "do it", tmp_path))
    await asyncio.wait_for(gate.started.wait(), timeout=10)
    _snapshot(db_path, tmp_path / "kill.db")
    gate.release.set()
    await asyncio.wait_for(turn, timeout=10)
    await storage.close()

    storage2, manager2 = await _open(tmp_path / "kill.db", tmp_path)
    try:
        rows = await manager2.get_transcript(KEY)
        assert [r.role for r in rows] == ["user", "user", "assistant"]
        assert rows[1].message_id == steering.written[0]
        progress = rows[2]
        assert is_turn_progress_entry(progress)
        record = decode_transcript_record(progress.provider_messages)
        assert record is not None
        assert record.steered_entry_ids == (steering.written[0],)

        tail = classify_transcript_tail(rows)
        assert tail.status == "interrupted"
        assert tail.trailing_user_entries == ()

        history = repair_tool_pairing(build_history_from_transcript(rows).messages)
        texts = [b for b in _blocks(history) if b[2] == "use the blue one"]
        assert len(texts) == 1
        blocks = _blocks(history)
        steer_at = blocks.index(texts[0])
        assert blocks[steer_at - 1] == ("user", "tool_result:act-1", "acted 1")
        assert blocks[steer_at + 1] == ("assistant", "text", "step 2. ")
    finally:
        await storage2.close()


@pytest.mark.asyncio
async def test_crash_during_the_call_after_a_steer_at_an_answer_keeps_both(
    tmp_path: Path,
) -> None:
    """A message taken when the model answered is on record before the next call.

    Before, nothing was written until the next tool round: a crash during the
    model call that followed lost the answer, and the two user rows read as a
    fresh, unanswered input.
    """
    db_path = tmp_path / "sessions.db"
    storage, manager = await _open(db_path, tmp_path)
    await manager.create(KEY)
    steering = _WritingSteering(manager)
    provider = _ScriptProvider([("text", "first answer"), ("text", "second answer")])
    provider.on_call[1] = lambda: steering.queue.append("and also this")
    started = asyncio.Event()
    release = asyncio.Event()

    async def _hold() -> None:
        started.set()
        await release.wait()

    provider.on_call[2] = _hold
    runner = _runner(provider, manager, _Gate(hold=None), tmp_path)
    turn = asyncio.create_task(_run_steered(runner, manager, steering, "hi", tmp_path))
    await asyncio.wait_for(started.wait(), timeout=10)
    _snapshot(db_path, tmp_path / "kill.db")
    release.set()
    await asyncio.wait_for(turn, timeout=10)
    await storage.close()

    storage2, manager2 = await _open(tmp_path / "kill.db", tmp_path)
    try:
        rows = await manager2.get_transcript(KEY)
        assert [r.role for r in rows] == ["user", "user", "assistant"]
        assert is_turn_progress_entry(rows[2])
        tail = classify_transcript_tail(rows)
        assert tail.status == "interrupted"
        assert tail.trailing_user_entries == ()

        history = build_history_from_transcript(rows).messages
        blocks = [b for b in _blocks(history) if not b[2].startswith("Current local")]
        assert [b for b in blocks if b[0] == "assistant" or "also" in b[2]] == [
            ("assistant", "text", "first answer"),
            ("user", "text", "and also this"),
        ]
        assert blocks[-1] == ("user", "text", "and also this")
    finally:
        await storage2.close()


# ---------------------------------------------------------------------------
# Display segments: a marker where the message came in
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finished_steered_turn_marks_the_steer_in_its_segments(tmp_path: Path) -> None:
    storage, manager = await _open(tmp_path / "sessions.db", tmp_path)
    try:
        await manager.create(KEY)
        steering = _WritingSteering(manager)
        provider = _ScriptProvider([("tool", 1), ("text", "switched to blue")])
        provider.on_call[1] = lambda: steering.queue.append("use the blue one")
        runner = _runner(provider, manager, _Gate(hold=None), tmp_path)
        events = await _run_steered(runner, manager, steering, "do it", tmp_path)
        assert any(isinstance(e, SteeringEvent) for e in events)
        assert any(isinstance(e, DoneEvent) for e in events)

        rows = await manager.get_transcript(KEY)
        assert [r.role for r in rows] == ["user", "user", "assistant"]
        final = rows[2]
        assert not is_turn_progress_entry(final)
        assert [(s["type"], s.get("message_id")) for s in final.tool_calls or []] == [
            ("text", None),
            ("tool_use", None),
            ("tool_result", None),
            (STEER_SEGMENT_TYPE, steering.written[0]),
            ("text", None),
        ]
        assert classify_transcript_tail(rows).status == "finished"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_steer_at_an_answer_closes_the_text_segment(tmp_path: Path) -> None:
    storage, manager = await _open(tmp_path / "sessions.db", tmp_path)
    try:
        await manager.create(KEY)
        steering = _WritingSteering(manager)
        provider = _ScriptProvider([("text", "first answer"), ("text", "second answer")])
        provider.on_call[1] = lambda: steering.queue.append("and also this")
        runner = _runner(provider, manager, _Gate(hold=None), tmp_path)
        await _run_steered(runner, manager, steering, "hi", tmp_path)

        final = (await manager.get_transcript(KEY))[-1]
        assert final.tool_calls == [
            {"type": "text", "text": "first answer"},
            {"type": STEER_SEGMENT_TYPE, "message_id": steering.written[0]},
            {"type": "text", "text": "second answer"},
        ]
    finally:
        await storage.close()


# ---------------------------------------------------------------------------
# Tail classification: steered rows are part of their turn, in either order
# ---------------------------------------------------------------------------


def _entry(role: str, message_id: str, *, record: dict[str, Any] | None = None) -> TranscriptEntry:
    return TranscriptEntry(
        session_id="s",
        session_key=KEY,
        role=role,
        message_id=message_id,
        content="" if role == "assistant" else message_id,
        provider_messages=record,
    )


def _record(*, steered: tuple[str, ...] = (), running: bool = True) -> dict[str, Any]:
    record: dict[str, Any] = {
        "kind": "agentos.provider_messages",
        "v": 1,
        "mode": "append",
        "input_entry_id": "u1",
        "messages": [],
    }
    if steered:
        record["steered_entry_ids"] = list(steered)
    if running:
        record[TURN_PROGRESS_KEY] = {"state": "running", "round": 1, "phase": "round_complete"}
    return record


def test_steered_row_before_the_swapped_progress_row_is_not_input() -> None:
    user = _entry("user", "u1")
    steered = _entry("user", "s1")
    progress = _entry("assistant", "p", record=_record(steered=("s1",)))
    tail = classify_transcript_tail([user, steered, progress])
    assert tail.status == "interrupted"
    assert tail.trailing_user_entries == ()


def test_steered_row_after_the_progress_row_naming_it_is_not_input() -> None:
    user = _entry("user", "u1")
    progress = _entry("assistant", "p", record=_record(steered=("s1",)))
    steered = _entry("user", "s1")
    tail = classify_transcript_tail([user, progress, steered])
    assert tail.status == "interrupted"
    assert tail.trailing_user_entries == ()


def test_follow_up_queued_before_the_swapped_progress_row_is_trailing() -> None:
    """A follow-up written mid-turn ends up before the rewritten progress row."""
    user = _entry("user", "u1")
    follow_up = _entry("user", "f1")
    steered = _entry("user", "s1")
    progress = _entry("assistant", "p", record=_record(steered=("s1",)))
    later = _entry("user", "f2")
    tail = classify_transcript_tail([user, follow_up, steered, progress, later])
    assert tail.status == "interrupted"
    assert tail.trailing_user_entries == (follow_up, later)


def test_steered_row_the_progress_row_does_not_name_yet_is_trailing() -> None:
    """Died between writing the steered row and the next progress write."""
    user = _entry("user", "u1")
    progress = _entry("assistant", "p", record=_record())
    steered = _entry("user", "s1")
    tail = classify_transcript_tail([user, progress, steered])
    assert tail.status == "interrupted"
    assert tail.trailing_user_entries == (steered,)


def test_finished_turn_with_its_steered_row_sorted_after_it_is_finished() -> None:
    user = _entry("user", "u1")
    final = _entry("assistant", "a", record=_record(steered=("s1",), running=False))
    steered = _entry("user", "s1")
    assert classify_transcript_tail([user, final, steered]).status == "finished"
    fresh = _entry("user", "n1")
    tail = classify_transcript_tail([user, final, steered, fresh])
    assert (tail.status, tail.trailing_user_entries) == ("unanswered_input", (fresh,))
