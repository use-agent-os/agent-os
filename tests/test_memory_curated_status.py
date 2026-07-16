"""MemoryManager.status() surfaces curated MEMORY.md / USER.md usage."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentos.memory.curated import ENTRY_DELIMITER
from agentos.memory.manager import MemoryManager


class _FakeStore:
    async def file_count(self) -> int:
        return 0

    async def chunk_count(self) -> int:
        return 0

    async def total_size(self) -> int:
        return 0

    async def source_counts(self) -> dict:
        return {}


def _manager(
    memory_dir: Path | None, *, workspace_dir: Path | None = None
) -> MemoryManager:
    return MemoryManager(
        agent_id="main",
        db_path=Path("db.sqlite"),
        store=_FakeStore(),
        sync_manager=SimpleNamespace(),
        retriever=SimpleNamespace(),
        turn_capture=SimpleNamespace(),
        memory_config=SimpleNamespace(
            curated_memory_char_limit=4000,
            curated_user_char_limit=2000,
        ),
        memory_dir=memory_dir,
        workspace_dir=workspace_dir,
    )


@pytest.mark.asyncio
async def test_status_curated_section_reports_entries_and_usage(tmp_path):
    (tmp_path / "MEMORY.md").write_text(
        f"Deploys with make deploy{ENTRY_DELIMITER}Prod region is us-east-1",
        encoding="utf-8",
    )
    (tmp_path / "USER.md").write_text("Name is Ada", encoding="utf-8")
    manager = _manager(tmp_path, workspace_dir=tmp_path)

    status = await manager.status()

    assert status["curated"]["memory"]["entries"] == 2
    assert status["curated"]["user"]["entries"] == 1
    assert status["curated"]["memory"]["usage"].endswith("/4,000")
    assert status["curated"]["user"]["usage"].endswith("/2,000")


@pytest.mark.asyncio
async def test_status_curated_section_empty_when_no_entries(tmp_path):
    manager = _manager(tmp_path, workspace_dir=tmp_path)

    status = await manager.status()

    assert status["curated"]["memory"]["entries"] == 0
    assert status["curated"]["user"]["entries"] == 0


@pytest.mark.asyncio
async def test_status_omits_curated_section_when_memory_dir_is_none():
    manager = _manager(None)

    status = await manager.status()

    assert status.get("curated") in (None, {})


@pytest.mark.asyncio
async def test_status_curated_section_reads_workspace_root_not_memory_subdir(tmp_path):
    """Regression test for the production directory-mismatch bug.

    ``build_memory_managers`` always constructs ``MemoryManager`` with
    ``memory_dir=<workspace>/memory`` (the daily-notes/turn-capture
    subfolder) and ``workspace_dir=<workspace>`` (the root where the
    ``memory`` tool and runtime injection actually read/write MEMORY.md /
    USER.md -- see ``_curated_store_for`` in
    ``tools/builtin/memory_tools.py``). Status must count curated entries
    from the workspace root, not from the ``memory/`` subfolder, or it will
    always report 0 entries in production even though the agent has curated
    memory.
    """
    workspace_root = tmp_path
    memory_subdir = workspace_root / "memory"
    memory_subdir.mkdir()

    (workspace_root / "MEMORY.md").write_text(
        f"Deploys with make deploy{ENTRY_DELIMITER}Prod region is us-east-1",
        encoding="utf-8",
    )
    (workspace_root / "USER.md").write_text("Name is Ada", encoding="utf-8")

    manager = _manager(memory_subdir, workspace_dir=workspace_root)

    status = await manager.status()

    assert status["curated"]["memory"]["entries"] == 2
    assert status["curated"]["user"]["entries"] == 1
    assert status["curated"]["memory"]["usage"].endswith("/4,000")
    assert status["curated"]["user"]["usage"].endswith("/2,000")
    # Sanity check: the memory/ subdir must remain untouched by curated I/O.
    assert not (memory_subdir / "MEMORY.md").exists()
    assert not (memory_subdir / "USER.md").exists()
