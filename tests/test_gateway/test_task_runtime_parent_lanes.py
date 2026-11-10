"""Per-parent subagent lanes and the nested pool (``subagent-lifecycle`` spec).

* At most ``max_concurrent_children`` children of one parent run at once; the
  rest wait in spawn order instead of being rejected.
* Another parent's children are not held back by that limit.
* A parent run that holds a slot (for example waiting on a child inline with
  ``sessions_yield(session_key=...)``) never starves its own children: they run
  in the nested pool, even when every shared slot is taken.
* The nested pool is bounded (``max_nested_concurrency``) and only children
  use it; a child keeps the slot it got until it ends, and running turns never
  exceed ``max_concurrency + max_nested_concurrency``.
* ``subagent_reserved_slots`` holds in both directions: children never take
  the reserved shared slots (a user's own turn starts beside them), and
  children waiting for a slot use free nested slots instead of queueing for
  the one shared slot left to them.
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


class _PeakGate(_Gate):
    """A ``_Gate`` that also records the most turns it saw running at once."""

    def __init__(self) -> None:
        super().__init__()
        self.running = 0
        self.peak = 0

    async def __call__(self, run: Any) -> None:
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await super().__call__(run)
        finally:
            self.running -= 1


async def _drain(runtime: TaskRuntime, gate: _Gate, keys: list[str]) -> None:
    for _ in range(40):
        for key in keys:
            gate.open(key)
        await asyncio.sleep(0.02)
        if not runtime._tasks:
            break
    assert runtime._tasks == {}
    assert runtime.active_turns == 0
    assert runtime._nested_in_flight == 0


async def test_a_nested_child_keeps_its_slot_when_the_parent_turn_ends() -> None:
    gate = _Gate()
    runtime = TaskRuntime(storage=_Storage({}), turn_handler=gate, max_concurrency=1)
    parent = "agent:main:webchat:spawner"
    kid = "agent:worker:subagent:k0"
    await runtime.enqueue(_top(parent), "spawn then yield", run_kind="default")
    await gate.wait_started(1)
    await runtime.enqueue(_child(kid, parent), "work", run_kind="subagent")
    await gate.wait_started(2)
    assert runtime._nested_in_flight == 1
    assert runtime._global_in_flight == 1

    # The parent ends its turn (spawn-then-``sessions_yield``); the child runs
    # on in its nested slot.
    gate.open(parent)
    await _settle()
    assert runtime._nested_in_flight == 1
    assert runtime._global_in_flight == 0

    # So the shared slot is free for a new top-level turn at once.
    other = "agent:main:webchat:other"
    await runtime.enqueue(_top(other), "hello", run_kind="default")
    await gate.wait_started(3)
    assert gate.started[-1] == other
    assert runtime.active_turns == 2  # max_concurrency + max_nested_concurrency
    await _drain(runtime, gate, [kid, other])


async def test_a_users_turn_does_not_wait_behind_children_of_ended_turns() -> None:
    """The tester's scenario: ``max_concurrency=2`` with the default reserve
    (2, clamped to 1). Two users' turns spawn three children each and end;
    a third user's message must start at once, and the waiting children must
    use the free nested slots rather than run one at a time."""
    gate = _PeakGate()
    runtime = TaskRuntime(
        storage=_Storage({}),
        turn_handler=gate,
        max_concurrency=2,
        subagent_reserved_slots=2,
        max_concurrent_children=None,
    )
    parents = ["agent:main:webchat:u1", "agent:main:webchat:u2"]
    kids: list[str] = []
    for parent in parents:
        await runtime.enqueue(_top(parent), "spawn three", run_kind="default")
    await gate.wait_started(2)
    for parent in parents:
        for i in range(3):
            key = parent.replace("webchat", "subagent") + f"c{i}"
            kids.append(key)
            await runtime.enqueue(_child(key, parent), "work", run_kind="subagent")
    await gate.wait_started(4)
    await _settle()
    assert runtime._nested_in_flight == 2  # both shared slots are the parents'

    # Both parents end their turns; their children are still running or queued.
    for parent in parents:
        gate.open(parent)
    await gate.wait_started(5)
    await _settle()
    # Two nested children run on, and one more child took the one shared slot
    # the reserve leaves to children. The reserved slot stays free.
    assert runtime._nested_in_flight == 2
    assert runtime._global_in_flight == 1
    assert len(gate.started) == 5

    # A third user's own message starts at once, beside the children.
    third = "agent:main:webchat:u3"
    await runtime.enqueue(_top(third), "hello", run_kind="default")
    await gate.wait_started(6, timeout=1.0)
    assert gate.started[-1] == third

    # Children finishing hand their slots to the queued ones: more than one
    # child runs at a time, never more turns than both pools hold.
    running_kids = [key for key in gate.started if key in kids]
    for key in running_kids:
        gate.open(key)
    await gate.wait_started(8, timeout=2.0)
    await _settle()
    # Two queued children took the nested slots; the last one waits, since
    # the only free shared slot is the reserved one while u3 runs.
    assert len(gate.started) == 8
    assert runtime._nested_in_flight == 2
    assert runtime.active_turns == 3  # u3 + two nested children
    await _drain(runtime, gate, [*kids, third])
    assert sorted(gate.started) == sorted([*parents, *kids, third])
    assert gate.peak <= 4  # max_concurrency + max_nested_concurrency


async def test_spawn_then_yield_parents_cannot_pile_up_children() -> None:
    # The review's scenario: four parents each spawn two children and end their
    # turn. Before, all eight children ran beside ``max_concurrency=1``.
    gate = _PeakGate()
    runtime = TaskRuntime(
        storage=_Storage({}), turn_handler=gate, max_concurrency=1, max_concurrent_children=2
    )
    keys: list[str] = []
    for n in range(4):
        parent = f"agent:main:webchat:p{n}"
        keys.append(parent)
        await runtime.enqueue(_top(parent), "spawn", run_kind="default")
        await _settle()
        for i in range(2):
            key = f"agent:worker:subagent:p{n}c{i}"
            keys.append(key)
            await runtime.enqueue(_child(key, parent), "t", run_kind="subagent")
        await _settle()
        gate.open(parent)
        await _settle()
        assert runtime.active_turns <= 2  # max_concurrency + max_nested_concurrency
    await _drain(runtime, gate, keys)
    assert gate.peak <= 2
    assert sorted(gate.started) == sorted(keys)


async def test_no_per_parent_limit_is_still_globally_bounded() -> None:
    # ``[subagents] max_concurrent_children = 0`` reaches the runtime as None.
    gate = _PeakGate()
    runtime = TaskRuntime(
        storage=_Storage({}),
        turn_handler=gate,
        max_concurrency=2,
        max_concurrent_children=None,
        max_nested_concurrency=3,
    )
    parent = "agent:main:webchat:fan"
    await runtime.enqueue(_top(parent), "fan out", run_kind="default")
    await gate.wait_started(1)
    kids = [f"agent:worker:subagent:f{i}" for i in range(10)]
    for key in kids:
        await runtime.enqueue(_child(key, parent), "t", run_kind="subagent")
    await gate.wait_started(5)
    await _settle()

    # The parent's shared slot, one more shared slot, three nested ones.
    assert len(gate.started) == 5
    assert runtime._nested_in_flight == 3
    assert runtime._global_in_flight == 2
    assert runtime.active_turns == 5  # max_concurrency + max_nested_concurrency

    await _drain(runtime, gate, [parent, *kids])
    assert gate.peak == 5
    assert sorted(gate.started) == sorted([parent, *kids])


async def test_nested_pool_defaults_to_max_concurrency() -> None:
    async def handler(_: Any) -> None:
        return None

    runtime = TaskRuntime(storage=_Storage({}), turn_handler=handler, max_concurrency=3)
    assert runtime._max_nested_concurrency == 3
    with pytest.raises(ValueError):
        TaskRuntime(storage=_Storage({}), turn_handler=handler, max_nested_concurrency=0)


async def test_nesting_deeper_than_one_level_stays_bounded() -> None:
    gate = _PeakGate()
    runtime = TaskRuntime(
        storage=_Storage({}),
        turn_handler=gate,
        max_concurrency=1,
        max_nested_concurrency=2,
        max_concurrent_children=None,
    )
    parent = "agent:main:webchat:root"
    child = "agent:worker:subagent:mid"
    grandkids = ["agent:worker:subagent:leaf0", "agent:worker:subagent:leaf1"]
    await runtime.enqueue(_top(parent), "spawn", run_kind="default")
    await gate.wait_started(1)
    await runtime.enqueue(_child(child, parent), "spawn more", run_kind="subagent")
    await gate.wait_started(2)
    # The child's run is alive, so its own child is nested too.
    for key in grandkids:
        await runtime.enqueue(_child(key, child), "leaf", run_kind="subagent")
    await gate.wait_started(3)
    await _settle()
    assert gate.started == [parent, child, grandkids[0]]
    assert runtime._nested_in_flight == 2
    assert runtime.active_turns == 3  # 1 shared + 2 nested: the ceiling

    # The root ends and frees the shared slot; the mid child and the first
    # leaf keep their nested slots. The mid child's run is alive but the
    # nested pool is full, so the second leaf takes the shared slot. The total
    # never passes the ceiling.
    gate.open(parent)
    await gate.wait_started(4)
    await _settle()
    assert gate.started[-1] == grandkids[1]
    assert runtime._global_in_flight == 1
    assert runtime._nested_in_flight == 2
    assert runtime.active_turns == 3

    await _drain(runtime, gate, [child, *grandkids])
    assert gate.peak == 3


async def test_a_child_landing_in_the_shared_pool_honours_reserved_slots() -> None:
    gate = _Gate()
    runtime = TaskRuntime(
        storage=_Storage({}),
        turn_handler=gate,
        max_concurrency=3,
        subagent_reserved_slots=1,
        max_nested_concurrency=1,
        max_concurrent_children=None,
    )
    parent = "agent:main:webchat:busy"
    kids = [f"agent:worker:subagent:r{i}" for i in range(3)]
    await runtime.enqueue(_top(parent), "spawn", run_kind="default")
    await gate.wait_started(1)
    for key in kids:
        await runtime.enqueue(_child(key, parent), "t", run_kind="subagent")
    await gate.wait_started(3)
    await _settle()

    # r0 nested, r1 shared (3 - 1 in flight > 1 reserved), r2 must wait.
    assert gate.started == [parent, kids[0], kids[1]]
    assert runtime._global_in_flight == 2
    # The reserved slot is still there for other work.
    other = "agent:ops:webchat:other"
    await runtime.enqueue(
        RouteEnvelope(
            source_kind=SourceKind.SYSTEM, source_name="test", agent_id="ops", session_key=other
        ),
        "hello",
        run_kind="default",
    )
    await gate.wait_started(4)
    assert gate.started[-1] == other

    # The parent's run ends: r0 keeps its nested slot (the pool's only one),
    # and r2 still waits: the free shared slot is the reserved one.
    gate.open(parent)
    await _settle()
    assert kids[2] not in gate.started
    assert runtime._global_in_flight == 2
    assert runtime._nested_in_flight == 1
    gate.open(other)
    gate.open(kids[0])
    gate.open(kids[1])
    await gate.wait_started(5)
    assert gate.started[-1] == kids[2]
    await _drain(runtime, gate, kids)
