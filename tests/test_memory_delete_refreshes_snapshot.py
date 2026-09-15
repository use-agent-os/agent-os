"""``memory_delete`` notifies ``on_memory_write`` like ``memory_save`` does (#1806).

``TurnRunner`` freezes MEMORY.md into a per-session ``MemorySnapshot`` and only
rebuilds it through the ``on_memory_write`` callback. ``memory_save`` fired it;
``memory_delete`` unlinked the file and dropped it from the vector index but
never notified, so the deleted memories kept being injected into every later
turn of the session.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentos.tools.builtin.memory_tools import create_memory_tools
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import CallerKind, ToolContext, current_tool_context


class _FakeStore:
    def __init__(self) -> None:
        self.removed: list[str] = []

    async def index_file(self, *, path: str, content: str, source) -> int:
        return 1 if content else 0

    async def remove_file(self, path: str) -> None:
        self.removed.append(path)

    async def total_size(self) -> int:
        return 0


@pytest.fixture()
def tools(tmp_path):
    refreshed: list[str] = []
    registry = ToolRegistry()
    store = _FakeStore()
    create_memory_tools(
        stores=store,
        retrievers=SimpleNamespace(),
        memory_dir=str(tmp_path),
        registry=registry,
        on_memory_write=refreshed.append,
    )
    handlers = {name: registry.get(name).handler for name in registry.list_names()}
    return SimpleNamespace(handlers=handlers, refreshed=refreshed, store=store, root=tmp_path)


async def test_memory_delete_notifies_snapshot_refresh(tools) -> None:
    await tools.handlers["memory_save"](content="deployed to prod", path="memory/notes.md")
    assert tools.refreshed == ["main"]

    result = await tools.handlers["memory_delete"](path="memory/notes.md")

    assert result == "Deleted memory/notes.md and removed from index."
    assert not (tools.root / "memory" / "notes.md").exists()
    assert tools.store.removed == ["memory/notes.md"]
    assert tools.refreshed == ["main", "main"]


async def test_memory_delete_of_memory_md_notifies(tools) -> None:
    """MEMORY.md is what the frozen snapshot actually injects, so its removal
    is the case the callback exists for."""
    (tools.root / "MEMORY.md").write_text("- outdated fact\n", encoding="utf-8")

    result = await tools.handlers["memory_delete"](path="MEMORY.md")

    assert result == "Deleted MEMORY.md and removed from index."
    assert tools.refreshed == ["main"]


async def test_memory_delete_notifies_for_the_calling_agent(tools) -> None:
    (tools.root / "memory").mkdir()
    (tools.root / "memory" / "notes.md").write_text("stale\n", encoding="utf-8")
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            channel_kind="cli",
            channel_id="cli:test",
            agent_id="research",
        )
    )
    try:
        await tools.handlers["memory_delete"](path="memory/notes.md")
    finally:
        current_tool_context.reset(token)

    assert tools.refreshed == ["research"]


async def test_memory_delete_does_not_notify_when_nothing_was_deleted(tools) -> None:
    missing = await tools.handlers["memory_delete"](path="memory/absent.md")
    assert missing == "Error: memory/absent.md not found."

    (tools.root / "README.md").write_text("not memory\n", encoding="utf-8")
    rejected = await tools.handlers["memory_delete"](path="README.md")
    assert rejected.startswith("Error: path is not a memory source file.")

    assert tools.refreshed == []
    assert tools.store.removed == []
