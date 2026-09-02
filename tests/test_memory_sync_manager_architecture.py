from __future__ import annotations

import os
from unittest.mock import Mock

import pytest

from agentos.memory.sync_manager import FileSyncFailures, MemorySyncManager


class NoopStore:
    def __init__(self) -> None:
        self.indexed: list[str] = []
        self.removed: list[str] = []

    async def index_file(
        self,
        *,
        path: str,
        content: str,
        source: object,
        mtime: float | None = None,
    ) -> int:
        self.indexed.append(path)
        return 1

    async def remove_file(self, path: str) -> None:
        self.removed.append(path)
        return None


class MtimeStore(NoopStore):
    def __init__(self) -> None:
        super().__init__()
        self.mtimes: dict[str, float | None] = {}

    async def index_file(
        self,
        *,
        path: str,
        content: str,
        source: object,
        mtime: float | None = None,
    ) -> int:
        self.indexed.append(path)
        self.mtimes[path] = mtime
        return 1


def test_sync_manager_scans_archive_as_curated_memory_subdir(tmp_path):
    workspace = tmp_path / "workspace"
    memory = workspace / "memory"
    archive = memory / "archive"
    hidden = memory / ".private"
    archive.mkdir(parents=True)
    hidden.mkdir(parents=True)
    (workspace / "MEMORY.md").write_text("root\n", encoding="utf-8")
    (memory / "a.md").write_text("a\n", encoding="utf-8")
    (memory / ".hidden.md").write_text("hidden file\n", encoding="utf-8")
    (archive / "x.md").write_text("archive is curated if user-created\n", encoding="utf-8")
    (hidden / "x.md").write_text("hidden\n", encoding="utf-8")

    manager = MemorySyncManager(
        store=NoopStore(),
        workspace_dir=workspace,
        memory_dir=memory,
    )

    assert sorted(manager._scan_files()) == [
        "MEMORY.md",
        "memory/a.md",
        "memory/archive/x.md",
    ]


@pytest.mark.asyncio
async def test_sync_force_rescans_unchanged_memory_sources(tmp_path):
    workspace = tmp_path / "workspace"
    memory = workspace / "memory"
    memory.mkdir(parents=True)
    (workspace / "MEMORY.md").write_text("root\n", encoding="utf-8")
    (memory / "a.md").write_text("a\n", encoding="utf-8")
    store = NoopStore()
    manager = MemorySyncManager(store=store, workspace_dir=workspace, memory_dir=memory)

    await manager.sync(reason="manual")
    first_indexed = list(store.indexed)
    await manager.sync(reason="manual")
    second_indexed = store.indexed[len(first_indexed) :]
    await manager.sync(reason="manual", force=True)
    forced_indexed = store.indexed[len(first_indexed) + len(second_indexed) :]

    assert sorted(first_indexed) == ["MEMORY.md", "memory/a.md"]
    assert second_indexed == []
    assert sorted(forced_indexed) == ["MEMORY.md", "memory/a.md"]


@pytest.mark.asyncio
async def test_sync_force_overrides_search_clean_fast_path(tmp_path):
    workspace = tmp_path / "workspace"
    memory = workspace / "memory"
    memory.mkdir(parents=True)
    (workspace / "MEMORY.md").write_text("root\n", encoding="utf-8")
    store = NoopStore()
    manager = MemorySyncManager(store=store, workspace_dir=workspace, memory_dir=memory)

    await manager.sync(reason="manual")
    first_count = len(store.indexed)
    sync_calls: list[dict[str, object]] = []

    async def fake_do_file_sync(**kwargs: object) -> FileSyncFailures:
        sync_calls.append(kwargs)
        return FileSyncFailures()

    manager._do_file_sync = fake_do_file_sync  # type: ignore[method-assign]
    await manager.sync(reason="search")
    await manager.sync(reason="search:tool")
    await manager.sync(reason="search:control")
    search_count = len(store.indexed)
    await manager.sync(reason="search:tool", force=True)

    assert first_count == 1
    assert search_count == first_count
    assert sync_calls == [{"force": True}]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["search", "search:tool", "search:control"])
@pytest.mark.parametrize("byte_count", [20, 150_000])
async def test_search_consumes_delta_without_session_indexer(
    tmp_path, monkeypatch, reason, byte_count
):
    manager = MemorySyncManager(
        store=NoopStore(), workspace_dir=tmp_path, memory_dir=tmp_path / "memory"
    )
    scan = Mock(wraps=manager._scan_files)
    monkeypatch.setattr(manager, "_scan_files", scan)
    manager.notify_message(byte_count)

    await manager.sync(reason=reason)

    assert scan.call_count == 1
    assert not manager._delta.has_pending()
    assert manager._dirty is False

    for search_reason in ("search", "search:tool", "search:control"):
        await manager.sync(reason=search_reason)
    assert scan.call_count == 1

    manager.notify_message(20)
    await manager.sync(reason=reason)
    assert scan.call_count == 2
    assert not manager._delta.has_pending()

    manager.mark_dirty()
    await manager.sync(reason=reason)
    assert scan.call_count == 3
    assert manager._dirty is False

    await manager.sync(reason=reason, force=True)
    assert scan.call_count == 4


@pytest.mark.asyncio
async def test_sync_passes_source_mtime_for_memory_and_knowledge_base_files(tmp_path):
    workspace = tmp_path / "workspace"
    memory = workspace / "memory"
    memory.mkdir(parents=True)
    knowledge_base = workspace / "knowledge_base"
    knowledge_base.mkdir()
    memory_file = workspace / "MEMORY.md"
    memory_file.write_text("Durable preference.\n", encoding="utf-8")
    document = knowledge_base / "guide.md"
    document.write_text("Deployment runbook.\n", encoding="utf-8")
    expected_mtimes = {
        "MEMORY.md": 1_700_000_000.0,
        "knowledge_base/guide.md": 1_600_000_000.0,
    }
    os.utime(memory_file, (expected_mtimes["MEMORY.md"], expected_mtimes["MEMORY.md"]))
    os.utime(
        document,
        (
            expected_mtimes["knowledge_base/guide.md"],
            expected_mtimes["knowledge_base/guide.md"],
        ),
    )

    store = MtimeStore()
    manager = MemorySyncManager(store=store, workspace_dir=workspace, memory_dir=memory)

    await manager.sync(reason="manual")

    assert sorted(store.indexed) == ["MEMORY.md", "knowledge_base/guide.md"]
    assert store.mtimes == expected_mtimes
