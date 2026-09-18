from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agentos.memory.retention import DEFAULT_EXEMPT_FILES, prune_expired_memory_files


def test_default_exempt_files_contains_memory_md_alias():
    assert "MEMORY.md" in DEFAULT_EXEMPT_FILES
    assert "memory.md" in DEFAULT_EXEMPT_FILES


@pytest.mark.asyncio
async def test_prune_expired_memory_files_exempts_lowercase_memory_md(tmp_path: Path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True)

    old_time = time.time() - (60 * 86400)

    lower_mem = memory_dir / "memory.md"
    lower_mem.write_text("lower memory content")
    os.utime(lower_mem, (old_time, old_time))

    expired_note = memory_dir / "expired_note.md"
    expired_note.write_text("expired note content")
    os.utime(expired_note, (old_time, old_time))

    store = AsyncMock()
    store.remove_file = AsyncMock()

    result = await prune_expired_memory_files(
        memory_dir=memory_dir,
        store=store,
        ttl_days=30,
        debounce_seconds=0.0,
    )

    assert result.files_pruned == 1
    assert not expired_note.exists()
    assert lower_mem.exists()
    store.remove_file.assert_awaited_once()
