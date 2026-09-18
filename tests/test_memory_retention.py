from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from agentos.identity.workspace import BOOTSTRAP_FILENAMES
from agentos.memory.retention import DEFAULT_EXEMPT_FILES, prune_expired_memory_files


class FakeStore:
    def __init__(self) -> None:
        self.removed: list[str] = []

    async def remove_file(self, path: str) -> None:
        self.removed.append(path)


def test_default_exempt_files_contains_memory_aliases_and_bootstrap() -> None:
    assert "MEMORY.md" in DEFAULT_EXEMPT_FILES
    assert "memory.md" in DEFAULT_EXEMPT_FILES
    for name in BOOTSTRAP_FILENAMES:
        assert name in DEFAULT_EXEMPT_FILES


@pytest.mark.asyncio
async def test_prune_expired_memory_files_preserves_lowercase_memory_md(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    memory = workspace / "memory"
    memory.mkdir(parents=True)

    lowercase_mem = memory / "memory.md"
    lowercase_mem.write_text("# Lowercase memory\n", encoding="utf-8")

    expired_note = memory / "expired_note.md"
    expired_note.write_text("# Note to prune\n", encoding="utf-8")

    # Backdate mtimes by 100 days
    old_time = time.time() - (100 * 86400)
    os.utime(lowercase_mem, (old_time, old_time))
    os.utime(expired_note, (old_time, old_time))

    store = FakeStore()
    result = await prune_expired_memory_files(
        memory_dir=memory,
        store=store,
        ttl_days=30,
        workspace_dir=workspace,
    )

    assert lowercase_mem.exists(), "memory.md should be exempt from TTL pruning"
    assert not expired_note.exists(), "expired_note.md should have been pruned"
    assert result.files_pruned == 1
    assert "memory/expired_note.md" in store.removed
    assert "memory/memory.md" not in store.removed


@pytest.mark.asyncio
async def test_prune_expired_memory_files_preserves_uppercase_memory_md(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    memory = workspace / "memory"
    memory.mkdir(parents=True)

    uppercase_mem = memory / "MEMORY.md"
    uppercase_mem.write_text("# Uppercase memory\n", encoding="utf-8")

    old_time = time.time() - (100 * 86400)
    os.utime(uppercase_mem, (old_time, old_time))

    store = FakeStore()
    result = await prune_expired_memory_files(
        memory_dir=memory,
        store=store,
        ttl_days=30,
        workspace_dir=workspace,
    )

    assert uppercase_mem.exists()
    assert result.files_pruned == 0
    assert len(store.removed) == 0


@pytest.mark.asyncio
async def test_prune_expired_memory_files_preserves_bootstrap_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    memory = workspace / "memory"
    memory.mkdir(parents=True)

    agents_file = memory / "AGENTS.md"
    agents_file.write_text("# Agents config\n", encoding="utf-8")

    old_time = time.time() - (100 * 86400)
    os.utime(agents_file, (old_time, old_time))

    store = FakeStore()
    result = await prune_expired_memory_files(
        memory_dir=memory,
        store=store,
        ttl_days=30,
        workspace_dir=workspace,
    )

    assert agents_file.exists()
    assert result.files_pruned == 0
