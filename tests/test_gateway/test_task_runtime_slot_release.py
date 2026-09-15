"""A concurrency slot must come back however the turn's start-up fails.

``_acquire_fair_slot`` claims the slot under the condition lock and then, still
inside itself, marks the task running: a storage write and an event emit. A
failure or a cancellation in that tail used to leave the slot claimed, because
``_execute`` only set its local "acquired" mirror after the call returned. The
counter never came back down, and at ``max_concurrency=1`` the runtime stopped
running turns at all.

Assertions are on ``_global_in_flight`` and on whether a later task still runs.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentos.gateway.routing import RouteEnvelope, SourceKind
from agentos.gateway.task_runtime import TaskRuntime
from agentos.session.models import AgentTaskRecord, AgentTaskStatus


def _envelope(session_key: str) -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="test",
        agent_id="a",
        session_key=session_key,
        input_provenance={"kind": "test"},
    )


def _storage(fail_running_for: set[str], error: BaseException | None = None) -> Any:
    """Storage whose ``status=RUNNING`` write raises for the named task ids."""
    storage = MagicMock()
    task_db: dict[str, AgentTaskRecord] = {}

    async def create(record: AgentTaskRecord) -> None:
        task_db[record.task_id] = record

    async def update(task_id: str, **kwargs: Any) -> None:
        if kwargs.get("status") == AgentTaskStatus.RUNNING and task_id in fail_running_for:
            raise error or RuntimeError("database is locked")
        record = task_db.get(task_id)
        if record is None:
            return
        for key, value in kwargs.items():
            if hasattr(record, key):
                object.__setattr__(record, key, value)

    async def get(task_id: str) -> AgentTaskRecord | None:
        return task_db.get(task_id)

    async def list_tasks(**_: Any) -> list[AgentTaskRecord]:
        return list(task_db.values())

    storage.create_agent_task = create
    storage.update_agent_task = update
    storage.get_agent_task = get
    storage.list_agent_tasks = list_tasks
    return storage


def _runtime(storage: Any, **kwargs: Any) -> TaskRuntime:
    async def turn_handler(_run: Any) -> None:
        await asyncio.sleep(0.001)

    return TaskRuntime(
        storage=storage,
        turn_handler=turn_handler,
        max_concurrency=1,
        **kwargs,
    )


async def _run_and_settle(runtime: TaskRuntime, session_key: str) -> None:
    handle = await runtime.enqueue(_envelope(session_key), "go")
    with_suppressed = asyncio.CancelledError, Exception
    try:
        await runtime.wait(handle.task_id, timeout=5.0)
    except with_suppressed:
        pass
    # The release runs as the task unwinds, after wait() has been settled.
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_a_completed_turn_leaves_no_slot_held() -> None:
    """Positive control: the counter is a meaningful thing to assert on."""
    runtime = _runtime(_storage(set()))

    await _run_and_settle(runtime, "agent:a:ok")

    assert runtime._global_in_flight == 0


@pytest.mark.asyncio
async def test_a_storage_failure_while_marking_running_releases_the_slot() -> None:
    failing: set[str] = set()
    runtime = _runtime(_storage(failing))
    handle = await runtime.enqueue(_envelope("agent:a:s1"), "go")
    failing.add(handle.task_id)

    try:
        await runtime.wait(handle.task_id, timeout=5.0)
    except Exception:
        pass
    await asyncio.sleep(0.1)

    assert runtime._global_in_flight == 0


@pytest.mark.asyncio
async def test_a_cancellation_while_marking_running_releases_the_slot() -> None:
    failing: set[str] = set()
    runtime = _runtime(_storage(failing, error=asyncio.CancelledError()))
    handle = await runtime.enqueue(_envelope("agent:a:s1"), "go")
    failing.add(handle.task_id)

    try:
        await runtime.wait(handle.task_id, timeout=5.0)
    except (asyncio.CancelledError, Exception):
        pass
    await asyncio.sleep(0.1)

    assert runtime._global_in_flight == 0


@pytest.mark.asyncio
async def test_an_event_emitter_failure_releases_the_slot() -> None:
    async def emitter(_session_key: str, event_name: str, _payload: dict[str, Any]) -> None:
        if event_name == "task.running":
            raise RuntimeError("websocket registry is gone")

    runtime = _runtime(_storage(set()), event_emitter=emitter)

    await _run_and_settle(runtime, "agent:a:s1")

    assert runtime._global_in_flight == 0


@pytest.mark.asyncio
async def test_the_runtime_still_serves_turns_after_a_failed_start() -> None:
    """The consequence that matters: at max_concurrency=1 a leak wedges the gateway."""
    failing: set[str] = set()
    runtime = _runtime(_storage(failing))
    first = await runtime.enqueue(_envelope("agent:a:s1"), "first")
    failing.add(first.task_id)
    try:
        await runtime.wait(first.task_id, timeout=5.0)
    except Exception:
        pass
    await asyncio.sleep(0.1)

    second = await runtime.enqueue(_envelope("agent:a:s2"), "second")
    record = await asyncio.wait_for(runtime.wait(second.task_id, timeout=3.0), timeout=3.5)

    assert record is not None
    assert runtime._global_in_flight == 0
