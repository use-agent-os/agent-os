"""Regression tests for the AgentTaskRegistry replacement-callback race (#1026).

Every task's done callback used to unconditionally pop the session key, so a
cancelled predecessor's late callback removed its replacement from the
registry even though the replacement was still running.
"""

from __future__ import annotations

import asyncio

import pytest

from agentos.gateway.agent_tasks import AgentTaskRegistry


async def _settle() -> None:
    """Let scheduled done callbacks run deterministically."""
    for _ in range(5):
        await asyncio.sleep(0)


async def _sleep_forever() -> None:
    await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_predecessor_done_callback_keeps_replacement_registered() -> None:
    """A replacement registered before the predecessor's callback runs survives it."""
    registry = AgentTaskRegistry()
    started = asyncio.Event()

    async def predecessor() -> None:
        started.set()
        await _sleep_forever()

    first = asyncio.create_task(predecessor())
    registry.register("session", first)
    await started.wait()

    second = asyncio.create_task(_sleep_forever())
    # register() cancels the predecessor and stores the replacement.
    registry.register("session", second)

    # Let the cancellation complete and the predecessor's done callback run
    # while the replacement is still active.
    with pytest.raises(asyncio.CancelledError):
        await first
    await _settle()

    assert first.done()
    assert not second.done()
    assert registry.get("session") is second
    assert registry.is_running("session")

    # When the replacement itself finishes, it is removed normally.
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    await _settle()

    assert registry.get("session") is None
    assert not registry.is_running("session")


@pytest.mark.asyncio
async def test_completed_task_still_cleans_up_normally() -> None:
    registry = AgentTaskRegistry()

    async def work() -> str:
        return "ok"

    task = asyncio.create_task(work())
    registry.register("session", task)

    assert await task == "ok"
    await _settle()

    assert registry.get("session") is None
    assert not registry.is_running("session")


@pytest.mark.asyncio
async def test_failed_task_is_removed_from_registry() -> None:
    registry = AgentTaskRegistry()

    async def boom() -> None:
        raise RuntimeError("boom")

    task = asyncio.create_task(boom())
    registry.register("session", task)
    await _settle()

    assert registry.get("session") is None


@pytest.mark.asyncio
async def test_cancel_existing_false_still_refuses_to_orphan_live_task() -> None:
    registry = AgentTaskRegistry()
    first = asyncio.create_task(_sleep_forever())
    registry.register("session", first)

    with pytest.raises(RuntimeError):
        registry.register("session", asyncio.create_task(_sleep_forever()), cancel_existing=False)

    assert registry.get("session") is first
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await _settle()
