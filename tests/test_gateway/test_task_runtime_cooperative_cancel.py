"""TaskRuntime cancels agent-loop turns cooperatively first (issue #3818).

``cancel`` asks a running turn that accepts it (the agent loop sets
``accepts_abort``) and is not inside a tool batch to stop
(``TurnStopSignal.request_abort``), and cancels its task only when it has not
stopped after ``cancel_grace_s``. A turn that stops on its own is still
recorded as a cancelled run. Every other running turn is cancelled at once.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from agentos.engine.turn_progress import current_turn_stop_signal
from agentos.gateway.routing import RouteEnvelope, SourceKind
from agentos.gateway.task_runtime import TaskRuntime
from agentos.session.models import AgentTaskRecord, AgentTaskStatus


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


def _envelope() -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.SYSTEM,
        source_name="test",
        agent_id="main",
        session_key="agent:main:webchat:coop",
    )


async def _start(runtime: TaskRuntime, started: asyncio.Event) -> Any:
    handle = await runtime.enqueue(_envelope(), "hi", run_kind="default")
    await asyncio.wait_for(started.wait(), timeout=2)
    return handle


async def test_a_turn_that_honours_the_abort_ends_cancelled_without_a_hard_cancel() -> None:
    started = asyncio.Event()
    outcome: dict[str, Any] = {}

    async def handler(run: Any) -> None:
        signal = current_turn_stop_signal()
        assert signal is not None
        signal.accepts_abort = True  # what the agent loop does
        started.set()
        try:
            await asyncio.wait_for(signal.wait_abort(), timeout=5)
            outcome["stopped_on_its_own"] = True
        except asyncio.CancelledError:
            outcome["hard_cancelled"] = True
            raise

    storage = _Storage({})
    runtime = TaskRuntime(storage=storage, turn_handler=handler, cancel_grace_s=5.0)
    handle = await _start(runtime, started)

    assert await runtime.cancel(task_id=handle.task_id, source="user") == 1
    await runtime.wait(handle.task_id, timeout=3)

    assert outcome == {"stopped_on_its_own": True}
    record = storage.records[handle.task_id]
    assert record.status == AgentTaskStatus.CANCELLED
    assert record.terminal_reason == "cancelled"


async def test_a_turn_that_ignores_the_abort_is_cancelled_after_the_grace() -> None:
    started = asyncio.Event()
    outcome: dict[str, Any] = {}

    async def handler(run: Any) -> None:
        signal = current_turn_stop_signal()
        assert signal is not None
        signal.accepts_abort = True
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            outcome["hard_cancelled"] = True
            raise

    storage = _Storage({})
    runtime = TaskRuntime(storage=storage, turn_handler=handler, cancel_grace_s=0.1)
    handle = await _start(runtime, started)
    loop = asyncio.get_running_loop()
    began = loop.time()

    await runtime.cancel(task_id=handle.task_id, source="user")
    await runtime.wait(handle.task_id, timeout=3)

    assert outcome == {"hard_cancelled": True}
    # The grace was honoured. Windows' loop clock ticks in ~16 ms steps, so a
    # 0.1 s wait can read as 0.094 s there.
    assert loop.time() - began >= 0.1 - 0.02
    assert storage.records[handle.task_id].status == AgentTaskStatus.CANCELLED


async def test_zero_grace_cancels_at_once() -> None:
    started = asyncio.Event()
    outcome: dict[str, Any] = {}

    async def handler(run: Any) -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            signal = current_turn_stop_signal()
            outcome["abort_requested"] = bool(signal and signal.aborted)
            raise

    storage = _Storage({})
    runtime = TaskRuntime(storage=storage, turn_handler=handler, cancel_grace_s=0)
    handle = await _start(runtime, started)

    await runtime.cancel(task_id=handle.task_id, source="user")
    await runtime.wait(handle.task_id, timeout=3)

    assert outcome == {"abort_requested": False}
    assert storage.records[handle.task_id].status == AgentTaskStatus.CANCELLED


async def _cancel_is_immediate(prepare: Any) -> None:
    started = asyncio.Event()
    outcome: dict[str, Any] = {}

    async def handler(run: Any) -> None:
        signal = current_turn_stop_signal()
        assert signal is not None
        prepare(signal)
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            outcome["abort_requested"] = signal.aborted
            raise

    storage = _Storage({})
    runtime = TaskRuntime(storage=storage, turn_handler=handler, cancel_grace_s=30.0)
    handle = await _start(runtime, started)
    loop = asyncio.get_running_loop()
    began = loop.time()

    await runtime.cancel(task_id=handle.task_id, source="user")
    await runtime.wait(handle.task_id, timeout=3)

    assert loop.time() - began < 2
    assert outcome == {"abort_requested": False}
    assert storage.records[handle.task_id].status == AgentTaskStatus.CANCELLED


async def test_a_turn_inside_a_tool_batch_is_cancelled_at_once() -> None:
    def in_batch(signal: Any) -> None:
        signal.accepts_abort = True
        signal.in_tool_batch = True

    await _cancel_is_immediate(in_batch)


async def test_a_turn_that_does_not_accept_aborts_is_cancelled_at_once() -> None:
    await _cancel_is_immediate(lambda signal: None)  # e.g. an ACP harness turn


async def test_a_turn_that_stops_near_the_deadline_still_records_its_outcome() -> None:
    # The turn honours the abort just before the grace runs out, and recording
    # its outcome takes longer than what is left. The fallback must not cancel
    # that bookkeeping: it skipped ``done`` and every waiter hung.
    started = asyncio.Event()

    async def handler(run: Any) -> None:
        signal = current_turn_stop_signal()
        assert signal is not None
        signal.accepts_abort = True
        started.set()
        await signal.wait_abort()
        await asyncio.sleep(0.25)  # stops on its own, 0.15 s before the grace ends

    class _SlowStorage(_Storage):
        async def update_agent_task(self, task_id: str, **fields: Any) -> None:
            if "terminal_reason" in fields:
                await asyncio.sleep(0.5)  # the terminal write outlives the grace
            await super().update_agent_task(task_id, **fields)

    storage = _SlowStorage({})
    runtime = TaskRuntime(storage=storage, turn_handler=handler, cancel_grace_s=0.4)
    handle = await _start(runtime, started)

    await runtime.cancel(task_id=handle.task_id, source="user")
    await asyncio.wait_for(runtime.wait(handle.task_id, timeout=3), timeout=4)

    record = storage.records[handle.task_id]
    assert record.status == AgentTaskStatus.CANCELLED
    assert record.terminal_reason == "cancelled"
