"""Test that memory_delete fires the on_memory_write callback (#1806)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos.memory.types import MemorySource
from agentos.tools.builtin.memory_tools import create_memory_tools
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import ToolContext, current_tool_context


class _IndexStore:
    """Minimal store that tracks index_file / remove_file calls."""

    def __init__(self) -> None:
        self.indexed: list[tuple[str, str, MemorySource]] = []
        self.removed: list[str] = []

    async def index_file(self, *, path: str, content: str, source: MemorySource) -> int:
        self.indexed.append((path, content, source))
        return 1

    async def remove_file(self, path: str) -> None:
        self.removed.append(path)


class _FakeRetriever:
    async def search(self, query, opts, *, intent):  # noqa: ANN001, ANN002
        return []


@pytest.mark.asyncio
async def test_memory_delete_fires_on_memory_write_callback(tmp_path: Path) -> None:
    """After a successful delete, on_memory_write must be called so the
    TurnRunner can refresh (clear) its cached memory snapshot.

    Regression test for #1806.
    """
    registry = ToolRegistry()
    store = _IndexStore()
    notified_agents: list[str] = []

    def _on_write(agent_id: str) -> None:
        notified_agents.append(agent_id)

    create_memory_tools(
        stores=store,  # type: ignore[arg-type]
        retrievers=_FakeRetriever(),
        memory_dir=str(tmp_path),
        registry=registry,
        on_memory_write=_on_write,
    )

    # Seed a memory file so memory_delete has something to remove
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir(exist_ok=True)
    target = mem_dir / "notes.md"
    target.write_text("some notes", encoding="utf-8")

    # Set up ToolContext so memory_delete can resolve workspace_dir / agent_id
    ctx = ToolContext(workspace_dir=str(tmp_path), agent_id="test-agent")
    token = current_tool_context.set(ctx)
    try:
        registered = registry.get("memory_delete")
        assert registered is not None
        result = await registered.handler(path="memory/notes.md")
    finally:
        current_tool_context.reset(token)

    # File must be gone
    assert not target.exists()
    # Index must have been told to drop the entry
    assert len(store.removed) == 1
    # Callback must have fired exactly once with the correct agent_id
    assert notified_agents == ["test-agent"]
    assert "Deleted" in result


@pytest.mark.asyncio
async def test_memory_delete_callback_not_called_when_file_missing(tmp_path: Path) -> None:
    """on_memory_write must NOT fire when the file doesn't exist (error path)."""
    registry = ToolRegistry()
    store = _IndexStore()
    notified_agents: list[str] = []

    create_memory_tools(
        stores=store,  # type: ignore[arg-type]
        retrievers=_FakeRetriever(),
        memory_dir=str(tmp_path),
        registry=registry,
        on_memory_write=lambda aid: notified_agents.append(aid),
    )

    ctx = ToolContext(workspace_dir=str(tmp_path), agent_id="test-agent")
    token = current_tool_context.set(ctx)
    try:
        registered = registry.get("memory_delete")
        assert registered is not None
        result = await registered.handler(path="memory/nonexistent.md")
    finally:
        current_tool_context.reset(token)

    assert "not found" in result
    assert notified_agents == []


@pytest.mark.asyncio
async def test_memory_save_fires_on_memory_write_callback(tmp_path: Path) -> None:
    """Verify memory_save fires the callback (control test for parity)."""
    registry = ToolRegistry()
    store = _IndexStore()
    notified_agents: list[str] = []

    create_memory_tools(
        stores=store,  # type: ignore[arg-type]
        retrievers=_FakeRetriever(),
        memory_dir=str(tmp_path),
        registry=registry,
        on_memory_write=lambda aid: notified_agents.append(aid),
    )

    ctx = ToolContext(workspace_dir=str(tmp_path), agent_id="test-agent")
    token = current_tool_context.set(ctx)
    try:
        registered = registry.get("memory_save")
        assert registered is not None
        await registered.handler(content="hello", path="memory/test.md")
    finally:
        current_tool_context.reset(token)

    assert notified_agents == ["test-agent"]
