"""Unit tests for SubagentRegistry save_state and load_state persistence."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentos.engine.subagent import SubagentHandle, SubagentRegistry


@pytest.mark.asyncio
async def test_subagent_registry_save_and_load_state_utf8(tmp_path: Path) -> None:
    registry = SubagentRegistry()

    async def _dummy() -> str:
        return "done"

    task = asyncio.create_task(_dummy())
    handle = SubagentHandle(
        run_id="run-unicode-1",
        label="researcher-🤖-привет-日本語",
        task=task,
        status="completed",
        result="Success: ✨ 100% completed — “smart quotes”",
        error="",
        spawned_at=1700000000.0,
        completed_at=1700000010.0,
    )
    registry._runs[handle.run_id] = handle

    state_file = tmp_path / "subagent_state.json"
    registry.save_state(state_file)

    # Verify the file was written as valid UTF-8
    content = state_file.read_text(encoding="utf-8")
    assert "run-unicode-1" in content

    # Restore in a fresh registry
    new_registry = SubagentRegistry()
    loaded = new_registry.load_state(state_file)

    assert "run-unicode-1" in loaded
    restored = loaded["run-unicode-1"]
    assert restored.run_id == "run-unicode-1"
    assert restored.label == "researcher-🤖-привет-日本語"
    assert restored.status == "orphaned"
    assert restored.result == "Success: ✨ 100% completed — “smart quotes”"
    assert restored.spawned_at == 1700000000.0
    assert restored.completed_at == 1700000010.0


def test_subagent_registry_load_state_nonexistent_file(tmp_path: Path) -> None:
    registry = SubagentRegistry()
    missing_file = tmp_path / "nonexistent.json"

    loaded = registry.load_state(missing_file)
    assert loaded == {}
