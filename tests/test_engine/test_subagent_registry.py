from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentos.engine.subagent import SubagentHandle, SubagentManager, SubagentRegistry


def test_subagent_registry_save_and_load_state_outside_event_loop(tmp_path: Path) -> None:
    """SubagentRegistry.load_state should not fail when no event loop is running."""
    # Ensure there is no running loop in this sync test
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()

    reg = SubagentRegistry()
    handle = SubagentHandle(
        run_id="run-123",
        label="worker-task",
        task=None,
        status="done",
        result="success",
        error="",
        spawned_at=100.0,
        completed_at=105.0,
    )
    reg.register(handle)

    file_path = tmp_path / "subagents.json"
    reg.save_state(file_path)

    new_reg = SubagentRegistry()
    loaded = new_reg.load_state(file_path)

    assert "run-123" in loaded
    restored = loaded["run-123"]
    assert restored.run_id == "run-123"
    assert restored.label == "worker-task"
    assert restored.status == "orphaned"
    assert restored.result == "success"
    assert restored.task is None

    # Aborting an orphaned handle without an active task must not raise
    assert new_reg.abort("run-123") is True
    assert restored.status == "aborted"


@pytest.mark.asyncio
async def test_subagent_registry_load_state_in_async_context(tmp_path: Path) -> None:
    """Restoring state in an async context marks handles as orphaned with task=None."""
    reg = SubagentRegistry()
    handle = SubagentHandle(
        run_id="run-456",
        label="async-worker",
        task=None,
        status="done",
        result="all good",
    )
    reg.register(handle)

    file_path = tmp_path / "subagents_async.json"
    reg.save_state(file_path)

    new_reg = SubagentRegistry()
    loaded = new_reg.load_state(file_path)

    assert "run-456" in loaded
    restored = loaded["run-456"]
    assert restored.status == "orphaned"
    assert restored.task is None

    mgr = SubagentManager()
    mgr.registry = new_reg
    # wait_all should safely complete and not crash on orphaned/None tasks
    await mgr.wait_all(timeout=0.1)
