"""``memory.max_files`` caps memory files, not every Markdown file in the workspace.

With the default ``source="workspace"``, ``memory_save`` writes under the
agent's workspace, which also holds bootstrap files, ``knowledge_base/`` and
anything the agent cloned there. The cap used to count every ``*.md`` under
that tree, so a repository's docs alone refused the first memory file.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.gateway.config import MemoryConfig
from agentos.tools.builtin.memory_tools import create_memory_tools
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import CallerKind, ToolContext, ToolError, current_tool_context


class _Store:
    def __init__(self) -> None:
        self.indexed: list[str] = []

    async def index_file(self, *, path: str, content: str, source: Any) -> int:
        self.indexed.append(path)
        return 1

    async def remove_file(self, path: str) -> None:
        return None

    async def total_size(self) -> int:
        return 0


@pytest.fixture()
def workspace(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "workspace"
    root.mkdir()
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            channel_kind="cli",
            channel_id="cli:test",
            workspace_dir=str(root),
        )
    )
    try:
        yield root
    finally:
        current_tool_context.reset(token)


def _memory_save(workspace: Path, **config: Any) -> tuple[Callable[..., Any], _Store]:
    registry = ToolRegistry()
    store = _Store()
    create_memory_tools(
        stores=store,
        retrievers=SimpleNamespace(),
        memory_dir=str(workspace / "memory"),
        registry=registry,
        memory_source="workspace",
        memory_config=MemoryConfig(**config),
    )
    handler = registry.get("memory_save").handler
    return handler, store


def _write_markdown(directory: Path, count: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (directory / f"page{index}.md").write_text("# page\n", encoding="utf-8")


async def test_a_cloned_repository_does_not_use_up_the_default_cap(workspace: Path) -> None:
    assert MemoryConfig().max_files == 500
    _write_markdown(workspace / "projects" / "some-repo" / "docs", 500)
    save, store = _memory_save(workspace)

    result = await save(content="User prefers metric units.", path="memory/preferences.md")

    assert "memory/preferences.md" in result
    assert (workspace / "memory" / "preferences.md").is_file()
    assert store.indexed == ["memory/preferences.md"]


async def test_knowledge_base_and_bootstrap_files_are_not_memory_files(workspace: Path) -> None:
    _write_markdown(workspace / "knowledge_base", 2)
    for name in ("AGENTS.md", "SOUL.md", "USER.md"):
        (workspace / name).write_text("# bootstrap\n", encoding="utf-8")
    save, _ = _memory_save(workspace, max_files=2)

    await save(content="first note", path="memory/a.md")

    assert (workspace / "memory" / "a.md").is_file()


async def test_hidden_directories_under_memory_do_not_count(workspace: Path) -> None:
    # memory_save refuses dot-prefixed paths, and the sync scanner skips them,
    # so they are not memory files either.
    _write_markdown(workspace / "memory" / ".archive", 3)
    save, _ = _memory_save(workspace, max_files=1)

    await save(content="first note", path="memory/a.md")

    assert (workspace / "memory" / "a.md").is_file()


async def test_the_cap_still_refuses_a_new_file_past_the_limit(workspace: Path) -> None:
    (workspace / "MEMORY.md").write_text("- durable fact\n", encoding="utf-8")
    _write_markdown(workspace / "memory" / "notes", 1)
    save, store = _memory_save(workspace, max_files=2)

    with pytest.raises(ToolError, match=r"max file count reached \(2\)"):
        await save(content="one too many", path="memory/extra.md")

    assert not (workspace / "memory" / "extra.md").exists()
    assert store.indexed == []


async def test_appending_to_an_existing_file_is_allowed_at_the_limit(workspace: Path) -> None:
    _write_markdown(workspace / "memory", 2)
    save, _ = _memory_save(workspace, max_files=2)

    await save(content="more", path="memory/page0.md")

    assert (workspace / "memory" / "page0.md").read_text(encoding="utf-8") == "# page\n\n\nmore"
