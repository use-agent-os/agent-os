"""Per-parent subagent lanes and the nested pool (``subagent-lifecycle`` spec).

* At most ``max_concurrent_children`` children of one parent run at once; the
  rest wait in spawn order instead of being rejected.
* Another parent's children are not held back by that limit.
* A parent run that holds a slot (for example waiting on a child inline with
  ``sessions_yield(session_key=...)``) never starves its own children: they run
  in the nested pool, even when every shared slot is taken.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from agentos.gateway.routing import RouteEnvelope, SourceKind
from agentos.gateway.task_runtime import TaskRuntime
from agentos.session.models import AgentTaskRecord


@dataclass
class _Storage:
    records: dict

    async def create_agent_task(self, record: AgentTaskRecord) -> None:
        self.records[record.task_id] = record

    async def get_agent_task(self, task_id: str) -> Any:
        return self.records.get(task_id)

    async def list_agent_tasks(self, **_: Any) -> list[Any]:
        return list(self.records.values())

    async def update_agent_task(self, task_id: str, **fields: Any) -> None:
        record = self.records.get(task_id)
        if record is not None:
            for key, value in fields.items():
                setattr(record, key, value)


def _child(session_key: str, parent: str) -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.SYSTEM,
        source_name="test",
        agent_id="worker",
        session_key=session_key,
        metadata={"parent_session_key": parent, "parent_task_id": "t-" + parent},
    )


def _top(session_key: str) -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.SYSTEM, source_name="test", agent_id="main", session_key=session_key
    )


class _Gate:
    """A turn handler whose runs block until released, recording start order."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.release: dict[str, asyncio.Event] = {}
        self.changed = asyncio.Event()

    async def __call__(self, run: Any) -> None:
        key = run.session_key
        event = self.release.setdefault(key, asyncio.Event())
        self.started.append(key)
        self.changed.set()
        await event.wait()

    def open(self, key: str) -> None:
        self.release.setdefault(key, asyncio.Event()).set()

    async def wait_started(self, count: int, timeout: float = 3.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(self.started) < count:
            assert loop.time() < deadline, f"only {self.started} started"
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=0.2)
            except TimeoutError:
                pass


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0.01)


async def test_children_beyond_the_parent_limit_wait_in_spawn_order() -> None:
    gate = _Gate()
    runtime = TaskRuntime(
        storage=_Storage({}), turn_handler=gate, max_concurrency=8, max_concurrent_children=2
    )
    parent = "agent:main:webchat:p1"
    children = [f"agent:worker:subagent:c{i}" for i in range(5)]
    handles = [
        await runtime.enqueue(_child(key, parent), "task", run_kind="subagent") for key in children
    ]

    await gate.wait_started(2)
    await _settle()
    assert gate.started == children[:2]
    assert runtime._parent_in_flight[parent] == 2

    for key in children:
        gate.open(key)
    for handle in handles:
        await runtime.wait(handle.task_id, timeout=3.0)

    assert gate.started == children
    assert runtime._parent_in_flight == {}


async def test_another_parents_children_are_not_held_back() -> None:
    gate = _Gate()
    runtime = TaskRuntime(
        storage=_Storage({}), turn_handler=gate, max_concurrency=8, max_concurrent_children=1
    )
    await runtime.enqueue(
        _child("agent:worker:subagent:a1", "agent:main:webchat:pa"), "t", run_kind="subagent"
    )
    await runtime.enqueue(
        _child("agent:worker:subagent:a2", "agent:main:webchat:pa"), "t", run_kind="subagent"
    )
    await runtime.enqueue(
        _child("agent:worker:subagent:b1", "agent:main:webchat:pb"), "t", run_kind="subagent"
    )

    await gate.wait_started(2)
    await _settle()

    assert sorted(gate.started) == ["agent:worker:subagent:a1", "agent:worker:subagent:b1"]
    for key in list(gate.release) + ["agent:worker:subagent:a2"]:
        gate.open(key)


async def test_a_parent_waiting_inline_never_starves_its_children() -> None:
    gate = _Gate()
    # Two shared slots, both reserved away from subagents by default rules:
    # before the nested pool, two parents waiting on their children inline
    # deadlocked (4 - 2 in flight is not > 2 reserved, here 2 - 2 = 0).
    runtime = TaskRuntime(
        storage=_Storage({}),
        turn_handler=gate,
        max_concurrency=2,
        subagent_reserved_slots=1,
        max_concurrent_children=2,
    )
    parents = ["agent:main:webchat:x", "agent:main:webchat:y"]
    for parent in parents:
        await runtime.enqueue(_top(parent), "spawn and wait", run_kind="default")
    await gate.wait_started(2)
    assert runtime._global_in_flight == 2  # every shared slot is taken

    kids = []
    for parent in parents:
        key = parent.replace("webchat", "subagent")
        kids.append(key)
        await runtime.enqueue(_child(key, parent), "work", run_kind="subagent")

    await gate.wait_started(4)
    assert set(gate.started[2:]) == set(kids)
    assert runtime._nested_in_flight == 2
    assert runtime.active_turns == 4

    for key in list(gate.release):
        gate.open(key)
    await _settle()
    await _settle()
    assert runtime._nested_in_flight == 0
    assert runtime.active_turns == 0


async def test_resolver_overrides_the_default_per_parent() -> None:
    gate = _Gate()

    async def resolver(parent: str) -> int | None:
        return 1 if parent.endswith(":strict") else None

    runtime = TaskRuntime(
        storage=_Storage({}),
        turn_handler=gate,
        max_concurrency=8,
        max_concurrent_children=3,
        children_limit_resolver=resolver,
    )
    for i in range(3):
        await runtime.enqueue(
            _child(f"agent:worker:subagent:s{i}", "agent:main:webchat:strict"),
            "t",
            run_kind="subagent",
        )
    await gate.wait_started(1)
    await _settle()

    assert gate.started == ["agent:worker:subagent:s0"]
    for i in range(3):
        gate.open(f"agent:worker:subagent:s{i}")


def test_invalid_limit_is_rejected() -> None:
    async def handler(_: Any) -> None:
        return None

    with pytest.raises(ValueError):
        TaskRuntime(storage=_Storage({}), turn_handler=handler, max_concurrent_children=0)


async def test_a_second_cancel_does_not_leave_a_dead_head_in_the_parent_queue() -> None:
    gate = _Gate()
    runtime = TaskRuntime(
        storage=_Storage({}), turn_handler=gate, max_concurrency=8, max_concurrent_children=1
    )
    parent = "agent:main:webchat:twice"
    first, waiting, later = (f"agent:worker:subagent:t{i}" for i in range(3))
    await runtime.enqueue(_child(first, parent), "t", run_kind="subagent")
    await gate.wait_started(1)
    queued = await runtime.enqueue(_child(waiting, parent), "t", run_kind="subagent")
    await runtime.enqueue(_child(later, parent), "t", run_kind="subagent")
    await _settle()
    assert list(runtime._parent_queue[parent])[0] == queued.task_id

    # Cancel the head while it waits, then cancel it again while it waits for
    # the condition lock on its way out of the queue (the lock is contended).
    record = runtime._tasks[queued.task_id]
    assert record.asyncio_task is not None
    cond = runtime._ensure_fair_cond()
    await cond.acquire()
    record.asyncio_task.cancel()
    await asyncio.sleep(0)  # it blocks re-acquiring the lock inside wait()
    cond.release()
    await cond.acquire()  # queue up behind it: its cleanup now has to wait
    record.asyncio_task.cancel()
    cond.release()
    await _settle()

    assert queued.task_id not in runtime._parent_queue.get(parent, ())
    gate.open(first)
    await gate.wait_started(2)
    assert gate.started == [first, later]
    gate.open(later)
