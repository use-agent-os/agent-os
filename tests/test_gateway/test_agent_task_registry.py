"""Regression tests for AgentTaskRegistry replacement-callback race (#1026).

Verifies that a cancelled predecessor's completion callback does not evict
a replacement task registered under the same session key.
"""

from __future__ import annotations

import asyncio

import pytest

from agentos.gateway.agent_tasks import AgentTaskRegistry


@pytest.mark.asyncio
async def test_cancelled_predecessor_does_not_evict_replacement() -> None:
    registry = AgentTaskRegistry()
    session_key = "agent:main:ws:dm:peer"

    blocker1 = asyncio.Event()
    task1 = asyncio.create_task(blocker1.wait())
    registry.register(session_key, task1)

    # Attach callback to wait deterministically for task1's done callbacks
    task1_done = asyncio.Event()
    task1.add_done_callback(lambda _: task1_done.set())

    blocker2 = asyncio.Event()
    task2 = asyncio.create_task(blocker2.wait())
    registry.register(session_key, task2)

    # Wait for task1 cancellation to complete and its done callbacks to fire
    await task1_done.wait()

    # Replacement task must remain registered and reported as running
    assert registry.is_running(session_key) is True
    assert registry.get(session_key) is task2

    # When replacement task finishes, it must be removed normally
    task2_done = asyncio.Event()
    task2.add_done_callback(lambda _: task2_done.set())
    blocker2.set()
    await task2_done.wait()

    assert registry.is_running(session_key) is False
    assert registry.get(session_key) is None


@pytest.mark.asyncio
async def test_chained_multiple_replacements() -> None:
    registry = AgentTaskRegistry()
    session_key = "agent:main:ws:dm:chain"

    blocker1 = asyncio.Event()
    task1 = asyncio.create_task(blocker1.wait())
    registry.register(session_key, task1)
    task1_done = asyncio.Event()
    task1.add_done_callback(lambda _: task1_done.set())

    blocker2 = asyncio.Event()
    task2 = asyncio.create_task(blocker2.wait())
    registry.register(session_key, task2)
    task2_done = asyncio.Event()
    task2.add_done_callback(lambda _: task2_done.set())

    blocker3 = asyncio.Event()
    task3 = asyncio.create_task(blocker3.wait())
    registry.register(session_key, task3)
    task3_done = asyncio.Event()
    task3.add_done_callback(lambda _: task3_done.set())

    # Wait for both cancelled predecessors to settle
    await task1_done.wait()
    await task2_done.wait()

    assert registry.is_running(session_key) is True
    assert registry.get(session_key) is task3

    blocker3.set()
    await task3_done.wait()

    assert registry.is_running(session_key) is False
    assert registry.get(session_key) is None


@pytest.mark.asyncio
async def test_normal_completion_cleans_up() -> None:
    registry = AgentTaskRegistry()
    session_key = "agent:main:ws:dm:normal"

    blocker = asyncio.Event()
    task = asyncio.create_task(blocker.wait())
    registry.register(session_key, task)

    assert registry.is_running(session_key) is True

    done_event = asyncio.Event()
    task.add_done_callback(lambda _: done_event.set())
    blocker.set()
    await done_event.wait()

    assert registry.is_running(session_key) is False
    assert registry.get(session_key) is None


@pytest.mark.asyncio
async def test_failed_task_cleans_up() -> None:
    registry = AgentTaskRegistry()
    session_key = "agent:main:ws:dm:fail"

    async def _failing() -> None:
        raise ValueError("boom")

    task = asyncio.create_task(_failing())
    registry.register(session_key, task)

    done_event = asyncio.Event()
    task.add_done_callback(lambda _: done_event.set())
    with pytest.raises(ValueError, match="boom"):
        await task

    await done_event.wait()
    assert registry.is_running(session_key) is False
    assert registry.get(session_key) is None


@pytest.mark.asyncio
async def test_cancel_existing_false_refuses_orphaning() -> None:
    registry = AgentTaskRegistry()
    session_key = "agent:main:ws:dm:no-cancel"

    blocker = asyncio.Event()
    task1 = asyncio.create_task(blocker.wait())
    registry.register(session_key, task1)

    task2 = asyncio.create_task(blocker.wait())
    with pytest.raises(RuntimeError, match="called while a task is still running"):
        registry.register(session_key, task2, cancel_existing=False)

    # task1 must still be running and registered
    assert registry.get(session_key) is task1
    task1.cancel()
    task2.cancel()


@pytest.mark.asyncio
async def test_cancel_method() -> None:
    registry = AgentTaskRegistry()
    session_key = "agent:main:ws:dm:cancel"

    # No task registered
    assert registry.cancel(session_key) is False

    blocker = asyncio.Event()
    task = asyncio.create_task(blocker.wait())
    registry.register(session_key, task)

    done_event = asyncio.Event()
    task.add_done_callback(lambda _: done_event.set())

    assert registry.cancel(session_key) is True
    await done_event.wait()

    assert task.cancelled() is True
    assert registry.is_running(session_key) is False
