from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from agentos.memory.retention import DEFAULT_EXEMPT_FILES, prune_expired_memory_files


class _FakeStore:
    def __init__(self) -> None:
        self.removed: list[str] = []

    async def remove_file(self, store_key: str) -> None:
        self.removed.append(store_key)


def _age_file(path: Path, days: int) -> None:
    old = time.time() - (days * 86400) - 60
    os.utime(path, (old, old))


def _run_prune(memory_dir: Path, store: _FakeStore, *, ttl_days: int = 30) -> object:
    return asyncio.run(
        prune_expired_memory_files(memory_dir=memory_dir, store=store, ttl_days=ttl_days)
    )


def test_default_exempt_files_includes_lowercase_memory_md() -> None:
    """The module's own docstring promises MEMORY.md AND memory.md aliases
    are never deleted by TTL; the constant must actually list both."""
    assert "memory.md" in DEFAULT_EXEMPT_FILES
    assert "MEMORY.md" in DEFAULT_EXEMPT_FILES


def test_lowercase_memory_md_survives_the_ttl_sweep(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    target = memory_dir / "memory.md"
    target.write_text("curated notes", encoding="utf-8")
    _age_file(target, days=400)

    store = _FakeStore()
    result = _run_prune(memory_dir, store)

    assert target.exists()
    assert result.files_pruned == 0
    assert store.removed == []


def test_uppercase_memory_md_still_survives_the_ttl_sweep(tmp_path: Path) -> None:
    """Guard: the existing exemption must keep working after adding the alias."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    target = memory_dir / "MEMORY.md"
    target.write_text("curated notes", encoding="utf-8")
    _age_file(target, days=400)

    store = _FakeStore()
    result = _run_prune(memory_dir, store)

    assert target.exists()
    assert result.files_pruned == 0


def test_an_ordinary_expired_note_is_still_pruned(tmp_path: Path) -> None:
    """Positive control through the same public entry point: an ordinary,
    non-curated file older than the TTL must still be removed, proving the
    exemption list isn't just swallowing every file."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    target = memory_dir / "2024-01-01-notes.md"
    target.write_text("old note", encoding="utf-8")
    _age_file(target, days=400)

    store = _FakeStore()
    result = _run_prune(memory_dir, store)

    assert not target.exists()
    assert result.files_pruned == 1
    assert store.removed == ["memory/2024-01-01-notes.md"]
