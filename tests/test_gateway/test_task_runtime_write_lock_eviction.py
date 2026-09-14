"""A session's in-flight locks must survive ceiling pressure from other sessions.

``TaskRuntime._execute`` fetches ``_session_locks[session_key]`` once at the
top of a turn but only actually holds it for an instant (see the no-op
``async with write_lock: pass``); for the rest of the turn its *identity* is
what ``engine.runtime``'s write-lock-bypass contextvars key off of. Between
that instant and the end of the turn, the lock sits unlocked -- which used to
make ``BoundedRegistry`` treat it as evictable the moment enough *other*
sessions filled the registry past its ceiling, exactly what a busy gateway
serving many concurrent sessions does routinely.

The execution lock has the same exposure in a narrower window: a queued
turn's asyncio.Task is already running (and already pins the lock -- see
``_pin_lock``) by the time it blocks on ``async with execution_lock:``, but
``asyncio.Lock.locked()`` itself reads False for a real window between the
previous turn's ``release()`` and this waiter's re-acquire, since the wakeup
runs on a later loop iteration rather than synchronously inside ``release()``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentos.gateway.routing import RouteEnvelope, SourceKind
from agentos.gateway.task_runtime import TaskRuntime
from agentos.session.models import AgentTaskRecord


def _make_envelope(session_key: str) -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="test",
        agent_id="agent-1",
        session_key=session_key,
        input_provenance={"kind": "test"},
    )


def _make_storage() -> Any:
    storage = MagicMock()
    task_db: dict[str, AgentTaskRecord] = {}

    async def create(record: AgentTaskRecord) -> None:
        task_db[record.task_id] = record

    async def update(task_id: str, **kwargs: Any) -> None:
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


def _make_runtime(turn_handler: Callable[..., Awaitable[Any]]) -> TaskRuntime:
    return TaskRuntime(storage=_make_storage(), turn_handler=turn_handler)


@pytest.mark.asyncio
async def test_write_lock_survives_ceiling_pressure_during_long_turn() -> None:
    """An in-flight turn's write lock must not be evicted by unrelated session churn."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_handler(_run: Any) -> None:
        started.set()
        await release.wait()

    rt = _make_runtime(turn_handler=_blocking_handler)
    target_key = "agent-1::sess-target"
    env = _make_envelope(target_key)

    handle = await rt.enqueue(env, "hello")
    try:
        await asyncio.wait_for(started.wait(), timeout=2.0)

        write_lock = rt._session_locks.get(target_key)
        assert write_lock is not None
        assert not write_lock.locked()

        # Fill the registry past its ceiling with unrelated, never-locked
        # sessions -- the exact shape of session churn on a busy gateway.
        # Every one of these is evictable; the target's write lock would be
        # the least-recently-touched entry if it weren't pinned.
        ceiling = rt._session_locks.max_entries
        for i in range(ceiling + 50):
            rt._session_locks.set(f"filler-{i}", asyncio.Lock())

        assert target_key in rt._session_locks, (
            "the in-flight turn's write lock was evicted mid-turn by "
            "unrelated session churn -- a later caller for this session_key "
            "would get a different Lock object via setdefault, silently "
            "breaking the serialization TaskRuntime and TurnRunner rely on "
            "its identity for"
        )
        assert rt._session_locks.get(target_key) is write_lock
    finally:
        release.set()
        await rt.wait(handle.task_id, timeout=2.0)


@pytest.mark.asyncio
async def test_execution_lock_survives_ceiling_pressure_across_queued_turns() -> None:
    """The execution lock must not be evicted in the handoff between one
    turn finishing and a queued turn for the same session re-acquiring it."""
    first_started = asyncio.Event()
    first_release = asyncio.Event()
    second_started = asyncio.Event()
    second_release = asyncio.Event()
    calls: list[int] = []

    async def _handler(_run: Any) -> None:
        if not calls:
            calls.append(1)
            first_started.set()
            await first_release.wait()
        else:
            second_started.set()
            await second_release.wait()

    rt = _make_runtime(turn_handler=_handler)
    target_key = "agent-1::sess-target"
    env = _make_envelope(target_key)

    handle1 = await rt.enqueue(env, "first")
    await asyncio.wait_for(first_started.wait(), timeout=2.0)

    handle2 = await rt.enqueue(env, "second")
    # Let the second task's coroutine actually start running (and pin the
    # execution lock) while it queues behind the first.
    await asyncio.sleep(0)

    execution_lock = rt._session_execution_locks.get(target_key)
    assert execution_lock is not None
    assert execution_lock.locked()
    assert id(execution_lock) in rt._locks_pinned_for_turn

    # Finish the first turn -- this releases the lock and hands off to the
    # queued second turn. Meanwhile flood the registry with churn to try to
    # evict the execution lock during that handoff.
    first_release.set()
    await asyncio.wait_for(second_started.wait(), timeout=2.0)

    ceiling = rt._session_execution_locks.max_entries
    for i in range(ceiling + 50):
        rt._session_execution_locks.set(f"filler-{i}", asyncio.Lock())

    assert target_key in rt._session_execution_locks, (
        "the execution lock was evicted during the handoff between the "
        "finished turn and the still-running queued turn for the same "
        "session -- a later caller for this session_key would get a "
        "different Lock object, breaking whole-turn-lifecycle serialization"
    )
    assert rt._session_execution_locks.get(target_key) is execution_lock

    second_release.set()
    await rt.wait(handle1.task_id, timeout=2.0)
    await rt.wait(handle2.task_id, timeout=2.0)

    # The pin must not leak once both turns are done.
    assert id(execution_lock) not in rt._locks_pinned_for_turn
