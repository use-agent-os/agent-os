"""The single `memory` tool (curated store entry point) + memory_save redirect."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agentos.tools.builtin.memory_tools import create_memory_tools
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import ToolError


class _FakeMemorySaveStore:
    """Minimal store double satisfying memory_save's indexing calls."""

    async def index_file(self, *, path: str, content: str, source) -> int:
        return 1 if content else 0

    async def remove_file(self, path: str) -> None:
        return None

    async def total_size(self) -> int:
        return 0


@pytest.fixture()
def memory_tools_fixture(tmp_path):
    """Build create_memory_tools against a tmp workspace; return name -> handler."""
    registry = ToolRegistry()
    create_memory_tools(
        stores=_FakeMemorySaveStore(),
        retrievers=SimpleNamespace(),
        memory_dir=str(tmp_path),
        registry=registry,
    )
    return {name: registry.get(name).handler for name in registry.list_names()}


async def test_memory_tool_add_and_remove(memory_tools_fixture):
    tools = memory_tools_fixture
    result = json.loads(await tools["memory"](action="add", content="fact A"))
    assert result["success"] is True
    result = json.loads(await tools["memory"](action="remove", old_text="fact A"))
    assert result["success"] is True


async def test_memory_tool_batch_operations(memory_tools_fixture):
    tools = memory_tools_fixture
    result = json.loads(
        await tools["memory"](
            target="memory",
            operations=[{"action": "add", "content": "a"}, {"action": "add", "content": "b"}],
        )
    )
    assert result["success"] is True
    assert result["entry_count"] == 2


async def test_memory_tool_null_target_defaults_to_memory(memory_tools_fixture):
    tools = memory_tools_fixture
    result = json.loads(await tools["memory"](action="add", target=None, content="x"))
    assert result["success"] is True
    assert result["target"] == "memory"


async def test_memory_tool_invalid_target_returns_error(memory_tools_fixture):
    tools = memory_tools_fixture
    result = json.loads(await tools["memory"](action="add", target="bogus", content="x"))
    assert result["success"] is False
    assert "bogus" in result["error"]


async def test_memory_tool_missing_old_text_returns_inventory(memory_tools_fixture):
    tools = memory_tools_fixture
    await tools["memory"](action="add", content="only entry")
    result = json.loads(await tools["memory"](action="replace", content="new"))
    assert result["success"] is False
    assert result["current_entries"] == ["only entry"]


async def test_memory_tool_missing_old_text_on_remove_returns_inventory(memory_tools_fixture):
    tools = memory_tools_fixture
    await tools["memory"](action="add", content="only entry")
    result = json.loads(await tools["memory"](action="remove"))
    assert result["success"] is False
    assert result["current_entries"] == ["only entry"]


async def test_memory_tool_unknown_action_returns_error(memory_tools_fixture):
    tools = memory_tools_fixture
    result = json.loads(await tools["memory"](action="bogus", content="x"))
    assert result["success"] is False
    assert "bogus" in result["error"]


async def test_memory_tool_add_requires_content(memory_tools_fixture):
    tools = memory_tools_fixture
    result = json.loads(await tools["memory"](action="add"))
    assert result["success"] is False


async def test_memory_tool_operations_not_a_list_returns_error(memory_tools_fixture):
    tools = memory_tools_fixture
    result = json.loads(await tools["memory"](action="add", operations="nope"))
    assert result["success"] is False


async def test_memory_tool_user_target_writes_user_md(memory_tools_fixture, tmp_path):
    tools = memory_tools_fixture
    result = json.loads(await tools["memory"](action="add", target="user", content="Name: Key"))
    assert result["success"] is True
    assert result["target"] == "user"
    assert "Name: Key" in (tmp_path / "USER.md").read_text(encoding="utf-8")


async def test_memory_save_rejects_memory_md(memory_tools_fixture):
    tools = memory_tools_fixture
    with pytest.raises(ToolError, match="managed by the `memory` tool"):
        await tools["memory_save"](path="MEMORY.md", content="x", mode="replace")


async def test_memory_save_rejects_memory_md_path_variants(memory_tools_fixture):
    tools = memory_tools_fixture
    # "./MEMORY.md" normalizes to the same target as "MEMORY.md" and must be
    # rejected identically -- not silently accepted via path spelling.
    with pytest.raises(ToolError, match="managed by the `memory` tool"):
        await tools["memory_save"](path="./MEMORY.md", content="x", mode="append")


async def test_memory_save_still_accepts_memory_notes(memory_tools_fixture, tmp_path):
    tools = memory_tools_fixture
    result = await tools["memory_save"](path="memory/notes.md", content="daily note")
    assert "Saved to memory/notes.md" in result
    assert "daily note" in (tmp_path / "memory" / "notes.md").read_text(encoding="utf-8")
