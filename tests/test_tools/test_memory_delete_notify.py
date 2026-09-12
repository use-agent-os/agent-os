from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentos.memory.retrieval import MemoryRetriever
from agentos.memory.store import LongTermMemoryStore
from agentos.tools.builtin.memory_tools import create_memory_tools
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import ToolContext, current_tool_context


@pytest.mark.asyncio
async def test_memory_delete_notifies_on_memory_write(tmp_path: Path) -> None:
    db_path = tmp_path / "test_memory.db"
    store = LongTermMemoryStore(db_path)
    await store.initialize()

    mem_file = tmp_path / "memory" / "notes.md"
    mem_file.parent.mkdir(parents=True, exist_ok=True)
    mem_file.write_text("Old notes to delete", encoding="utf-8")

    retriever = MemoryRetriever(store)
    registry = ToolRegistry()
    mock_on_write = MagicMock()

    create_memory_tools(
        store,
        retriever,
        memory_dir=str(tmp_path / "memory"),
        registry=registry,
        on_memory_write=mock_on_write,
        memory_source="workspace",
    )

    delete_tool = registry.get("memory_delete")
    assert delete_tool is not None

    ctx = ToolContext(workspace_dir=str(tmp_path), agent_id="agent-xyz")
    token = current_tool_context.set(ctx)
    try:
        result = await delete_tool.handler(path="memory/notes.md")
        assert "Deleted memory/notes.md" in result
        assert not mem_file.exists()
        # Verify on_memory_write was invoked with the agent ID
        mock_on_write.assert_called_once_with("agent-xyz")
    finally:
        current_tool_context.reset(token)
        await store.close()
