"""Issue #1984: a turn whose start-up fails must release its concurrency slot.

``_acquire_fair_slot`` claims the slot under the condition lock and then runs
``_mark_running`` / ``_emit`` outside it. ``_execute`` used to mirror the
claim in a local flag set only after that call returned, so a storage error, a
failing event emitter, or a cancellation landing in that tail left
``_global_in_flight`` permanently raised. At ``max_concurrency=1`` one such
failure wedged the runtime: every later turn waited forever on the slot.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentos.gateway.routing import RouteEnvelope, SourceKind
from agentos.gateway.task_runtime import TaskRuntime
from agentos.session.models import AgentTaskRecord, AgentTaskStatus


def _make_envelope(session_key: str) -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="test",
        agent_id="agent-1",
        session_key=session_key,
    )


def _make_storage(
    on_running: Callable[[str], Awaitable[None]] | None = None,
) -> Any:
    """Storage mock whose ``update_agent_task(status=RUNNING)`` can misbehave."""
    storage = MagicMock()
    task_db: dict[str, AgentTaskRecord] = {}

    async def create(record: AgentTaskRecord) -> None:
        task_db[record.task_id] = record

    async def update(task_id: str, **kwargs: Any) -> None:
        if kwargs.get("status") == AgentTaskStatus.RUNNING and on_running is not None:
            await on_running(task_id)
        rec = task_db.get(task_id)
        if rec is None:
            return
        for k, v in kwargs.items():
            if hasattr(rec, k):
                object.__setattr__(rec, k, v)

    async def get(task_id: str) -> AgentTaskRecord | None:
        return task_db.get(task_id)

    async def list_tasks(**_: Any) -> list[AgentTaskRecord]:
        return list(task_db.values())

    storage.create_agent_task = create
    storage.update_agent_task = update
    storage.get_agent_task = get
    storage.list_agent_tasks = list_tasks
    return storage


async def _settle(runtime: TaskRuntime, task_id: str, *, timeout: float = 5.0) -> AgentTaskRecord:
    try:
        return await runtime.wait(task_id, timeout=timeout)
    except Exception:  # noqa: BLE001 - the terminal record is what the test inspects.
        return await runtime.status(task_id)


async def _second_turn_runs(runtime: TaskRuntime) -> None:
    """Fail fast if the runtime is wedged instead of waiting on the slot forever."""
    handle = await runtime.enqueue(_make_envelope("agent-1::sess-2"), "second")
    record = await asyncio.wait_for(runtime.wait(handle.task_id, timeout=3.0), timeout=3.5)
    assert record.status == AgentTaskStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_storage_failure_while_marking_running_releases_the_slot() -> None:
    failing: set[str] = set()

    async def on_running(task_id: str) -> None:
        if task_id in failing:
            raise RuntimeError("database is locked")

    async def handler(_run: Any) -> None:
        await asyncio.sleep(0.001)

    runtime = TaskRuntime(
        storage=_make_storage(on_running), turn_handler=handler, max_concurrency=1
    )
    try:
        handle = await runtime.enqueue(_make_envelope("agent-1::sess-1"), "first")
        failing.add(handle.task_id)
        record = await _settle(runtime, handle.task_id)
        assert record.status == AgentTaskStatus.FAILED

        # The slot is released before the terminal record is written, so the
        # counter is already back down once wait() returns.
        assert runtime._global_in_flight == 0
        assert runtime._agent_in_flight == {}

        await _second_turn_runs(runtime)
    finally:
        await runtime.shutdown(timeout=1.0)


@pytest.mark.asyncio
async def test_event_emitter_failure_while_marking_running_releases_the_slot() -> None:
    async def emitter(_session_key: str, event_name: str, _payload: dict[str, Any]) -> None:
        if event_name == "task.running":
            raise RuntimeError("emitter down")

    async def handler(_run: Any) -> None:
        await asyncio.sleep(0.001)

    runtime = TaskRuntime(
        storage=_make_storage(),
        turn_handler=handler,
        max_concurrency=1,
        event_emitter=emitter,
    )
    try:
        handle = await runtime.enqueue(_make_envelope("agent-1::sess-1"), "first")
        record = await _settle(runtime, handle.task_id)
        assert record.status == AgentTaskStatus.FAILED
        assert runtime._global_in_flight == 0

        # The emitter fails every turn, so the runtime must still drain each
        # of them rather than wedging on the first.
        handle = await runtime.enqueue(_make_envelope("agent-1::sess-2"), "second")
        record = await asyncio.wait_for(_settle(runtime, handle.task_id, timeout=3.0), 3.5)
        assert record.status == AgentTaskStatus.FAILED
    finally:
        await runtime.shutdown(timeout=1.0)


@pytest.mark.asyncio
async def test_cancel_while_marking_running_releases_the_slot() -> None:
    """``sessions.abort`` can cancel a task while its start-up write is in flight."""
    runtime: TaskRuntime | None = None
    cancel_on: set[str] = set()

    async def on_running(task_id: str) -> None:
        if task_id in cancel_on and runtime is not None:
            await runtime.cancel(task_id=task_id, source="test", reason="abort")
            # cancel() targets the current task, so the CancelledError is
            # raised at this suspension point -- inside _mark_running.
            await asyncio.sleep(0)

    async def handler(_run: Any) -> None:
        await asyncio.sleep(0.001)

    runtime = TaskRuntime(
        storage=_make_storage(on_running), turn_handler=handler, max_concurrency=1
    )
    try:
        handle = await runtime.enqueue(_make_envelope("agent-1::sess-1"), "first")
        cancel_on.add(handle.task_id)
        record = await _settle(runtime, handle.task_id)
        assert record.status == AgentTaskStatus.CANCELLED
        assert runtime._global_in_flight == 0

        await _second_turn_runs(runtime)
    finally:
        await runtime.shutdown(timeout=1.0)
