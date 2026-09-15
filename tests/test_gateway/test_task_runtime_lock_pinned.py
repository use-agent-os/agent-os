"""Regression tests for #1965: a session's write lock survives registry churn.

``TaskRuntime._execute`` fetches the session's write lock once and records
``id(write_lock)`` in the bypass contextvars for the whole turn, but the lock
is only *held* for an instant. ``_session_locks`` is a ``BoundedRegistry``
whose eviction predicate is "not locked", so enough unrelated sessions could
evict the in-flight turn's lock and hand every later caller for the same
session a different object — the turn's own writes then bypassed a lock
nobody else was holding, and an RPC writer took a lock the turn never saw.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentos.gateway.routing import RouteEnvelope, SourceKind
from agentos.gateway.task_runtime import TaskRuntime
from agentos.session.models import AgentTaskRecord
from agentos.util.bounded_registry import (
    configure_registry_limits,
    reset_registry_limits,
)


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


@pytest.fixture
def small_registry():
    configure_registry_limits(session_max_entries=4)
    try:
        yield
    finally:
        reset_registry_limits()


@pytest.mark.usefixtures("small_registry")
async def test_in_flight_turn_keeps_its_write_lock_identity() -> None:
    session_key = "agent-1::sess-target"
    started = asyncio.Event()
    release = asyncio.Event()
    seen: dict[str, asyncio.Lock] = {}

    async def _handler(_run: Any) -> None:
        # The lock the turn was started with, as TurnRunner would see it.
        seen["during"] = rt._get_session_lock_for_turn(session_key)
        started.set()
        await release.wait()
        seen["after_churn"] = rt._get_session_lock_for_turn(session_key)

    rt = TaskRuntime(storage=_make_storage(), turn_handler=_handler, max_concurrency=4)
    handle = await rt.enqueue(_make_envelope(session_key), "msg")
    await asyncio.wait_for(started.wait(), timeout=5.0)
    original = seen["during"]

    # Unrelated sessions churn through the registry well past its ceiling
    # while the target turn is still in flight.
    for i in range(20):
        rt._get_session_lock_for_turn(f"agent-1::sess-other-{i}")

    assert session_key in rt._session_locks, "in-flight session's lock was evicted"
    assert rt._get_session_lock_for_turn(session_key) is original

    release.set()
    await rt.wait(handle.task_id, timeout=5.0)
    assert seen["after_churn"] is original


@pytest.mark.usefixtures("small_registry")
async def test_write_lock_becomes_evictable_again_after_the_turn() -> None:
    session_key = "agent-1::sess-done"

    async def _instant(_run: Any) -> None:
        pass

    rt = TaskRuntime(storage=_make_storage(), turn_handler=_instant, max_concurrency=4)
    handle = await rt.enqueue(_make_envelope(session_key), "msg")
    await rt.wait(handle.task_id, timeout=5.0)

    for i in range(20):
        rt._get_session_lock_for_turn(f"agent-1::sess-other-{i}")

    assert session_key not in rt._session_locks, "finished turn kept its lock pinned"


@pytest.mark.usefixtures("small_registry")
async def test_queued_turns_on_one_session_keep_the_pin_until_the_last_finishes() -> None:
    """Two queued turns fetch the same lock before either runs; the pin must
    outlive the first turn or the second runs on an evicted lock."""
    session_key = "agent-1::sess-queued"
    gate = asyncio.Event()
    locks_seen: list[asyncio.Lock] = []

    async def _handler(_run: Any) -> None:
        locks_seen.append(rt._get_session_lock_for_turn(session_key))
        await gate.wait()

    rt = TaskRuntime(
        storage=_make_storage(),
        turn_handler=_handler,
        max_concurrency=4,
        max_pending_per_session=None,
    )
    first = await rt.enqueue(_make_envelope(session_key), "one")
    second = await rt.enqueue(_make_envelope(session_key), "two")
    await asyncio.sleep(0.05)
    original = rt._get_session_lock_for_turn(session_key)

    gate.set()
    await rt.wait(first.task_id, timeout=5.0)
    gate.clear()
    await asyncio.sleep(0.05)
    for i in range(20):
        rt._get_session_lock_for_turn(f"agent-1::sess-other-{i}")
    assert rt._get_session_lock_for_turn(session_key) is original

    gate.set()
    await rt.wait(second.task_id, timeout=5.0)
    assert locks_seen == [original, original]


@pytest.mark.usefixtures("small_registry")
async def test_execution_lock_is_pinned_for_queued_turns_too() -> None:
    """``asyncio.Lock.locked()`` is False between ``release()`` and the woken
    waiter re-locking it, so a queued turn's execution lock could be evicted
    in that window and a later turn for the same session would run alongside
    it. The pin must cover the execution lock for every turn in ``_execute``,
    queued or running."""
    session_key = "agent-1::sess-exec"
    gate = asyncio.Event()

    async def _handler(_run: Any) -> None:
        await gate.wait()

    rt = TaskRuntime(
        storage=_make_storage(),
        turn_handler=_handler,
        max_concurrency=4,
        max_pending_per_session=None,
    )
    first = await rt.enqueue(_make_envelope(session_key), "one")
    second = await rt.enqueue(_make_envelope(session_key), "two")
    await asyncio.sleep(0.05)
    execution_lock = rt._session_execution_locks[session_key]
    assert execution_lock.locked()
    # Both turns are inside ``_execute``: the running one and the queued one.
    assert rt._pinned_locks[id(execution_lock)] == 2

    gate.set()
    await rt.wait(first.task_id, timeout=5.0)
    gate.clear()
    await asyncio.sleep(0.05)
    assert rt._pinned_locks[id(execution_lock)] == 1

    gate.set()
    await rt.wait(second.task_id, timeout=5.0)
    assert id(execution_lock) not in rt._pinned_locks


@pytest.mark.usefixtures("small_registry")
async def test_pinned_unlocked_lock_survives_registry_churn() -> None:
    """The pin, not ``locked()``, is what keeps an in-use lock resident."""
    rt = TaskRuntime(storage=_make_storage(), turn_handler=lambda _run: None, max_concurrency=4)

    lock = rt._pin_lock(rt._session_execution_locks, "agent-1::sess-pinned")
    assert not lock.locked()
    for i in range(20):
        rt._session_execution_locks.setdefault(f"agent-1::sess-other-{i}", asyncio.Lock())
    assert rt._session_execution_locks.get("agent-1::sess-pinned") is lock

    rt._unpin_lock(lock)
    for i in range(20, 40):
        rt._session_execution_locks.setdefault(f"agent-1::sess-other-{i}", asyncio.Lock())
    assert "agent-1::sess-pinned" not in rt._session_execution_locks
