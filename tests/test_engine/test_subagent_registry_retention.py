"""A settled subagent run leaves the active set and lands in the bounded one.

#1131 made ``SubagentRegistry._archived`` a ``BoundedRegistry``, but the only
thing that writes to it -- ``archive()`` -- had no caller anywhere in ``src``.
So ``_archived`` stayed empty for the life of the process while ``_runs``, a
plain dict, kept every handle a session ever spawned, each one pinning its
``asyncio.Task`` and the entire result string the run produced.

``max_concurrent`` bounds how many run at once, not how many are remembered, so
a sequential workflow -- spawn, await, spawn the next -- grew ``_runs`` without
limit.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from agentos.engine.subagent import SubagentManager, SubagentRegistry, SubagentSpec
from agentos.util.bounded_registry import BoundedRegistry


@dataclass
class _Event:
    kind: str
    text: str = ""


class _Answering:
    """A child agent that emits one answer and finishes."""

    def __init__(self, payload: str) -> None:
        self._payload = payload

    async def run_turn(self, task: str):
        yield _Event("text_delta", self._payload)
        yield _Event("done")


class _Failing:
    def __init__(self, message: str) -> None:
        self._message = message

    async def run_turn(self, task: str):
        raise RuntimeError(self._message)
        yield _Event("done")  # pragma: no cover - generator marker


class _Hanging:
    async def run_turn(self, task: str):
        await asyncio.Event().wait()
        yield _Event("done")  # pragma: no cover - never reached


async def _settle(handle) -> None:
    """Let a cancelled or finished task run its done-callback."""
    try:
        await handle.task
    except (asyncio.CancelledError, Exception):
        pass
    await asyncio.sleep(0)


async def _cancel(*handles) -> None:
    for handle in handles:
        handle.task.cancel()
    for handle in handles:
        await _settle(handle)


async def _spawn_and_settle(manager: SubagentManager, agent, task: str = "t"):
    """Spawn one run and let its done-callback fire."""
    handle = await manager.spawn(SubagentSpec(task=task), lambda spec, depth: agent)
    try:
        await handle.task
    except Exception:
        pass
    await asyncio.sleep(0)
    return handle


@pytest.mark.asyncio
async def test_a_sequential_workflow_does_not_accumulate_handles() -> None:
    """The leak: 200 runs, one at a time, well inside ``max_concurrent``."""
    manager = SubagentManager()
    payload = "x" * 10_000

    for i in range(200):
        await _spawn_and_settle(manager, _Answering(payload), task=f"task {i}")

    registry = manager.registry
    assert registry.count_active() == 0
    assert len(registry._runs) == 0, "a settled run must not stay in the active set"
    assert len(registry._archived) == 200
    retained = sum(len(h.result) for h in registry._runs.values())
    assert retained == 0, "result text must not be pinned by the active set"


@pytest.mark.asyncio
async def test_the_archive_respects_its_ceiling() -> None:
    """Archiving must not just move the unbounded growth one field across."""
    manager = SubagentManager()
    registry = manager.registry
    ceiling = registry._archived.max_entries

    for i in range(ceiling + 200):
        await _spawn_and_settle(manager, _Answering("ok"), task=f"task {i}")

    assert len(registry._runs) == 0
    assert len(registry._archived) <= ceiling


@pytest.mark.asyncio
async def test_a_finished_run_is_still_addressable_by_its_id() -> None:
    """Callers poll a run by id after it ends; archiving must not lose it."""
    manager = SubagentManager()
    handle = await _spawn_and_settle(manager, _Answering("the answer"))

    found = manager.registry.get(handle.run_id)

    assert found is handle
    assert found.status == "done"
    assert found.result == "the answer"


@pytest.mark.asyncio
async def test_a_failed_run_is_archived_with_its_error() -> None:
    """The direction the issue did not report: errors settle the same way."""
    manager = SubagentManager()
    handle = await _spawn_and_settle(manager, _Failing("upstream refused"))

    registry = manager.registry
    assert handle.run_id not in registry._runs
    assert registry.get(handle.run_id) is handle
    assert handle.status == "error"
    assert "upstream refused" in handle.error


@pytest.mark.asyncio
async def test_an_aborted_run_is_archived_too() -> None:
    manager = SubagentManager()
    handle = await manager.spawn(SubagentSpec(task="t"), lambda spec, depth: _Hanging())
    await asyncio.sleep(0)

    assert manager.registry.abort(handle.run_id) is True
    await _settle(handle)

    registry = manager.registry
    assert handle.status == "aborted"
    assert handle.run_id not in registry._runs
    assert registry.get(handle.run_id) is handle


@pytest.mark.asyncio
async def test_a_running_run_stays_in_the_active_set() -> None:
    """Guard: only a settled run is archived, or concurrency accounting breaks."""
    manager = SubagentManager()
    handle = await manager.spawn(SubagentSpec(task="t"), lambda spec, depth: _Hanging())
    await asyncio.sleep(0)

    registry = manager.registry
    try:
        assert handle.run_id in registry._runs
        assert len(registry._archived) == 0
        assert registry.count_active() == 1
        assert registry.get_by_status("running") == [handle]
        assert registry.get(handle.run_id) is handle
    finally:
        await _cancel(handle)


@pytest.mark.asyncio
async def test_the_concurrency_limit_still_counts_live_runs() -> None:
    """Guard: settled runs never counted toward it; live ones still must."""
    manager = SubagentManager(max_concurrent=2)
    first = await manager.spawn(SubagentSpec(task="a"), lambda spec, depth: _Hanging())
    second = await manager.spawn(SubagentSpec(task="b"), lambda spec, depth: _Hanging())
    await asyncio.sleep(0)

    try:
        assert manager.can_spawn() is False
        with pytest.raises(RuntimeError, match="Max concurrent"):
            await manager.spawn(SubagentSpec(task="c"), lambda spec, depth: _Hanging())
    finally:
        await _cancel(first, second)

    assert manager.can_spawn() is True


@pytest.mark.asyncio
async def test_abort_all_still_reaches_every_running_run() -> None:
    """Guard: ``abort_all`` reads the active set, which is now smaller."""
    manager = SubagentManager()
    handles = [
        await manager.spawn(SubagentSpec(task=f"t{i}"), lambda spec, depth: _Hanging())
        for i in range(3)
    ]
    await _spawn_and_settle(manager, _Answering("done already"))

    aborted = await manager.abort_all()

    assert aborted == 3, "a settled run must not be re-aborted, a live one must be"
    for handle in handles:
        assert handle.status == "aborted"


def test_the_archived_registry_is_still_the_bounded_primitive() -> None:
    """Guard on the #1131 invariant this change relies on."""
    registry = SubagentRegistry()

    assert isinstance(registry._archived, BoundedRegistry)
