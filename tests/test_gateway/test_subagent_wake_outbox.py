"""Parent wakes go through a durable outbox (``subagent-lifecycle`` spec).

A wake is recorded before it is sent, becomes ``delivered`` only once the task
runtime accepted it, is retried with backoff when it was not, survives a
restart, and is never delivered twice for the same spawn group.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.gateway import subagent_announce
from agentos.gateway.subagent_announce import _send_parent_wake, _tracker
from agentos.gateway.subagent_wakes import (
    BACKOFF_S,
    CLAIM_LEASE_S,
    MAX_PENDING_AGE_S,
    SubagentWakeOutbox,
    SubagentWakeWorker,
    group_key,
    set_wake_outbox,
)
from agentos.session.storage import SessionStorage

PARENT = "agent:main:webchat:outbox-parent"
TASK = "task-outbox-parent"
PAYLOADS = [
    {
        "type": "subagent_completion",
        "child_session_key": "agent:main:subagent:c1",
        "task_id": "task-c1",
        "status": "succeeded",
        "terminal_reason": "done",
        "parent_task_id": TASK,
        "result": "child result",
    }
]


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += int(seconds * 1000)


class _Runtime:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.sent: list[tuple[str, str]] = []

    async def send(self, session_key: str, message: str, **kwargs: Any) -> Any:
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("task runtime is draining")
        self.sent.append((session_key, message))
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


def _install(storage: SessionStorage, clock: _Clock) -> SubagentWakeOutbox:
    outbox = SubagentWakeOutbox(storage, clock=clock)
    set_wake_outbox(outbox)
    return outbox


async def _row(storage: SessionStorage) -> dict[str, Any]:
    row = await storage.get_subagent_wake(group_key(PARENT, TASK))
    assert row is not None
    return row


# ----------------------------------------------------------------- storage


async def test_storage_records_once_and_lists_due_rows(storage: SessionStorage) -> None:
    created, row = await storage.record_subagent_wake(
        group_key="g1",
        parent_session_key=PARENT,
        parent_task_id=TASK,
        message="m",
        provenance={"k": 1},
        payloads=PAYLOADS,
        now_ms=100,
        next_attempt_at=200,
    )
    again, _ = await storage.record_subagent_wake(
        group_key="g1",
        parent_session_key=PARENT,
        parent_task_id=TASK,
        message="other",
        provenance={},
        payloads=[],
        now_ms=150,
    )

    assert created is True and again is False
    assert row["state"] == "pending" and row["provenance"] == {"k": 1}
    assert row["payloads"] == PAYLOADS
    assert await storage.list_due_subagent_wakes(199) == []
    assert [r["group_key"] for r in await storage.list_due_subagent_wakes(200)] == ["g1"]
    await storage.update_subagent_wake("g1", state="delivered", updated_at=300)
    assert await storage.list_due_subagent_wakes(10_000) == []
    with pytest.raises(ValueError):
        await storage.update_subagent_wake("g1", message="nope")


# ----------------------------------------------------------------- delivery


async def test_wake_is_delivered_and_recorded(storage: SessionStorage) -> None:
    _install(storage, _Clock())
    runtime = _Runtime()

    await _send_parent_wake(PARENT, TASK, PAYLOADS, task_runtime=runtime)

    assert len(runtime.sent) == 1
    assert (await _row(storage))["state"] == "delivered"


async def test_a_delivered_group_is_not_woken_again_after_a_restart(
    storage: SessionStorage,
) -> None:
    _install(storage, _Clock())
    runtime = _Runtime()
    await _send_parent_wake(PARENT, TASK, PAYLOADS, task_runtime=runtime)

    _tracker.evict(PARENT)  # a new process: nothing remembered in memory
    _install(storage, _Clock())
    await _send_parent_wake(PARENT, TASK, PAYLOADS, task_runtime=runtime)

    assert len(runtime.sent) == 1


async def test_a_transient_failure_is_retried_by_the_worker(storage: SessionStorage) -> None:
    clock = _Clock()
    outbox = _install(storage, clock)
    runtime = _Runtime(failures=1)

    await _send_parent_wake(PARENT, TASK, PAYLOADS, task_runtime=runtime)

    row = await _row(storage)
    assert runtime.sent == []
    assert row["state"] == "pending" and row["attempts"] == 1
    assert "draining" in row["last_error"]

    async def deliver(pending: dict[str, Any]) -> None:
        await subagent_announce.deliver_pending_wake(pending, task_runtime=runtime)

    worker = SubagentWakeWorker(outbox, deliver)
    assert await worker.run_once() == 0  # not due yet: backoff
    clock.advance(BACKOFF_S[0])
    assert await worker.run_once() == 1

    assert len(runtime.sent) == 1
    assert (await _row(storage))["state"] == "delivered"


async def test_backoff_grows_and_an_old_wake_is_dropped(storage: SessionStorage) -> None:
    clock = _Clock()
    outbox = _install(storage, clock)
    await outbox.claim(
        parent_session_key=PARENT, parent_task_id=TASK, message="m", provenance={}, payloads=[]
    )

    delays = []
    for _ in range(5):
        before = clock.now
        await outbox.failed(PARENT, TASK, "boom")
        delays.append(((await _row(storage))["next_attempt_at"] - before) / 1000)
        clock.advance(1)
    assert delays == [BACKOFF_S[0], BACKOFF_S[1], BACKOFF_S[2], BACKOFF_S[3], BACKOFF_S[3]]

    clock.advance(MAX_PENDING_AGE_S)
    await outbox.failed(PARENT, TASK, "still down")
    row = await _row(storage)
    assert row["state"] == "dropped" and row["reason"] == "delivery_timeout"


async def test_a_wake_cut_off_by_a_restart_is_sent_by_the_next_process(
    storage: SessionStorage,
) -> None:
    clock = _Clock()
    first = _install(storage, clock)
    # The first process recorded the wake and died before sending it.
    await first.claim(
        parent_session_key=PARENT,
        parent_task_id=TASK,
        message="wake up",
        provenance={"kind": "internal_system"},
        payloads=PAYLOADS,
    )

    second = _install(storage, clock)  # new process: empty in-flight set
    runtime = _Runtime()

    async def deliver(pending: dict[str, Any]) -> None:
        await subagent_announce.deliver_pending_wake(pending, task_runtime=runtime)

    worker = SubagentWakeWorker(second, deliver)
    assert await worker.run_once() == 0  # the first sender's lease is still running
    clock.advance(CLAIM_LEASE_S)
    assert await worker.run_once() == 1
    assert runtime.sent == [(PARENT, "wake up")]


async def test_the_worker_does_not_resend_a_wake_this_process_is_sending(
    storage: SessionStorage,
) -> None:
    clock = _Clock()
    outbox = _install(storage, clock)
    await outbox.claim(
        parent_session_key=PARENT, parent_task_id=TASK, message="m", provenance={}, payloads=[]
    )
    clock.advance(CLAIM_LEASE_S + 60)
    calls: list[str] = []

    async def deliver(pending: dict[str, Any]) -> None:
        calls.append(pending["group_key"])

    assert await SubagentWakeWorker(outbox, deliver).run_once() == 0
    assert calls == []


async def test_completion_manager_path_waits_for_the_runtime_to_accept(
    storage: SessionStorage,
) -> None:
    _install(storage, _Clock())

    class _Manager:
        def __init__(self, error: BaseException | None) -> None:
            self.error = error
            self.calls = 0

        async def send_parent_wake(self, *, on_enqueue_result: Any = None, **kwargs: Any) -> None:
            self.calls += 1
            await on_enqueue_result(self.error)

    failing = _Manager(RuntimeError("enqueue refused"))
    await _send_parent_wake(
        PARENT, TASK, PAYLOADS, task_runtime=_Runtime(), completion_manager=failing
    )
    row = await _row(storage)
    assert row["state"] == "pending" and "enqueue refused" in row["last_error"]

    ok = _Manager(None)
    subagent_announce.set_background_completion_manager(ok)
    await subagent_announce.deliver_pending_wake(row, task_runtime=_Runtime())
    assert ok.calls == 1


async def test_an_aborted_parent_group_is_dropped_and_never_woken(
    storage: SessionStorage,
) -> None:
    outbox = _install(storage, _Clock())
    await outbox.drop(parent_session_key=PARENT, parent_task_id=TASK, reason="parent_aborted")
    runtime = _Runtime()

    await _send_parent_wake(PARENT, TASK, PAYLOADS, task_runtime=runtime)

    assert runtime.sent == []
    row = await _row(storage)
    assert row["state"] == "dropped" and row["reason"] == "parent_aborted"


async def test_without_an_outbox_the_previous_behaviour_is_kept() -> None:
    set_wake_outbox(None)
    runtime = _Runtime(failures=1)

    with pytest.raises(RuntimeError):
        await _send_parent_wake(PARENT, TASK, PAYLOADS, task_runtime=runtime)
