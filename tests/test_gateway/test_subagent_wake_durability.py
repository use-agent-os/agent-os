"""Gaps in the parent-wake outbox found by the 2026-11-10 review.

* The wake is recorded right after the group-complete decision, before the
  announce's other side effects, and is still sent when one of them fails.
* A group the parent closed is recorded as ``awaiting``: a crash before the
  last child's announce no longer loses the wake (the boot reconcile sends it).
* A wake whose ``delivered`` write was lost is not enqueued twice.
* The worker drops an over-age row and takes over a hand-off stuck in flight
  without waiting for a failure report.
* A retried enqueue failure is not reported as a failed group; a dropped wake is.
* Rows go with their parent session and are pruned once old.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from agentos.gateway import subagent_announce
from agentos.gateway.background_completion import BackgroundCompletionManager
from agentos.gateway.routing import RouteEnvelope, SourceKind
from agentos.gateway.subagent_announce import (
    _tracker,
    announce_subagent_completion,
    close_subagent_spawn_group,
    reconcile_awaiting_wakes,
)
from agentos.gateway.subagent_wakes import (
    CLAIM_LEASE_S,
    IN_FLIGHT_STALE_S,
    MAX_PENDING_AGE_S,
    RETENTION_S,
    SubagentWakeOutbox,
    SubagentWakeWorker,
    group_key,
    set_wake_outbox,
    wake_idempotency_key,
)
from agentos.gateway.task_runtime import SubagentCompletionEvent, TaskRuntime
from agentos.session.models import AgentTaskStatus
from agentos.session.storage import SessionStorage
from tests.test_gateway.test_subagent_announce_group_wake import (
    PARENT,
    PARENT_TASK,
    _SessionManager,
    _SessionRow,
)

CHILD = "agent:worker:subagent:only"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += int(seconds * 1000)


class _Runtime:
    """Records sends; accepts the keyword arguments the real runtime takes."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    async def send(self, session_key: str, message: str, **kwargs: Any) -> Any:
        self.sent.append((session_key, message, kwargs))
        return SimpleNamespace(task_id=f"wake-{len(self.sent)}")


@pytest.fixture
async def storage() -> AsyncIterator[SessionStorage]:
    store = SessionStorage(":memory:")
    await store.connect()
    yield store
    await store.close()


@pytest.fixture(autouse=True)
def _clean() -> Any:
    _tracker.evict(PARENT)
    subagent_announce.set_background_completion_manager(None)
    yield
    set_wake_outbox(None)
    _tracker.evict(PARENT)
    subagent_announce.set_background_completion_manager(None)


def _install(storage: SessionStorage, clock: _Clock | None = None) -> SubagentWakeOutbox:
    outbox = SubagentWakeOutbox(storage, clock=clock or _Clock())
    set_wake_outbox(outbox)
    return outbox


def _manager(status: str) -> _SessionManager:
    task = SimpleNamespace(
        task_id="task-only",
        agent_id="worker",
        status=AgentTaskStatus.SUCCEEDED,
        run_kind="subagent",
        terminal_reason="completed",
        created_at=1,
        updated_at=2,
        finished_at=3,
    )
    return _SessionManager(
        [_SessionRow(CHILD, status=status)],
        tasks_by_session={CHILD: [task]},
        transcripts={CHILD: "the answer"},
    )


def _event() -> SubagentCompletionEvent:
    return SubagentCompletionEvent(
        parent_session_key=PARENT,
        child_session_key=CHILD,
        task_id="task-only",
        status=AgentTaskStatus.SUCCEEDED,
        terminal_reason="completed",
        agent_id="worker",
        parent_task_id=PARENT_TASK,
    )


async def _row(storage: SessionStorage) -> dict[str, Any] | None:
    return await storage.get_subagent_wake(group_key(PARENT, PARENT_TASK))


# ------------------------------------------------- claim before side effects


async def test_the_wake_is_recorded_and_sent_when_a_later_announce_step_fails(
    storage: SessionStorage,
) -> None:
    _install(storage)
    manager = _manager("running")
    runtime = _Runtime()
    _tracker.mark_closed(PARENT, PARENT_TASK)  # the parent yielded

    async def failing_append(*_: Any, **__: Any) -> None:
        raise RuntimeError("database is locked")

    manager.append_message = failing_append  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="locked"):
        await announce_subagent_completion(_event(), session_manager=manager, task_runtime=runtime)

    row = await _row(storage)
    assert row is not None and row["state"] == "delivered"
    assert len(runtime.sent) == 1
    assert runtime.sent[0][2]["idempotency_key"] == wake_idempotency_key(PARENT, PARENT_TASK)


async def test_the_wake_is_recorded_before_the_event_and_channel_push(
    storage: SessionStorage,
) -> None:
    _install(storage)
    manager = _manager("running")
    states: list[Any] = []

    async def emitter(session_key: str, name: str, payload: dict[str, Any]) -> None:
        row = await _row(storage)
        states.append(row and row["state"])

    _tracker.mark_closed(PARENT, PARENT_TASK)
    await announce_subagent_completion(
        _event(), session_manager=manager, event_emitter=emitter, task_runtime=_Runtime()
    )

    assert states == ["pending"]  # recorded before the event, sent after it
    row = await _row(storage)
    assert row is not None and row["state"] == "delivered"


async def test_a_terminal_listener_failure_is_logged() -> None:
    class _Records:
        def __init__(self) -> None:
            self.rows: dict[str, Any] = {}

        async def create_agent_task(self, record: Any) -> None:
            self.rows[record.task_id] = record

        async def get_agent_task(self, task_id: str) -> Any:
            return self.rows.get(task_id)

        async def list_agent_tasks(self, **_: Any) -> list[Any]:
            return list(self.rows.values())

        async def update_agent_task(self, task_id: str, **fields: Any) -> None:
            for key, value in fields.items():
                setattr(self.rows[task_id], key, value)

    async def handler(_: Any) -> None:
        return None

    async def listener(_: Any) -> None:
        raise RuntimeError("announce blew up before the claim")

    runtime = TaskRuntime(storage=_Records(), turn_handler=handler, terminal_listener=listener)
    envelope = RouteEnvelope(
        source_kind=SourceKind.SYSTEM,
        source_name="test",
        agent_id="worker",
        session_key=CHILD,
        metadata={"parent_session_key": PARENT, "parent_task_id": PARENT_TASK},
    )
    with structlog.testing.capture_logs() as logs:
        handle = await runtime.enqueue(envelope, "work", run_kind="subagent")
        await runtime.wait(handle.task_id, timeout=2)
        await asyncio.sleep(0)

    failures = [e for e in logs if e["event"] == "task_runtime.subagent_terminal_listener_failed"]
    assert failures and failures[0]["parent_session_key"] == PARENT


# ------------------------------------------------------------ boot reconcile


async def test_a_crash_before_the_last_announce_is_reconciled_at_boot(
    storage: SessionStorage,
) -> None:
    _install(storage)
    manager = _manager("running")
    # The parent yields while its child runs: the group is recorded awaiting.
    await close_subagent_spawn_group(
        PARENT, PARENT_TASK, session_manager=manager, task_runtime=_Runtime()
    )
    row = await _row(storage)
    assert row is not None and row["state"] == "awaiting"

    # The child ends and the process dies before (or while) announcing it.
    manager.rows[0].status = "done"
    _tracker.evict(PARENT)
    _install(storage)
    runtime = _Runtime()

    assert await reconcile_awaiting_wakes(session_manager=manager, task_runtime=runtime) == 1
    assert len(runtime.sent) == 1 and "the answer" in runtime.sent[0][1]
    row = await _row(storage)
    assert row is not None and row["state"] == "delivered"
    # A second boot does nothing more.
    _tracker.evict(PARENT)
    assert await reconcile_awaiting_wakes(session_manager=manager, task_runtime=runtime) == 0
    assert len(runtime.sent) == 1


async def test_reconcile_leaves_a_group_with_running_children(storage: SessionStorage) -> None:
    outbox = _install(storage)
    await outbox.await_group(parent_session_key=PARENT, parent_task_id=PARENT_TASK)
    runtime = _Runtime()

    assert (
        await reconcile_awaiting_wakes(session_manager=_manager("running"), task_runtime=runtime)
        == 0
    )
    assert runtime.sent == []
    row = await _row(storage)
    assert row is not None and row["state"] == "awaiting"


async def test_reconcile_skips_a_parent_being_recovered(storage: SessionStorage) -> None:
    outbox = _install(storage)
    await outbox.await_group(parent_session_key=PARENT, parent_task_id=PARENT_TASK)
    runtime = _Runtime()
    recovery = SimpleNamespace(is_recovering=lambda key: key == PARENT)

    await reconcile_awaiting_wakes(
        session_manager=_manager("done"), task_runtime=runtime, turn_recovery=recovery
    )

    assert runtime.sent == []
    row = await _row(storage)
    assert row is not None and row["state"] == "dropped" and row["reason"] == "parent_recovering"


async def test_the_last_child_turns_an_awaiting_row_into_the_wake(
    storage: SessionStorage,
) -> None:
    _install(storage)
    manager = _manager("running")
    runtime = _Runtime()
    await close_subagent_spawn_group(
        PARENT, PARENT_TASK, session_manager=manager, task_runtime=runtime
    )

    await announce_subagent_completion(_event(), session_manager=manager, task_runtime=runtime)

    assert len(runtime.sent) == 1
    row = await _row(storage)
    assert row is not None and row["state"] == "delivered" and row["message"]


# --------------------------------------------------------- duplicate wakes


async def test_a_lost_delivered_write_does_not_wake_the_parent_twice(
    storage: SessionStorage,
) -> None:
    clock = _Clock()
    outbox = _install(storage, clock)
    turns: list[str] = []

    class _Records:
        def __init__(self) -> None:
            self.rows: dict[str, Any] = {}

        async def create_agent_task(self, record: Any) -> None:
            self.rows[record.task_id] = record

        async def get_agent_task(self, task_id: str) -> Any:
            return self.rows.get(task_id)

        async def list_agent_tasks(self, **_: Any) -> list[Any]:
            return list(self.rows.values())

        async def update_agent_task(self, task_id: str, **fields: Any) -> None:
            for key, value in fields.items():
                setattr(self.rows[task_id], key, value)

    async def handler(run: Any) -> None:
        turns.append(run.message)

    runtime = TaskRuntime(storage=_Records(), turn_handler=handler)
    real_update = storage.update_subagent_wake
    fail_once = {"left": 1}

    async def flaky_update(key: str, **fields: Any) -> None:
        if fields.get("state") == "delivered" and fail_once["left"]:
            fail_once["left"] -= 1
            raise RuntimeError("database is locked")
        await real_update(key, **fields)

    storage.update_subagent_wake = flaky_update  # type: ignore[method-assign]
    _tracker.mark_closed(PARENT, PARENT_TASK)
    await announce_subagent_completion(
        _event(), session_manager=_manager("running"), task_runtime=runtime
    )
    row = await _row(storage)
    assert row is not None and row["state"] == "pending"  # the write was lost

    async def deliver(pending: dict[str, Any], report: Any) -> None:
        await subagent_announce.deliver_pending_wake(pending, report, task_runtime=runtime)

    clock.advance(CLAIM_LEASE_S)
    await SubagentWakeWorker(outbox, deliver).run_once()
    for _ in range(20):
        await asyncio.sleep(0.01)

    assert len(turns) == 1
    row = await _row(storage)
    assert row is not None and row["state"] == "delivered"


async def test_send_with_a_known_idempotency_key_returns_the_first_task() -> None:
    class _Records:
        def __init__(self) -> None:
            self.rows: dict[str, Any] = {}

        async def create_agent_task(self, record: Any) -> None:
            self.rows[record.task_id] = record

        async def get_agent_task(self, task_id: str) -> Any:
            return self.rows.get(task_id)

        async def list_agent_tasks(self, **_: Any) -> list[Any]:
            return list(self.rows.values())

        async def update_agent_task(self, task_id: str, **fields: Any) -> None:
            for key, value in fields.items():
                setattr(self.rows[task_id], key, value)

    release = asyncio.Event()

    async def handler(_: Any) -> None:
        await release.wait()

    records = _Records()
    runtime = TaskRuntime(storage=records, turn_handler=handler)
    first = await runtime.send(PARENT, "wake", idempotency_key="k1")
    queued_again = await runtime.send(PARENT, "wake", idempotency_key="k1")
    assert queued_again.task_id == first.task_id and queued_again.deduplicated
    release.set()
    await runtime.wait(first.task_id, timeout=2)
    # Gone from memory, still found in the ledger.
    again = await runtime.send(PARENT, "wake", idempotency_key="k1")
    assert again.task_id == first.task_id and again.deduplicated
    other = await runtime.send(PARENT, "wake", idempotency_key="k2")
    assert other.task_id != first.task_id and not other.deduplicated
    await runtime.wait(other.task_id, timeout=2)
    assert len(records.rows) == 2


# ---------------------------------------------------------------- worker


async def test_the_worker_drops_an_over_age_row_without_a_failure_report(
    storage: SessionStorage,
) -> None:
    clock = _Clock()
    outbox = _install(storage, clock)
    dropped: list[tuple[str, str | None, str]] = []

    async def on_dropped(parent: str, task_id: str | None, reason: str) -> None:
        dropped.append((parent, task_id, reason))

    outbox.set_drop_listener(on_dropped)
    await outbox.claim(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        message="m",
        provenance={},
        payloads=[],
    )
    calls: list[str] = []

    async def deliver(pending: dict[str, Any], report: Any) -> None:
        calls.append(pending["group_key"])  # hands off, never reports

    # The hand-off hangs (the parent's task never ends); nothing reports.
    clock.advance(MAX_PENDING_AGE_S + 1)
    await SubagentWakeWorker(outbox, deliver).run_once()

    assert calls == []
    row = await _row(storage)
    assert row is not None and row["state"] == "dropped" and row["reason"] == "delivery_timeout"
    assert dropped == [(PARENT, PARENT_TASK, "delivery_timeout")]


async def test_the_worker_takes_over_a_hand_off_stuck_in_flight(
    storage: SessionStorage,
) -> None:
    clock = _Clock()
    outbox = _install(storage, clock)
    await outbox.claim(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        message="m",
        provenance={},
        payloads=[],
    )
    stuck_report = outbox.reporter(PARENT, PARENT_TASK)
    calls: list[str] = []

    async def deliver(pending: dict[str, Any], report: Any) -> None:
        calls.append(pending["group_key"])
        await report(None)

    worker = SubagentWakeWorker(outbox, deliver)
    clock.advance(CLAIM_LEASE_S + 1)
    assert await worker.run_once() == 0  # in flight: not ours yet
    clock.advance(IN_FLIGHT_STALE_S)
    assert await worker.run_once() == 1
    row = await _row(storage)
    assert row is not None and row["state"] == "delivered"
    # The stuck attempt reporting late changes nothing.
    await stuck_report(RuntimeError("late"))
    row = await _row(storage)
    assert row is not None and row["state"] == "delivered"


# ------------------------------------------------------------- group events


async def test_a_retried_enqueue_failure_is_not_a_failed_group() -> None:
    events: list[str] = []

    async def emitter(session_key: str, name: str, payload: dict[str, Any]) -> None:
        events.append(name.rsplit(".", 1)[-1])

    manager = BackgroundCompletionManager(session_manager=SimpleNamespace(), event_emitter=emitter)

    class _Refusing:
        async def send(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("not admitting")

    reports: list[BaseException | None] = []

    async def on_result(error: BaseException | None) -> None:
        reports.append(error)

    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        payloads=[],
        task_runtime=_Refusing(),
        message="m",
        provenance={},
        on_enqueue_result=on_result,
    )
    await manager.drain(timeout=2)

    assert len(reports) == 1 and isinstance(reports[0], RuntimeError)
    assert "failed" not in events
    await manager.emit_wake_dropped(PARENT, PARENT_TASK, "delivery_timeout")
    assert events[-1] == "failed"
    await manager.close(timeout=1)


async def test_without_an_outbox_an_enqueue_failure_is_still_a_failed_group() -> None:
    events: list[str] = []

    async def emitter(session_key: str, name: str, payload: dict[str, Any]) -> None:
        events.append(name.rsplit(".", 1)[-1])

    manager = BackgroundCompletionManager(session_manager=SimpleNamespace(), event_emitter=emitter)

    class _Refusing:
        async def send(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("not admitting")

    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        payloads=[],
        task_runtime=_Refusing(),
        message="m",
        provenance={},
    )
    await manager.drain(timeout=2)
    assert events == ["failed"]
    await manager.close(timeout=1)


# ------------------------------------------------------------ row lifetime


async def test_prune_deletes_old_finished_rows_only(storage: SessionStorage) -> None:
    clock = _Clock()
    outbox = _install(storage, clock)
    for name, state in (
        ("old-done", "delivered"),
        ("old-drop", "dropped"),
        ("old-wait", "awaiting"),
    ):
        await storage.record_subagent_wake(
            group_key=name,
            parent_session_key=PARENT,
            parent_task_id=name,
            message="",
            provenance={},
            payloads=[],
            now_ms=clock.now,
            state=state,
        )
    await storage.record_subagent_wake(
        group_key="old-pending",
        parent_session_key=PARENT,
        parent_task_id="p",
        message="m",
        provenance={},
        payloads=[],
        now_ms=clock.now,
    )
    clock.advance(RETENTION_S + 1)
    await storage.record_subagent_wake(
        group_key="new-done",
        parent_session_key=PARENT,
        parent_task_id="n",
        message="",
        provenance={},
        payloads=[],
        now_ms=clock.now,
        state="delivered",
    )

    assert await outbox.prune() == 3
    remaining = {
        key
        for key in ("old-done", "old-drop", "old-wait", "old-pending", "new-done")
        if await storage.get_subagent_wake(key) is not None
    }
    assert remaining == {"old-pending", "new-done"}
