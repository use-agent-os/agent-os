"""A run's end is authoritative and replayable, whatever path ends it.

Driven through ``gateway_turn_rig``: a real ``TaskRuntime``, the production
dispatch, ``EventBridge`` and lifecycle listener; only the model is scripted.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from gateway_turn_rig import RecordingConn, ScriptedRunner, turn_rig, wait_for

from agentos.engine.types import DoneEvent, TextDeltaEvent
from agentos.gateway import run_abort
from agentos.gateway.rpc import get_dispatcher
from agentos.gateway.session_streams import SessionStreamRegistry, get_session_streams
from agentos.gateway.terminal_events import (
    TerminalPublications,
    mark_aborted_done,
    runtime_terminal_frame,
)
from agentos.gateway.websocket import get_registry

TERMINALS = ("session.event.done", "session.event.error")


# ── exactly one terminal per run ─────────────────────────────────────────


async def test_a_normal_end_publishes_one_done_from_the_stream(tmp_path: Path) -> None:
    async with turn_rig(tmp_path, ScriptedRunner(["hi"], block=False)) as rig:
        task_id = await rig.enqueue()
        await rig.runtime.wait(task_id, timeout=3)

    terminals = [(n, p) for n, p, _ in rig.live.events if n in TERMINALS]
    assert len(terminals) == 1
    name, payload = terminals[0]
    assert name == "session.event.done"
    assert payload["state"] == "done"
    assert payload["task_id"] == payload["run_id"] == task_id
    assert payload["text"] == "hi"


async def test_cancel_ends_with_one_aborted_done_before_task_cancelled(tmp_path: Path) -> None:
    runner = ScriptedRunner(["partial ", "answer"])
    async with turn_rig(tmp_path, runner) as rig:
        task_id = await rig.enqueue()
        await asyncio.wait_for(runner.streamed.wait(), 3)
        await rig.runtime.cancel(session_key=rig.key, source="test", reason="test")
        await rig.runtime.wait(task_id, timeout=3)

    names = rig.live.names()
    terminals = [p for n, p, _ in rig.live.events if n in TERMINALS]
    assert len(terminals) == 1
    assert terminals[0]["state"] == "aborted"
    assert terminals[0]["reason"] == "aborted"
    assert terminals[0]["task_id"] == task_id
    assert terminals[0]["partial_text"] == "partial answer"
    # The stream frame comes first, after the status is persisted; then task.*.
    assert names.index("session.event.done") < names.index("task.cancelled")
    changed = [p for n, p, _ in rig.live.events if n == "sessions.changed"]
    assert changed[-1]["run_status"] == "cancelled"


async def test_a_second_terminal_from_the_stream_is_suppressed(tmp_path: Path) -> None:
    class _TwiceDone:
        async def run(self, message: str, session_key: str, **kwargs: Any):
            yield DoneEvent(text="one")
            yield DoneEvent(text="two")

    async with turn_rig(tmp_path, _TwiceDone()) as rig:
        task_id = await rig.enqueue()
        await rig.runtime.wait(task_id, timeout=3)

    terminals = [p for n, p, _ in rig.live.events if n in TERMINALS]
    assert [p["text"] for p in terminals] == ["one"]


async def test_a_task_that_never_started_owes_no_stream_terminal(tmp_path: Path) -> None:
    runner = ScriptedRunner(["x"])
    async with turn_rig(tmp_path, runner) as rig:
        first = await rig.enqueue()
        await asyncio.wait_for(runner.streamed.wait(), 3)
        queued = await rig.enqueue()
        await rig.runtime.cancel(task_id=queued, source="test", reason="test")
        await rig.runtime.wait(queued, timeout=3)
        runner.release.set()
        await rig.runtime.wait(first, timeout=3)

    terminals = [p for n, p, _ in rig.live.events if n in TERMINALS]
    # Only the run that streamed ends with a stream frame: a ``done`` for the
    # never-started task would end the live view of the run in front of it.
    assert [p["task_id"] for p in terminals] == [first]
    assert "task.cancelled" in rig.live.names()


def test_an_abort_hold_parks_the_stream_frame_for_the_runtime() -> None:
    pubs = TerminalPublications()
    assert pubs.hold("run-1", by="abort") is True
    assert pubs.try_publish("run-1", "session.event.done", {"text": "late"}) is False
    owed, parked, held_by = pubs.take("run-1")
    assert owed is True and held_by == "abort"
    assert parked == ("session.event.done", {"text": "late"})
    # Settled: nothing is owed twice.
    assert pubs.try_publish("run-1", "session.event.done", {}) is True
    assert pubs.take("run-1") == (False, None, None)


def test_a_published_run_cannot_be_held() -> None:
    pubs = TerminalPublications()
    assert pubs.try_publish("run-1", "session.event.done", {}) is True
    assert pubs.hold("run-1", by="abort") is False


@pytest.mark.parametrize(
    ("status", "parked", "expected_event", "expected_state"),
    [
        ("succeeded", None, "session.event.done", "done"),
        ("cancelled", None, "session.event.done", "aborted"),
        ("failed", None, "session.event.error", "error"),
        ("timeout", None, "session.event.error", "error"),
        # A run that finished on its own while an abort held it keeps its done.
        ("succeeded", ("session.event.done", {"text": "all"}), "session.event.done", "done"),
        # A cancelled run reads as a stop, even if the stream parked an error.
        ("cancelled", ("session.event.error", {"code": "x"}), "session.event.done", "aborted"),
    ],
)
def test_runtime_terminal_frame(
    status: str, parked: Any, expected_event: str, expected_state: str
) -> None:
    event, payload = runtime_terminal_frame(
        status, task_id="t1", terminal_reason="r", terminal_message="m", parked=parked
    )
    assert event == expected_event
    assert payload["state"] == expected_state
    assert payload["task_id"] == "t1"
    if parked and status == "succeeded":
        assert payload["text"] == "all"


def test_a_cooperatively_aborted_done_keeps_its_payload_when_held() -> None:
    # The turn honoured the abort and ended with an aborted done while
    # sessions.abort held the run: its text and usage are not thrown away.
    parked = mark_aborted_done(
        {
            "text": "partial answer",
            "usage": {"input_tokens": 12, "output_tokens": 3},
            "outcome": {"kind": "interrupted", "reason": "cancelled"},
        }
    )

    event, payload = runtime_terminal_frame(
        "cancelled",
        task_id="t1",
        terminal_reason="cancelled",
        parked=("session.event.done", parked),
    )

    assert event == "session.event.done"
    assert payload["state"] == "aborted"
    assert payload["text"] == "partial answer"
    assert payload["usage"] == {"input_tokens": 12, "output_tokens": 3}
    assert payload["task_id"] == "t1"


# ── replay to a late subscriber ──────────────────────────────────────────


async def test_late_subscriber_after_cancel_receives_the_terminal(tmp_path: Path) -> None:
    runner = ScriptedRunner(["half"])
    async with turn_rig(tmp_path, runner) as rig:
        task_id = await rig.enqueue()
        await asyncio.wait_for(runner.streamed.wait(), 3)
        cursor = get_session_streams().current_seq(rig.key)
        await rig.runtime.cancel(session_key=rig.key, source="test", reason="test")
        await rig.runtime.wait(task_id, timeout=3)

        late = RecordingConn(f"late-{uuid.uuid4().hex[:6]}")
        get_registry().register(late)  # type: ignore[arg-type]
        try:
            res = await get_dispatcher().dispatch(
                "1",
                "sessions.messages.subscribe",
                {"key": rig.key, "since_stream_seq": cursor},
                rig.ctx(late.conn_id),
            )
        finally:
            get_registry().unregister(late.conn_id)

    assert res.ok, res.error
    assert res.payload["run_status"] == "cancelled"
    assert res.payload["in_flight_run"] is None
    assert all(meta == {"replayed": True} for _, _, meta in late.events)
    replayed = {name: payload for name, payload, _ in late.events}
    assert replayed["session.event.done"]["state"] == "aborted"
    assert replayed["task.cancelled"]["task_id"] == task_id
    assert replayed["sessions.changed"]["run_status"] == "cancelled"


def test_task_and_session_frames_are_kept_for_replay() -> None:
    streams = SessionStreamRegistry(max_events_per_session=3)
    key = "agent:main:replay"
    streams.record_if_replayable(key, "task.running", {"task_id": "t1"})
    for i in range(10):
        streams.record_if_replayable(key, "session.event.text_delta", {"text": str(i)})
    streams.record_if_replayable(key, "task.cancelled", {"task_id": "t1"})
    streams.record_if_replayable(key, "sessions.changed", {"key": key, "run_status": "cancelled"})
    passthrough = streams.record_if_replayable(key, "presence", {"x": 1})

    names = [event.event_name for event in streams.replay(key, 0).events]
    # Lossy deltas give way first; the lifecycle frames survive the trim.
    assert names == ["task.running", "task.cancelled", "sessions.changed"]
    assert "stream_seq" not in passthrough


# ── in-flight snapshot ────────────────────────────────────────────────────


async def test_subscribe_carries_the_in_flight_run_while_it_streams(tmp_path: Path) -> None:
    runner = ScriptedRunner(["hello ", "world"])
    async with turn_rig(tmp_path, runner) as rig:
        task_id = await rig.enqueue()
        await asyncio.wait_for(runner.streamed.wait(), 3)
        res = await get_dispatcher().dispatch(
            "1", "sessions.messages.subscribe", {"key": rig.key}, rig.ctx(rig.live.conn_id)
        )
        runner.release.set()
        await rig.runtime.wait(task_id, timeout=3)
        after = await get_dispatcher().dispatch(
            "2", "sessions.messages.subscribe", {"key": rig.key}, rig.ctx(rig.live.conn_id)
        )

    snapshot = res.payload["in_flight_run"]
    assert snapshot["run_id"] == snapshot["task_id"] == task_id
    assert snapshot["status"] == "running"
    assert snapshot["text"] == "hello world"
    assert snapshot["through_stream_seq"] == res.payload["current_stream_seq"]
    assert isinstance(snapshot["started_at"], int)
    assert after.payload["in_flight_run"] is None


def test_snapshot_budget_keeps_identity_and_the_newest_text() -> None:
    streams = SessionStreamRegistry()
    key = "agent:main:budget"
    streams.record(key, "task.running", {"task_id": "t1"})
    for i in range(40):
        streams.record(
            key, "session.event.tool_use_start", {"tool_use_id": f"u{i}", "tool_name": "bash"}
        )
    streams.record(key, "session.event.text_delta", {"text": "a" * 5000 + "TAIL"})

    snapshot = streams.in_flight_snapshot(
        key, active_task={"task_id": "t1", "status": "running"}, budget_bytes=1500
    )

    assert snapshot is not None
    assert snapshot["run_id"] == "t1" and "started_at" in snapshot
    assert snapshot["text_truncated"] is True
    assert snapshot["text"].endswith("TAIL")
    # Recent progress beats text; the oldest events are what gets dropped.
    assert snapshot["events"][-1]["tool_use_id"] == "u39"
    assert len(snapshot["events"]) < 40
    assert len(json.dumps(snapshot, separators=(",", ":")).encode()) <= 1500


def test_snapshot_follows_the_task_ledger() -> None:
    streams = SessionStreamRegistry()
    key = "agent:main:ledger"
    streams.record(key, "task.running", {"task_id": "old"})
    streams.record(key, "session.event.text_delta", {"text": "stale"})

    assert streams.in_flight_snapshot(key, active_task=None) is None
    queued = streams.in_flight_snapshot(key, active_task={"task_id": "new", "status": "queued"})
    assert queued is not None
    assert queued["task_id"] == "new" and queued["status"] == "queued" and queued["text"] == ""


# ── sessions.abort ───────────────────────────────────────────────────────


async def test_abort_waits_for_the_terminal_and_names_the_run(tmp_path: Path) -> None:
    runner = ScriptedRunner(["work"])
    async with turn_rig(tmp_path, runner) as rig:
        task_id = await rig.enqueue()
        await asyncio.wait_for(runner.streamed.wait(), 3)
        res = await get_dispatcher().dispatch(
            "1", "sessions.abort", {"key": rig.key}, rig.ctx(rig.live.conn_id)
        )
        # Answered only once the terminal was persisted and published.
        record = await rig.runtime.status(task_id)

    assert res.ok, res.error
    assert res.payload == {"aborted": [task_id], "children_aborted": [], "key": rig.key}
    assert str(getattr(record.status, "value", record.status)) == "cancelled"
    done = [p for n, p, _ in rig.live.events if n == "session.event.done"]
    assert len(done) == 1 and done[0]["state"] == "aborted"
    assert done[0]["partial_text"] == "work"


async def test_abort_with_nothing_running_reports_no_runs(tmp_path: Path) -> None:
    async with turn_rig(tmp_path, ScriptedRunner([])) as rig:
        res = await get_dispatcher().dispatch(
            "1", "sessions.abort", {"key": rig.key}, rig.ctx(rig.live.conn_id)
        )
    assert res.ok
    assert res.payload["aborted"] == []


async def test_abort_cascades_to_child_runs(tmp_path: Path) -> None:
    runner = ScriptedRunner(["child work"])
    async with turn_rig(tmp_path, runner) as rig:
        child = f"agent:main:subagent:{uuid.uuid4().hex[:8]}"
        await rig.sessions.create(child, agent_id="main")
        child_task = await rig.runtime.enqueue(
            rig.envelope(child, parent_session_key=rig.key), "child", run_kind="subagent"
        )
        await asyncio.wait_for(runner.streamed.wait(), 3)
        res = await get_dispatcher().dispatch(
            "1", "sessions.abort", {"key": rig.key}, rig.ctx(rig.live.conn_id)
        )
        record = await rig.runtime.status(child_task.task_id)

    assert res.ok, res.error
    assert res.payload["children_aborted"] == [child]
    assert str(getattr(record.status, "value", record.status)) == "cancelled"
    details = record.details or {}
    assert details["cancellation"]["source"] == run_abort.PARENT_ABORT_SOURCE


async def test_an_incomplete_cascade_is_an_error_naming_the_survivors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Stubborn:
        """A child turn that ignores cancellation for a while."""

        def __init__(self) -> None:
            self.streamed = asyncio.Event()

        async def run(self, message: str, session_key: str, **kwargs: Any):
            yield TextDeltaEvent(text="busy")
            self.streamed.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await asyncio.sleep(1.0)
                raise

    monkeypatch.setattr(run_abort, "ABORT_WAIT_TIMEOUT_S", 0.2)
    runner = _Stubborn()
    async with turn_rig(tmp_path, runner) as rig:
        child = f"agent:main:subagent:{uuid.uuid4().hex[:8]}"
        await rig.sessions.create(child, agent_id="main")
        await rig.runtime.enqueue(
            rig.envelope(child, parent_session_key=rig.key), "child", run_kind="subagent"
        )
        await asyncio.wait_for(runner.streamed.wait(), 3)
        res = await get_dispatcher().dispatch(
            "1", "sessions.abort", {"key": rig.key}, rig.ctx(rig.live.conn_id)
        )

    assert res.ok is False
    assert res.error is not None
    assert res.error.code == "UNAVAILABLE"
    assert res.error.details["code"] == "ABORT_INCOMPLETE"
    assert res.error.details["still_running"] == [child]


async def test_child_cancel_reaches_clients_as_cancelled_run_status(tmp_path: Path) -> None:
    runner = ScriptedRunner(["child"])
    async with turn_rig(tmp_path, runner) as rig:
        child = f"agent:main:acp:{uuid.uuid4().hex[:8]}"
        await rig.sessions.create(child, agent_id="main")
        rig.subs.subscribe_messages(rig.live.conn_id, child)
        handle = await rig.runtime.enqueue(
            rig.envelope(child, parent_session_key=rig.key), "child", run_kind="subagent"
        )
        await asyncio.wait_for(runner.streamed.wait(), 3)
        await rig.runtime.cancel(session_key=child, source="test", reason="test")
        await rig.runtime.wait(handle.task_id, timeout=3)
        await wait_for(
            lambda: any(
                n == "sessions.changed" and p.get("key") == child and p.get("run_status")
                for n, p, _ in rig.live.events
            )
        )

    changed = [p for n, p, _ in rig.live.events if n == "sessions.changed" and p["key"] == child]
    assert changed[-1]["run_status"] == "cancelled"


async def test_a_finished_session_keeps_its_stream_for_its_next_turn(tmp_path: Path) -> None:
    """A child row is finished when its run ends; a follow-up turn continues.

    Finishing used to evict the stream counter and replay buffer, so the next
    turn restarted at ``stream_seq`` 1: a view that saw the first turn dropped
    the second one's frames as duplicates, and a late subscriber got nothing.
    """
    from agentos.session.manager import SessionManager
    from agentos.session.storage import SessionStorage

    store = SessionStorage(":memory:")
    await store.connect()
    try:
        manager = SessionManager(store, inject_time_prefix=False)
        key = f"agent:main:subagent:{uuid.uuid4().hex[:8]}"
        await manager.create(key, agent_id="main")
        streams = get_session_streams()
        streams.record(key, "task.running", {"task_id": "t1"})
        done = streams.record(key, "session.event.done", {"task_id": "t1", "state": "done"})

        await manager.finish(key)

        assert streams.current_seq(key) == done["stream_seq"]
        assert [e.event_name for e in streams.replay(key, 0).events] == [
            "task.running",
            "session.event.done",
        ]
        follow_up = streams.record(key, "task.running", {"task_id": "t2"})
        assert follow_up["stream_seq"] == done["stream_seq"] + 1

        await manager.delete(key)
        assert streams.current_seq(key) == 0
    finally:
        await store.close()


async def test_abort_withdraws_the_prompt_the_run_was_waiting_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no poll left, a prompt for a dead run used to stay on screen."""
    from agentos.application import approval_queue as queue_mod
    from agentos.application.approval_events import add_approval_listener

    queue = queue_mod.ApprovalQueue(db_path=str(tmp_path / "q.sqlite"))
    monkeypatch.setattr(queue_mod, "_queue", queue)
    resolved: list[dict[str, Any]] = []
    remove = add_approval_listener(
        lambda name, payload: resolved.append(payload) if name == "approval.resolved" else None
    )
    waiting = asyncio.Event()

    class _AsksFirst:
        async def run(self, message: str, session_key: str, **kwargs: Any):
            yield TextDeltaEvent(text="Need to run a command.")
            approval_id = queue.request("exec", {"toolName": "bash", "sessionKey": session_key})
            waiting.set()
            await queue.wait(approval_id, timeout=30)
            yield DoneEvent(text="never")

    try:
        async with turn_rig(tmp_path, _AsksFirst()) as rig:
            await rig.enqueue()
            await asyncio.wait_for(waiting.wait(), 3)
            res = await get_dispatcher().dispatch(
                "1", "sessions.abort", {"key": rig.key}, rig.ctx(rig.live.conn_id)
            )
            pending = queue.list_pending()
    finally:
        remove()
        queue.close()

    assert res.ok, res.error
    assert pending == []
    assert [(p["decision"], p["resolved_by"]) for p in resolved] == [("cancelled", "abort")]


async def test_a_send_after_the_abort_is_not_stopped_by_it(tmp_path: Path) -> None:
    """The abort runs off the connection's request loop (it waits up to 10 s).

    It captures what it stops in its first step; a turn queued while it waits
    (the user's next message) must survive it.
    """
    runner = ScriptedRunner(["first"])
    async with turn_rig(tmp_path, runner) as rig:
        first = await rig.enqueue()
        await asyncio.wait_for(runner.streamed.wait(), 3)
        abort = asyncio.create_task(
            get_dispatcher().dispatch(
                "1", "sessions.abort", {"key": rig.key}, rig.ctx(rig.live.conn_id)
            )
        )
        await asyncio.sleep(0)  # the abort's first step, as the WS loop allows it
        second = await rig.enqueue()
        res = await abort
        runner.release.set()
        record = await rig.runtime.wait(second, timeout=3)

    assert res.ok, res.error
    assert res.payload["aborted"] == [first]
    assert str(getattr(record.status, "value", record.status)) == "succeeded"
