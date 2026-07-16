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


def _manager(memory_dir: Path | None) -> MemoryManager:
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
    )


@pytest.mark.asyncio
async def test_status_curated_section_reports_entries_and_usage(tmp_path):
    (tmp_path / "MEMORY.md").write_text(
        f"Deploys with make deploy{ENTRY_DELIMITER}Prod region is us-east-1",
        encoding="utf-8",
    )
    (tmp_path / "USER.md").write_text("Name is Ada", encoding="utf-8")
    manager = _manager(tmp_path)

    status = await manager.status()

    assert status["curated"]["memory"]["entries"] == 2
    assert status["curated"]["user"]["entries"] == 1
    assert status["curated"]["memory"]["usage"].endswith("/4,000")
    assert status["curated"]["user"]["usage"].endswith("/2,000")


@pytest.mark.asyncio
async def test_status_curated_section_empty_when_no_entries(tmp_path):
    manager = _manager(tmp_path)

    status = await manager.status()

    assert status["curated"]["memory"]["entries"] == 0
    assert status["curated"]["user"]["entries"] == 0


@pytest.mark.asyncio
async def test_status_omits_curated_section_when_memory_dir_is_none():
    manager = _manager(None)

    status = await manager.status()

    assert status.get("curated") in (None, {})
