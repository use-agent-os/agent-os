"""A turn blocked inline on another task holds no slot while it waits (#3867).

``sessions_yield(session_key=...)`` blocks the calling turn until the child's
task ends. Before, the waiting turn kept its slot, so a chain of parents each
waiting on its own child held one slot per level: two chains at the spawn
depth limit (root plus three children) filled both pools at the default limits
(4 shared slots with 2 reserved from children, 4 nested) and the leaves never
started until the inline waits timed out.

Now a waiting turn gives its slot back and takes one back, ahead of queued
turns, when the wait ends. The tests drive the loop with ``asyncio.sleep(0)``
only: nothing here waits on a wall-clock timer.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from agentos.gateway.routing import RouteEnvelope, SourceKind
from agentos.gateway.task_runtime import TaskRuntime
from agentos.session.models import AgentTaskRecord, AgentTaskStatus
from agentos.tools.builtin import sessions as sessions_tools
from agentos.tools.types import ToolContext, current_tool_context

#: ``MAX_SPAWN_DEPTH``: a root turn and three levels of children.
DEPTH = 3


@dataclass
class _Storage:
    records: dict

    async def create_agent_task(self, record: AgentTaskRecord) -> None:
        self.records[record.task_id] = record

    async def get_agent_task(self, task_id: str) -> Any:
        return self.records.get(task_id)

    async def list_agent_tasks(self, *, session_key: str | None = None, **_: Any) -> list[Any]:
        return [
            record
            for record in self.records.values()
            if session_key is None or record.session_key == session_key
        ]

    async def update_agent_task(self, task_id: str, **fields: Any) -> None:
        record = self.records.get(task_id)
        if record is not None:
            for key, value in fields.items():
                setattr(record, key, value)


class _Manager:
    """The slice of the session manager ``sessions_yield`` reads."""

    async def get_current_session(self) -> Any:
        return None

    async def get_session(self, key: str) -> Any:
        return type("_Session", (), {"session_key": key, "status": "running"})()


def _top(session_key: str) -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.SYSTEM, source_name="test", agent_id="main", session_key=session_key
    )


def _child(session_key: str, parent: str) -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.SYSTEM,
        source_name="test",
        agent_id="worker",
        session_key=session_key,
        metadata={"parent_session_key": parent},
    )


async def _spin_until(predicate: Callable[[], bool], *, rounds: int = 5000) -> bool:
    """Run the loop until *predicate* holds, one ready callback batch at a time."""
    for _ in range(rounds):
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


async def _yield_on(session_key: str, task_id: str, child_key: str) -> dict[str, Any]:
    """Call ``sessions_yield(session_key=child)`` as the turn ``task_id``."""
    token = current_tool_context.set(ToolContext(session_key=session_key, task_id=task_id))
    try:
        return json.loads(
            await sessions_tools.sessions_yield(session_key=child_key, timeout_seconds=3600)
        )
    finally:
        current_tool_context.reset(token)


class _Chains:
    """Turns that spawn one child each and wait on it inline, down to ``DEPTH``."""

    def __init__(self) -> None:
        self.runtime: TaskRuntime | None = None
        self.depth: dict[str, int] = {}
        self.started: list[str] = []
        self.yields: dict[str, dict[str, Any]] = {}
        self.peak_slots = 0

    def _note_slots(self) -> None:
        assert self.runtime is not None
        held = self.runtime._global_in_flight + self.runtime._nested_in_flight
        self.peak_slots = max(self.peak_slots, held)

    async def __call__(self, run: Any) -> None:
        assert self.runtime is not None
        key = run.session_key
        self.started.append(key)
        self._note_slots()
        depth = self.depth.setdefault(key, 0)
        if depth == DEPTH:
            return
        child_key = f"agent:worker:subagent:{key.rsplit(':', 1)[-1]}-{depth + 1}"
        self.depth[child_key] = depth + 1
        await self.runtime.enqueue(_child(child_key, key), "work", run_kind="subagent")
        self.yields[key] = await _yield_on(key, run.task_id, child_key)
        self._note_slots()


@pytest.fixture
def _sessions_tools(monkeypatch: pytest.MonkeyPatch) -> Callable[[TaskRuntime], None]:
    monkeypatch.setattr(sessions_tools, "_session_manager", _Manager())

    def install(runtime: TaskRuntime) -> None:
        monkeypatch.setattr(sessions_tools, "_task_runtime", runtime)

    return install


async def test_two_deep_chains_waiting_inline_do_not_fill_both_pools(_sessions_tools) -> None:
    chains = _Chains()
    # The shipped defaults: ``[task_runtime] max_concurrency = 4`` (nested pool
    # the same size), ``[subagents] subagent_reserved_slots = 2`` and
    # ``max_concurrent_children = 2``.
    runtime = TaskRuntime(
        storage=_Storage({}),
        turn_handler=chains,
        max_concurrency=4,
        subagent_reserved_slots=2,
        max_concurrent_children=2,
        running_heartbeat_interval_s=None,
    )
    chains.runtime = runtime
    _sessions_tools(runtime)
    roots = ["agent:main:webchat:a", "agent:main:webchat:b"]
    handles = [await runtime.enqueue(_top(key), "go deep", run_kind="default") for key in roots]
    try:
        finished = await _spin_until(lambda: all(h.task_id not in runtime._tasks for h in handles))
        leaves = [key for key in chains.started if chains.depth[key] == DEPTH]
        assert finished, (
            f"inline waits starved the chains: {len(leaves)} of 2 leaves started, "
            f"{runtime._global_in_flight} shared + {runtime._nested_in_flight} nested held"
        )
    finally:
        await runtime.shutdown(cancel=True, timeout=1.0)

    assert len(chains.started) == 2 * (DEPTH + 1)
    for handle in handles:
        record = await runtime.status(handle.task_id)
        assert record.status == AgentTaskStatus.SUCCEEDED
    # Every level's inline wait saw its child finish; none timed out.
    assert len(chains.yields) == 2 * DEPTH
    assert all(payload.get("timed_out") is None for payload in chains.yields.values())
    assert all(
        payload["status"] == str(AgentTaskStatus.SUCCEEDED) for payload in chains.yields.values()
    )
    # A waiting turn gave its slot back, so the pools were never exceeded.
    assert chains.peak_slots <= 4 + 4
    assert runtime.active_turns == 0
    assert runtime._parked_turns == 0


async def test_one_slot_each_still_runs_a_chain_to_the_depth_limit(_sessions_tools) -> None:
    # ``max_concurrency = 1``: one shared and one nested slot for a chain of
    # four turns. Each waiting level frees the slot its child needs.
    chains = _Chains()
    runtime = TaskRuntime(
        storage=_Storage({}),
        turn_handler=chains,
        max_concurrency=1,
        running_heartbeat_interval_s=None,
    )
    chains.runtime = runtime
    _sessions_tools(runtime)
    handle = await runtime.enqueue(_top("agent:main:webchat:solo"), "go deep", run_kind="default")
    try:
        assert await _spin_until(lambda: handle.task_id not in runtime._tasks)
    finally:
        await runtime.shutdown(cancel=True, timeout=1.0)
    assert (await runtime.status(handle.task_id)).status == AgentTaskStatus.SUCCEEDED
    assert len(chains.started) == DEPTH + 1
    assert chains.peak_slots <= 2
    assert runtime.active_turns == 0


class _Script:
    """Per-session turn bodies, with a log of what happened in which order."""

    def __init__(self) -> None:
        self.bodies: dict[str, Callable[[Any], Any]] = {}
        self.log: list[str] = []
        self.gates: dict[str, asyncio.Event] = {}

    def gate(self, key: str) -> asyncio.Event:
        return self.gates.setdefault(key, asyncio.Event())

    async def __call__(self, run: Any) -> None:
        key = run.session_key
        self.log.append(f"start {key}")
        body = self.bodies.get(key)
        if body is not None:
            await body(run)
        else:
            await self.gate(key).wait()
        self.log.append(f"end {key}")


class _YieldingStorage(_Storage):
    """Every write yields to the loop once, like real I/O does."""

    async def update_agent_task(self, task_id: str, **fields: Any) -> None:
        await asyncio.sleep(0)
        await super().update_agent_task(task_id, **fields)


def _runtime(script: _Script, storage: _Storage | None = None, **limits: Any) -> TaskRuntime:
    return TaskRuntime(
        storage=storage or _Storage({}),
        turn_handler=script,
        running_heartbeat_interval_s=None,
        **limits,
    )


def _parent_waiting_on(
    runtime: TaskRuntime, script: _Script, child_key: str, **wait: Any
) -> Callable[[Any], Any]:
    async def body(run: Any) -> None:
        handle = await runtime.enqueue(_child(child_key, run.session_key), "w", run_kind="subagent")
        try:
            await runtime.wait_inline(handle.task_id, waiter_task_id=run.task_id, **wait)
        except TimeoutError:
            script.log.append(f"timed out {run.session_key}")
        script.log.append(f"resumed {run.session_key}")
        await script.gate(run.session_key).wait()

    return body


async def test_a_parked_turn_comes_back_before_queued_turns() -> None:
    script = _Script()
    runtime = _runtime(script, max_concurrency=1)
    parent, kid = "agent:main:webchat:p", "agent:worker:subagent:k"
    first, second = "agent:main:webchat:f1", "agent:main:webchat:f2"
    script.bodies[parent] = _parent_waiting_on(runtime, script, kid)
    try:
        await runtime.enqueue(_top(parent), "spawn and wait", run_kind="default")
        assert await _spin_until(lambda: f"start {kid}" in script.log)
        # The parent is parked: its shared slot is free, the child is nested.
        assert runtime._global_in_flight == 0
        assert runtime._nested_in_flight == 1
        assert runtime._parked_turns == 1
        assert runtime.active_turns == 2

        # So another session's turn starts beside the waiting parent.
        await runtime.enqueue(_top(first), "hello", run_kind="default")
        assert await _spin_until(lambda: f"start {first}" in script.log)
        await runtime.enqueue(_top(second), "hello", run_kind="default")

        # The child ends; the parent wants its shared slot back. The second
        # turn, queued earlier, does not get the slot the first one frees.
        script.gate(kid).set()
        assert await _spin_until(lambda: runtime._resume_reserved_shared == 1)
        script.gate(first).set()
        assert await _spin_until(lambda: f"resumed {parent}" in script.log)
        assert f"start {second}" not in script.log
        assert runtime._global_in_flight == 1
        assert runtime._parked_turns == 0

        script.gate(parent).set()
        assert await _spin_until(lambda: f"start {second}" in script.log)
        script.gate(second).set()
        assert await _spin_until(lambda: not runtime._tasks)
    finally:
        await runtime.shutdown(cancel=True, timeout=1.0)
    assert runtime.active_turns == 0
    assert runtime._resume_reserved_shared == 0


async def test_the_slot_an_awaited_child_frees_goes_back_to_its_parent() -> None:
    # The child runs in the shared pool (the nested one is full). When it
    # ends, the turn queued for a shared slot must not slip into the one the
    # child frees before the parent wakes: that slot is the parent's. The
    # storage yields on every write, so the child's terminal bookkeeping
    # gives the queued turn a chance to run between the two.
    script = _Script()
    runtime = _runtime(script, _YieldingStorage({}), max_concurrency=3, max_nested_concurrency=1)
    holder, filler = "agent:main:webchat:h", "agent:worker:subagent:filler"
    parent, kid = "agent:main:webchat:p", "agent:worker:subagent:k"
    other, queued = "agent:main:webchat:o", "agent:main:webchat:q"
    script.bodies[parent] = _parent_waiting_on(runtime, script, kid)
    try:
        await runtime.enqueue(_top(holder), "hold", run_kind="default")
        assert await _spin_until(lambda: f"start {holder}" in script.log)
        await runtime.enqueue(_child(filler, holder), "w", run_kind="subagent")
        assert await _spin_until(lambda: f"start {filler}" in script.log)
        assert runtime._nested_in_flight == 1  # the nested pool is full
        await runtime.enqueue(_top(parent), "spawn and wait", run_kind="default")
        assert await _spin_until(lambda: f"start {kid}" in script.log)
        assert runtime._global_in_flight == 2  # the holder and the child
        await runtime.enqueue(_top(other), "hello", run_kind="default")
        assert await _spin_until(lambda: f"start {other}" in script.log)
        await runtime.enqueue(_top(queued), "hello", run_kind="default")
        assert await _spin_until(lambda: runtime._tasks and len(runtime._pending_by_session) == 1)

        script.gate(kid).set()
        assert await _spin_until(lambda: f"resumed {parent}" in script.log)
        assert f"start {queued}" not in script.log
        assert runtime._global_in_flight == 3

        for key in (parent, other, holder, filler, queued):
            script.gate(key).set()
        assert await _spin_until(lambda: not runtime._tasks)
    finally:
        await runtime.shutdown(cancel=True, timeout=1.0)
    assert runtime.active_turns == 0


async def test_a_timed_out_inline_wait_takes_the_slot_back() -> None:
    script = _Script()
    runtime = _runtime(script, max_concurrency=1, max_nested_concurrency=1)
    parent, kid = "agent:main:webchat:p", "agent:worker:subagent:k"
    script.bodies[parent] = _parent_waiting_on(runtime, script, kid, timeout=0)
    try:
        await runtime.enqueue(_top(parent), "spawn and wait", run_kind="default")
        assert await _spin_until(lambda: f"resumed {parent}" in script.log)
        assert f"timed out {parent}" in script.log
        # Back in its shared slot; the child keeps its nested one.
        assert runtime._global_in_flight == 1
        assert runtime._parked_turns == 0
        assert runtime._inline_waiters == {}
        script.gate(parent).set()
        script.gate(kid).set()
        assert await _spin_until(lambda: not runtime._tasks)
    finally:
        await runtime.shutdown(cancel=True, timeout=1.0)
    assert runtime.active_turns == 0


async def test_stopping_a_parked_turn_leaves_no_slot_behind() -> None:
    script = _Script()
    runtime = _runtime(script, max_concurrency=1, max_nested_concurrency=1)
    parent, kid = "agent:main:webchat:p", "agent:worker:subagent:k"
    later = "agent:main:webchat:later"
    script.bodies[parent] = _parent_waiting_on(runtime, script, kid)
    try:
        handle = await runtime.enqueue(_top(parent), "spawn and wait", run_kind="default")
        assert await _spin_until(lambda: f"start {kid}" in script.log)
        assert runtime._parked_turns == 1
        await runtime.cancel(handle.task_id, source="test", reason="stop")
        assert await _spin_until(lambda: handle.task_id not in runtime._tasks)
        assert (await runtime.status(handle.task_id)).status == AgentTaskStatus.CANCELLED
        assert runtime._parked_turns == 0
        assert runtime._global_in_flight == 0
        assert runtime._inline_waiters == {}

        # The shared slot is free for the next turn.
        await runtime.enqueue(_top(later), "hello", run_kind="default")
        assert await _spin_until(lambda: f"start {later}" in script.log)
        script.gate(later).set()
        script.gate(kid).set()
        assert await _spin_until(lambda: not runtime._tasks)
    finally:
        await runtime.shutdown(cancel=True, timeout=1.0)
    assert runtime.active_turns == 0
    assert runtime._resume_reserved_shared == 0


async def test_an_inline_wait_cancelled_inside_the_turn_takes_its_slot_back() -> None:
    # A tool's execution timeout cancels the wait, not the turn: the turn goes
    # on and must hold a slot again, even when none is free.
    script = _Script()
    runtime = _runtime(script, max_concurrency=1, max_nested_concurrency=1)
    parent, kid = "agent:main:webchat:p", "agent:worker:subagent:k"
    other = "agent:main:webchat:o"
    waits: list[asyncio.Task[Any]] = []

    async def body(run: Any) -> None:
        handle = await runtime.enqueue(_child(kid, run.session_key), "w", run_kind="subagent")
        wait = asyncio.create_task(runtime.wait_inline(handle.task_id, waiter_task_id=run.task_id))
        waits.append(wait)
        with pytest.raises(asyncio.CancelledError):
            await wait
        script.log.append(f"resumed {parent}")
        await script.gate(parent).wait()

    script.bodies[parent] = body
    try:
        await runtime.enqueue(_top(parent), "spawn and wait", run_kind="default")
        assert await _spin_until(lambda: f"start {kid}" in script.log)
        await runtime.enqueue(_top(other), "hello", run_kind="default")
        assert await _spin_until(lambda: f"start {other}" in script.log)
        assert runtime._global_in_flight == 1  # the other turn took the slot

        waits[0].cancel()
        assert await _spin_until(lambda: f"resumed {parent}" in script.log)
        assert runtime._parked_turns == 0
        assert runtime._global_in_flight == 2  # past the cap until one ends

        for key in (parent, other, kid):
            script.gate(key).set()
        assert await _spin_until(lambda: not runtime._tasks)
    finally:
        await runtime.shutdown(cancel=True, timeout=1.0)
    assert runtime._global_in_flight == 0
    assert runtime.active_turns == 0


async def test_waiting_on_a_finished_or_unknown_task_does_not_park() -> None:
    script = _Script()
    runtime = _runtime(script, max_concurrency=1)
    parent = "agent:main:webchat:p"
    seen: list[int] = []

    async def body(run: Any) -> None:
        with pytest.raises(KeyError):
            await runtime.wait_inline("missing", waiter_task_id=run.task_id)
        seen.append(runtime._parked_turns)

    script.bodies[parent] = body
    handle = await runtime.enqueue(_top(parent), "wait on nothing", run_kind="default")
    try:
        assert await _spin_until(lambda: handle.task_id not in runtime._tasks)
    finally:
        await runtime.shutdown(cancel=True, timeout=1.0)
    assert seen == [0]
    assert runtime.active_turns == 0
