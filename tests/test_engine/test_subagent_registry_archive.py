"""Tests for SubagentRegistry handle archiving and lifecycle bounds."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from agentos.engine.subagent import SubagentHandle, SubagentManager, SubagentRegistry, SubagentSpec


@dataclass
class _DummyEvent:
    kind: str
    text: str = ""


class _MockAgent:
    def __init__(self, output: str = "done", fail: bool = False) -> None:
        self.output = output
        self.fail = fail

    async def run_turn(self, task: str) -> Any:
        if self.fail:
            raise RuntimeError("agent execution failed")
        yield _DummyEvent("text_delta", self.output)
        yield _DummyEvent("done")


@pytest.mark.asyncio
async def test_completed_subagents_are_moved_to_archived() -> None:
    """A completed subagent handle should be archived and removed from _runs."""
    mgr = SubagentManager()
    payload = "result payload"

    handle = await mgr.spawn(
        SubagentSpec(task="test task"),
        lambda spec, depth: _MockAgent(output=payload),
    )
    await handle.task

    # Yield control to let done callbacks execute
    await asyncio.sleep(0.01)

    assert handle.status == "done"
    assert handle.result == payload
    assert handle.run_id not in mgr.registry._runs
    assert handle.run_id in mgr.registry._archived
    assert mgr.registry.count_active() == 0
    assert mgr.registry.get(handle.run_id) is handle


@pytest.mark.asyncio
async def test_sequential_subagent_runs_do_not_accumulate_in_runs() -> None:
    """Sequential subagent runs prune _runs so only active handles remain."""
    mgr = SubagentManager()

    for i in range(20):
        handle = await mgr.spawn(
            SubagentSpec(task=f"task {i}"),
            lambda spec, depth: _MockAgent(output=f"output {i}"),
        )
        await handle.task
        await asyncio.sleep(0.001)

    assert len(mgr.registry._runs) == 0
    assert len(mgr.registry._archived) == 20
    assert mgr.registry.count_active() == 0
    assert len(mgr.registry.all_handles()) == 20
    assert mgr.registry.summary() == {"done": 20}


@pytest.mark.asyncio
async def test_errored_subagents_are_archived() -> None:
    """Subagents failing with exceptions should also be archived."""
    mgr = SubagentManager()

    handle = await mgr.spawn(
        SubagentSpec(task="failing task"),
        lambda spec, depth: _MockAgent(fail=True),
    )
    with pytest.raises(RuntimeError, match="agent execution failed"):
        await handle.task
    await asyncio.sleep(0.01)

    assert handle.status == "error"
    assert "agent execution failed" in handle.error
    assert handle.run_id not in mgr.registry._runs
    assert handle.run_id in mgr.registry._archived
    assert mgr.registry.count_active() == 0
    assert mgr.registry.get(handle.run_id) is handle


@pytest.mark.asyncio
async def test_aborted_subagents_are_archived() -> None:
    """Aborted subagents should be archived upon cancellation."""
    mgr = SubagentManager()

    async def _long_turn(task: str) -> Any:
        yield _DummyEvent("text_delta", "starting")
        await asyncio.sleep(10.0)
        yield _DummyEvent("done")

    class _LongAgent:
        async def run_turn(self, task: str) -> Any:
            async for ev in _long_turn(task):
                yield ev

    handle = await mgr.spawn(
        SubagentSpec(task="long task"),
        lambda spec, depth: _LongAgent(),
    )
    assert mgr.registry.count_active() == 1
    assert handle.run_id in mgr.registry._runs

    mgr.registry.abort(handle.run_id)

    # Await cancellation
    with pytest.raises(asyncio.CancelledError):
        await handle.task
    await asyncio.sleep(0.01)

    assert handle.status == "aborted"
    assert handle.run_id not in mgr.registry._runs
    assert handle.run_id in mgr.registry._archived
    assert mgr.registry.count_active() == 0


@pytest.mark.asyncio
async def test_registry_queries_and_serialization_reach_archived(tmp_path) -> None:
    """get_by_status, summary, and save_state should include both runs and archived."""
    registry = SubagentRegistry()

    active_task: asyncio.Task[str] = asyncio.create_task(asyncio.sleep(10.0, result=""))  # type: ignore[arg-type]
    active_handle = SubagentHandle(
        run_id="run-active",
        label="active",
        task=active_task,
        status="running",
    )
    registry.register(active_handle)

    archived_task: asyncio.Task[str] = asyncio.create_task(asyncio.sleep(0, result="done"))  # type: ignore[arg-type]
    archived_handle = SubagentHandle(
        run_id="run-archived",
        label="archived",
        task=archived_task,
        status="done",
        result="some result",
    )
    registry.register(archived_handle)
    registry.archive("run-archived")

    assert registry.get("run-active") is active_handle
    assert registry.get("run-archived") is archived_handle
    assert set(h.run_id for h in registry.all_handles()) == {"run-active", "run-archived"}
    assert registry.get_by_status("running") == [active_handle]
    assert registry.get_by_status("done") == [archived_handle]
    assert registry.summary() == {"running": 1, "done": 1}

    state_path = tmp_path / "subagent_state.json"
    registry.save_state(state_path)

    restored_registry = SubagentRegistry()
    loaded = restored_registry.load_state(state_path)
    assert "run-active" in loaded
    assert "run-archived" in loaded

    active_task.cancel()
    archived_task.cancel()
