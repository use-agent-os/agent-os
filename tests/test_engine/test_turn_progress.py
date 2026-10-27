"""A running turn's tool rounds are durable; unanswered calls get an unknown outcome.

End to end through the ``TurnRunner`` with a real ``SessionManager`` on a
SQLite file. A "kill" is simulated by snapshotting the database while the
third tool call is still running (no cancel handler, no finalizer ran), then
opening the snapshot as a restarted process would.

Fixtures are synthetic.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.chat.history import transcript_entries_to_chat_messages
from agentos.engine.history import (
    is_unknown_tool_outcome_result,
    repair_tool_pairing,
)
from agentos.engine.runtime import TurnRunner
from agentos.engine.transcript import build_history_from_transcript
from agentos.engine.turn_progress import (
    TURN_PROGRESS_KEY,
    TurnProgress,
    TurnProgressWriter,
    classify_transcript_tail,
    hide_live_turn_progress,
    is_turn_progress_entry,
)
from agentos.engine.types import DoneEvent
from agentos.gateway.config import (
    AgentOSRouterConfig,
    AttachmentsConfig,
    GatewayConfig,
)
from agentos.provider import (
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
)
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.provider import ToolUseEndEvent as ProviderToolUseEnd
from agentos.provider import ToolUseStartEvent as ProviderToolUseStart
from agentos.session.manager import SessionManager
from agentos.session.models import AgentTaskRecord, AgentTaskStatus, TranscriptEntry
from agentos.session.storage import SessionStorage, StaleEpochError
from agentos.tools.registry import ToolRegistry, ToolSpec
from agentos.tools.types import CallerKind, ToolContext

KEY = "agent:main:webchat:turn-progress"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _RoundsProvider:
    """``rounds`` tool calls (``act`` with n = 1..rounds), then a text answer."""

    provider_name = "test"

    def __init__(self, rounds: int = 3) -> None:
        self.model = "test/model"
        self.rounds = rounds
        self.requests: list[list[Message]] = []

    def chat(self, messages: list[Message], tools=None, config=None) -> AsyncIterator[Any]:
        self.requests.append(list(messages))
        return self._stream(len(self.requests))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number <= self.rounds:
            tool_use_id = f"act-{call_number}"
            yield ProviderText(text=f"step {call_number}. ")
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="act")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="act",
                arguments={"n": call_number},
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=10, output_tokens=2)
            return
        yield ProviderText(text="all done")
        yield ProviderDone(stop_reason="end_turn", input_tokens=10, output_tokens=3)

    async def list_models(self) -> list[Any]:
        return []


class _AnswerProvider(_RoundsProvider):
    def __init__(self) -> None:
        super().__init__(rounds=0)


class _SelectorClone:
    current_config = SimpleNamespace(model="test/model")

    def __init__(self, provider: _RoundsProvider) -> None:
        self.provider = provider

    def override_model(self, model: str) -> None:
        self.current_config = SimpleNamespace(model=model)

    def resolve(self) -> _RoundsProvider:
        return self.provider


class _Selector:
    def __init__(self, provider: _RoundsProvider) -> None:
        self.provider = provider

    def clone(self) -> _SelectorClone:
        return _SelectorClone(self.provider)


class _Gate:
    """Holds the ``act`` call with a given ``n`` until released."""

    def __init__(self, hold: int | None) -> None:
        self.hold = hold
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[int] = []


def _registry(gate: _Gate) -> ToolRegistry:
    registry = ToolRegistry()

    async def act(n: int) -> str:
        gate.calls.append(n)
        if n == gate.hold:
            gate.started.set()
            await gate.release.wait()
        return f"acted {n}"

    registry.register(
        ToolSpec(
            name="act",
            description="Do something with side effects",
            parameters={
                "type": "object",
                "properties": {"n": {"type": "integer"}},
                "required": ["n"],
            },
        ),
        act,
    )
    return registry


async def _open(db_path: Path, tmp_path: Path) -> tuple[SessionStorage, SessionManager]:
    storage = SessionStorage(str(db_path))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False, checkpoint_workspace_dir=tmp_path)
    return storage, manager


def _runner(provider: _RoundsProvider, manager: SessionManager, gate: _Gate, tmp_path: Path):
    return TurnRunner(
        provider_selector=_Selector(provider),
        tool_registry=_registry(gate),
        session_manager=manager,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            agentos_router=AgentOSRouterConfig(enabled=False),
        ),
    )


async def _run(runner: TurnRunner, manager: SessionManager, text: str, tmp_path: Path):
    await manager.append_message(KEY, "user", text)
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


def _snapshot(db_path: Path, target: Path) -> None:
    """Copy the database as it is on disk right now (what a restart would see)."""
    source = sqlite3.connect(str(db_path))
    dest = sqlite3.connect(str(target))
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()


def _blocks(messages: list[Message]) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for message in messages:
        if isinstance(message.content, str):
            out.append((message.role, "text", message.content[:40]))
            continue
        for block in message.content:
            if isinstance(block, ContentBlockToolUse):
                out.append((message.role, "tool_use", block.id))
            elif isinstance(block, ContentBlockToolResult):
                label = "unknown" if is_unknown_tool_outcome_result(block) else str(block.content)
                out.append((message.role, f"tool_result:{block.tool_use_id}", label))
            elif isinstance(block, ContentBlockText):
                out.append((message.role, "text", block.text[:40]))
    return out


# ---------------------------------------------------------------------------
# Kill mid-turn, then restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_mid_turn_keeps_completed_rounds_and_the_running_call(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    storage, manager = await _open(db_path, tmp_path)
    await manager.create(KEY)
    # A task the gateway was running when the process died.
    await storage.create_agent_task(
        AgentTaskRecord(session_key=KEY, status=AgentTaskStatus.RUNNING, source_kind="chat")
    )
    gate = _Gate(hold=3)
    provider = _RoundsProvider(rounds=3)
    runner = _runner(provider, manager, gate, tmp_path)

    turn = asyncio.create_task(_run(runner, manager, "do three things", tmp_path))
    await asyncio.wait_for(gate.started.wait(), timeout=10)

    # --- the process dies here: copy what is on disk ---------------------
    snapshot_path = tmp_path / "after-kill.db"
    _snapshot(db_path, snapshot_path)

    # The original process finishes normally (checked further below).
    gate.release.set()
    events = await asyncio.wait_for(turn, timeout=10)
    assert any(isinstance(event, DoneEvent) for event in events)

    # --- restart on the snapshot -----------------------------------------
    storage2, manager2 = await _open(snapshot_path, tmp_path)
    try:
        # The startup sweep still marks the dead run's task abandoned.
        tasks = await storage2.list_agent_tasks()
        assert [task.status for task in tasks] == [AgentTaskStatus.ABANDONED]

        rows = await manager2.get_transcript(KEY)
        assert [row.role for row in rows] == ["user", "assistant"]
        progress_row = rows[1]
        assert is_turn_progress_entry(progress_row)
        assert progress_row.content == ""
        assert progress_row.turn_usage is None

        tail = classify_transcript_tail(rows)
        assert tail.status == "interrupted"
        assert tail.rounds_completed == 2
        assert tail.pending_tool_use_ids == ("act-3",)
        assert tail.input_entry_id == rows[0].message_id

        # History: both completed rounds with their real results, and the
        # third call kept with an unknown-outcome result.
        loaded = build_history_from_transcript(rows)
        history = repair_tool_pairing(loaded.messages)
        assert _blocks(history)[1:] == [
            ("assistant", "text", "step 1. "),
            ("assistant", "tool_use", "act-1"),
            ("user", "tool_result:act-1", "acted 1"),
            ("assistant", "text", "step 2. "),
            ("assistant", "tool_use", "act-2"),
            ("user", "tool_result:act-2", "acted 2"),
            ("assistant", "text", "step 3. "),
            ("assistant", "tool_use", "act-3"),
            ("user", "tool_result:act-3", "unknown"),
        ]

        # Display: one assistant message with the calls, the running one
        # showing what it was asked to do.
        shown = transcript_entries_to_chat_messages(rows)
        segments = shown[1]["tool_calls"]
        assert [(s["type"], s.get("tool_use_id")) for s in segments if s["type"] != "text"] == [
            ("tool_use", "act-1"),
            ("tool_result", "act-1"),
            ("tool_use", "act-2"),
            ("tool_result", "act-2"),
            ("tool_use", "act-3"),
        ]
        assert segments[-1]["input"] == {"n": 3}

        # The next turn after the restart: the model sees all of it.
        answer = _AnswerProvider()
        restarted = _runner(answer, manager2, _Gate(hold=None), tmp_path)
        await _run(restarted, manager2, "what happened?", tmp_path)
        sent = _blocks(answer.requests[0])
        assert ("user", "tool_result:act-1", "acted 1") in sent
        assert ("user", "tool_result:act-2", "acted 2") in sent
        assert ("user", "tool_result:act-3", "unknown") in sent
        stub_index = sent.index(("user", "tool_result:act-3", "unknown"))
        assert sent[stub_index - 1] == ("assistant", "tool_use", "act-3")
        assert sent[stub_index + 1][:2] == ("user", "text")
        assert sent[stub_index + 1][2].startswith("what happened?")
        final_rows = await manager2.get_transcript(KEY)
        assert classify_transcript_tail(final_rows).status == "finished"
    finally:
        await storage2.close()

    # --- the uninterrupted run: one final row, no leftovers --------------
    try:
        rows = await manager.get_transcript(KEY)
        assert [row.role for row in rows] == ["user", "assistant"]
        final = rows[1]
        assert not is_turn_progress_entry(final)
        assert TURN_PROGRESS_KEY not in (final.provider_messages or {})
        assert final.turn_usage is not None
        assert classify_transcript_tail(rows).status == "finished"
        assert gate.calls == [1, 2, 3]
    finally:
        await storage.close()


async def _finished_turn(tmp_path: Path, *, progress: bool, monkeypatch: pytest.MonkeyPatch):
    if not progress:
        monkeypatch.setattr(TurnProgressWriter, "supported", staticmethod(lambda _m: False))
    db_path = tmp_path / f"finished-{progress}.db"
    storage, manager = await _open(db_path, tmp_path)
    try:
        await manager.create(KEY)
        provider = _RoundsProvider(rounds=3)
        runner = _runner(provider, manager, _Gate(hold=None), tmp_path)
        await _run(runner, manager, "do three things", tmp_path)
        rows = await manager.get_transcript(KEY)
        node = await storage.get_session(KEY)
        assert node is not None
        return rows, node, provider
    finally:
        monkeypatch.undo()
        await storage.close()


@pytest.mark.asyncio
async def test_finished_turn_is_identical_with_and_without_progress_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written: list[TurnProgress] = []
    real_record = TurnProgressWriter.record

    async def _spy(self: TurnProgressWriter, progress: TurnProgress) -> None:
        written.append(progress)
        await real_record(self, progress)

    monkeypatch.setattr(TurnProgressWriter, "record", _spy)
    with_rows, with_node, with_provider = await _finished_turn(
        tmp_path, progress=True, monkeypatch=monkeypatch
    )
    without_rows, without_node, without_provider = await _finished_turn(
        tmp_path, progress=False, monkeypatch=monkeypatch
    )

    # Progress was written twice per round (calls issued, results in).
    assert [(p.round, p.phase) for p in written] == [
        (1, "tools_running"),
        (1, "round_complete"),
        (2, "tools_running"),
        (2, "round_complete"),
        (3, "tools_running"),
        (3, "round_complete"),
    ]
    # Same rows, same record, same display segments, same usage roll-up
    # (row ids and the minute-resolution clock line aside).
    assert [row.role for row in with_rows] == [row.role for row in without_rows]
    assert len(with_rows) == 2
    with_final, without_final = with_rows[1], without_rows[1]
    assert _normalized(with_final.provider_messages, with_rows[0].message_id) == _normalized(
        without_final.provider_messages, without_rows[0].message_id
    )
    assert with_final.tool_calls == without_final.tool_calls
    assert with_final.content == without_final.content
    assert (with_final.turn_usage or {}).get("input_tokens") == (
        without_final.turn_usage or {}
    ).get("input_tokens")
    assert with_node.total_tokens == without_node.total_tokens
    assert with_node.input_tokens == without_node.input_tokens
    assert with_node.output_tokens == without_node.output_tokens
    # Every provider request of the turn was the same.
    assert len(with_provider.requests) == len(without_provider.requests) == 4
    for with_request, without_request in zip(
        with_provider.requests, without_provider.requests, strict=True
    ):
        assert _normalized([m.model_dump(mode="json") for m in with_request]) == _normalized(
            [m.model_dump(mode="json") for m in without_request]
        )


_CLOCK_LINE = re.compile(r"Current local date/time: [^\\\n]*")


def _normalized(value: Any, input_id: str | None = None) -> str:
    text = json.dumps(value, sort_keys=True)
    if input_id:
        text = text.replace(input_id, "<input>")
    return _CLOCK_LINE.sub("Current local date/time: <now>", text)


@pytest.mark.asyncio
async def test_cancelled_turn_replaces_its_progress_row(tmp_path: Path) -> None:
    storage, manager = await _open(tmp_path / "cancel.db", tmp_path)
    try:
        await manager.create(KEY)
        gate = _Gate(hold=2)
        runner = _runner(_RoundsProvider(rounds=3), manager, gate, tmp_path)
        turn = asyncio.create_task(_run(runner, manager, "do things", tmp_path))
        await asyncio.wait_for(gate.started.wait(), timeout=10)
        rows = await manager.get_transcript(KEY)
        assert is_turn_progress_entry(rows[-1])

        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

        rows = await manager.get_transcript(KEY)
        assert [row.role for row in rows] == ["user", "assistant"]
        assert not is_turn_progress_entry(rows[1])
        assert "[interrupted]" in (rows[1].content or "")
        history = repair_tool_pairing(build_history_from_transcript(rows).messages)
        blocks = _blocks(history)
        assert ("user", "tool_result:act-1", "acted 1") in blocks
        assert ("user", "tool_result:act-2", "unknown") in blocks
    finally:
        await storage.close()


# ---------------------------------------------------------------------------
# Storage: the swap is atomic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_rows_replace_each_other_and_the_final_row_replaces_them(
    tmp_path: Path,
) -> None:
    storage, manager = await _open(tmp_path / "swap.db", tmp_path)
    try:
        await manager.create(KEY)
        await manager.append_message(KEY, "user", "hi")
        record = {"kind": "agentos.provider_messages", "v": 1, "mode": "append", "messages": []}
        for round_index in (1, 2):
            await manager.write_turn_progress(
                KEY,
                message_id="progress-1",
                tool_calls=[{"type": "text", "text": f"round {round_index}"}],
                provider_messages={**record, TURN_PROGRESS_KEY: {"state": "running"}},
            )
        rows = await manager.get_transcript(KEY)
        assert [row.message_id for row in rows][1:] == ["progress-1"]
        assert rows[1].tool_calls == [{"type": "text", "text": "round 2"}]

        await manager.append_message(KEY, "assistant", "final", replaces_message_id="progress-1")
        rows = await manager.get_transcript(KEY)
        assert [(row.role, row.content) for row in rows] == [
            ("user", "hi"),
            ("assistant", "final"),
        ]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_a_refused_replacement_keeps_the_old_row(tmp_path: Path) -> None:
    storage, manager = await _open(tmp_path / "epoch.db", tmp_path)
    try:
        await manager.create(KEY)
        await manager.write_turn_progress(
            KEY,
            message_id="progress-1",
            tool_calls=None,
            provider_messages={TURN_PROGRESS_KEY: {"state": "running"}},
        )
        node = await storage.get_session(KEY)
        assert node is not None
        entry = TranscriptEntry(
            session_id=node.session_id, session_key=KEY, role="assistant", content="final"
        )
        with pytest.raises(StaleEpochError):
            await storage.append_transcript_entry(
                entry,
                expected_epoch=(node.epoch or 0) + 5,
                replaces_message_id="progress-1",
            )
        rows = await manager.get_transcript(KEY)
        assert [row.message_id for row in rows] == ["progress-1"]
    finally:
        await storage.close()


# ---------------------------------------------------------------------------
# Classification and display filter
# ---------------------------------------------------------------------------


def _row(role: str, *, created_at: int = 1, progress: dict[str, Any] | None = None, **fields):
    record = None
    if progress is not None:
        record = {
            "kind": "agentos.provider_messages",
            "v": 1,
            "mode": "append",
            "input_entry_id": "u1",
            "messages": progress.pop("messages", []),
            TURN_PROGRESS_KEY: {"state": "running", **progress},
        }
    return TranscriptEntry(
        session_id="s",
        session_key=KEY,
        role=role,
        content=fields.pop("content", ""),
        provider_messages=record,
        created_at=created_at,
        **fields,
    )


def test_classify_transcript_tail() -> None:
    assert classify_transcript_tail([]).status == "empty"
    user = _row("user", content="hi", message_id="u1")
    assert classify_transcript_tail([user]).status == "unanswered_input"
    done = _row("assistant", content="hello")
    assert classify_transcript_tail([user, done]).status == "finished"
    error = _row("system", content="Error: boom")
    # A turn that failed after a round: its error row closes it.
    assert (
        classify_transcript_tail([user, _row("assistant", progress={"round": 1}), error]).status
        == "finished"
    )

    call = {"role": "assistant", "content": [{"type": "tool_use", "id": "c2", "input": {}}]}
    running = _row("assistant", progress={"round": 2, "messages": [call]})
    follow_up = _row("user", content="also this")
    tail = classify_transcript_tail([user, running, follow_up])
    assert tail.status == "interrupted"
    assert tail.rounds_completed == 1
    assert tail.pending_tool_use_ids == ("c2",)
    assert tail.input_entry_id == "u1"
    assert tail.trailing_user_entries == (follow_up,)

    result = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "c2", "content": "ok"}],
    }
    complete = _row("assistant", progress={"round": 2, "messages": [call, result]})
    tail = classify_transcript_tail([user, complete])
    assert (tail.status, tail.rounds_completed, tail.pending_tool_use_ids) == (
        "interrupted",
        2,
        (),
    )


def test_live_progress_row_is_hidden_only_while_its_run_is_in_flight() -> None:
    old_crash = _row("assistant", created_at=100, progress={"round": 1})
    user = _row("user", created_at=200, content="again")
    live = _row("assistant", created_at=300, progress={"round": 1})
    entries = [old_crash, user, live]

    assert hide_live_turn_progress(entries, None) is entries
    assert hide_live_turn_progress(entries, {"started_at": 250}) == [old_crash, user]
    # No start time known: hide every in-progress row rather than show it twice.
    assert hide_live_turn_progress(entries, {"started_at": None}) == [user]


@pytest.mark.asyncio
async def test_writer_failures_never_break_the_turn() -> None:
    class _Broken:
        async def write_turn_progress(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("disk full")

        async def append_message(self, *_args: Any, replaces_message_id=None, **_kwargs: Any):
            return None

    class _NoLock:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    writer = TurnProgressWriter(
        session_key=KEY,
        session_manager=_Broken(),
        segments=[],
        write_context=lambda _key: _NoLock(),
    )
    assert TurnProgressWriter.supported(_Broken())
    await writer.record(TurnProgress(record={"messages": []}, round=1, phase="round_complete"))
    assert writer.writes == 0
    assert writer.message_id is not None
