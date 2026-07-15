"""Curated memory — bounded, file-backed entry stores (MEMORY.md / USER.md).

Adapted from hermes-agent tools/memory_tool.py (MIT, © 2025 Nous Research).
See NOTICE. Two stores per agent:

  - MEMORY.md: agent's personal notes (environment facts, conventions, quirks)
  - USER.md:   what the agent knows about the user (preferences, style)

Entries are §-delimited and the whole store is char-budgeted: when full, the
agent must consolidate (replace/remove) instead of growing unbounded. Files
are the source of truth — writes re-read under an exclusive lock and persist
via atomic replace, so concurrent agent sessions never clobber each other.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

ENTRY_DELIMITER = "\n§\n"

# After this many failed consolidation attempts (overflow / zero-match) in ONE
# turn, stop instructing the model to retry and return a terminal result so a
# fragile replace/add can't loop the turn to budget exhaustion.
_MAX_CONSOLIDATION_FAILURES_PER_TURN = 3


def _scan(content: str) -> str | None:
    from agentos.tools.builtin.memory_tools import _scan_memory_content

    return _scan_memory_content(content)


class CuratedMemoryStore:
    """Bounded curated memory with file persistence. One instance per agent."""

    def __init__(
        self,
        memory_dir: Path,
        memory_char_limit: int = 4000,
        user_char_limit: int = 2000,
    ) -> None:
        self._memory_dir = memory_dir
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        self.memory_entries: list[str] = []
        self.user_entries: list[str] = []
        self._consolidation_failures = 0

    # -- loading ----------------------------------------------------------

    def load_from_disk(self) -> None:
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self.memory_entries = list(dict.fromkeys(self._read_file(self._path_for("memory"))))
        self.user_entries = list(dict.fromkeys(self._read_file(self._path_for("user"))))

    def reset_consolidation_failures(self) -> None:
        self._consolidation_failures = 0

    # -- public state -----------------------------------------------------

    def entries_for(self, target: str) -> list[str]:
        # Return a copy to prevent callers from mutating internal state
        return list(self.user_entries if target == "user" else self.memory_entries)

    # -- mutations --------------------------------------------------------

    def add(self, target: str, content: str) -> dict[str, Any]:
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}
        scan_error = _scan(content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            self._reload_target(target)
            entries = self.entries_for(target)
            limit = self._char_limit(target)
            if content in entries:
                return self._success(target, "Entry already exists (no duplicate added).")
            new_total = len(ENTRY_DELIMITER.join([*entries, content]))
            if new_total > limit:
                current = self._char_count(target)
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"Memory at {current:,}/{limit:,} chars. Adding this entry "
                        f"({len(content)} chars) would exceed the limit. Consolidate now: "
                        f"use 'replace' to merge overlapping entries or 'remove' stale "
                        f"ones (see current_entries), then retry — all in this turn."
                    ),
                    "current_entries": list(entries),
                    "usage": f"{current:,}/{limit:,}",
                })
            entries.append(content)
            self._set_entries(target, entries)
            self._save(target)
        return self._success(target, "Entry added.")

    def replace(self, target: str, old_text: str, new_content: str) -> dict[str, Any]:
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {
                "success": False,
                "error": "new_content cannot be empty. Use 'remove' to delete entries.",
            }
        scan_error = _scan(new_content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            self._reload_target(target)
            entries = self.entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]
            if not matches:
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"No entry matched '{old_text}'. Check current_entries and retry "
                        f"with the exact text of the entry you want to replace."
                    ),
                    "current_entries": list(entries),
                })
            if len({e for _, e in matches}) > 1:
                return {
                    "success": False,
                    "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                    "matches": self._previews([e for _, e in matches]),
                }
            idx = matches[0][0]
            limit = self._char_limit(target)
            test_entries = entries.copy()
            test_entries[idx] = new_content
            new_total = len(ENTRY_DELIMITER.join(test_entries))
            if new_total > limit:
                current = self._char_count(target)
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"Replacement would put memory at {new_total:,}/{limit:,} chars. "
                        f"Shorten the new content or 'remove' stale entries first, then "
                        f"retry — all in this turn."
                    ),
                    "current_entries": list(entries),
                    "usage": f"{current:,}/{limit:,}",
                })
            entries[idx] = new_content
            self._set_entries(target, entries)
            self._save(target)
        return self._success(target, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> dict[str, Any]:
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        with self._file_lock(self._path_for(target)):
            self._reload_target(target)
            entries = self.entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]
            if not matches:
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"No entry matched '{old_text}'. Check current_entries and retry "
                        f"with the exact text of the entry you want to remove."
                    ),
                    "current_entries": list(entries),
                })
            if len({e for _, e in matches}) > 1:
                return {
                    "success": False,
                    "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                    "matches": self._previews([e for _, e in matches]),
                }
            entries.pop(matches[0][0])
            self._set_entries(target, entries)
            self._save(target)
        return self._success(target, "Entry removed.")

    # -- internals ---------------------------------------------------------

    def _path_for(self, target: str) -> Path:
        return self._memory_dir / ("USER.md" if target == "user" else "MEMORY.md")

    def _set_entries(self, target: str, entries: list[str]) -> None:
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def _char_count(self, target: str) -> int:
        entries = self.entries_for(target)
        return len(ENTRY_DELIMITER.join(entries)) if entries else 0

    def _char_limit(self, target: str) -> int:
        return self.user_char_limit if target == "user" else self.memory_char_limit

    def _reload_target(self, target: str) -> None:
        fresh = list(dict.fromkeys(self._read_file(self._path_for(target))))
        self._set_entries(target, fresh)

    def _save(self, target: str) -> None:
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._write_file(self._path_for(target), self.entries_for(target))

    def _consolidation_failure(self, response: dict[str, Any]) -> dict[str, Any]:
        self._consolidation_failures += 1
        if self._consolidation_failures <= _MAX_CONSOLIDATION_FAILURES_PER_TURN:
            return response
        return {
            "success": False,
            "done": True,
            "error": (
                f"Memory consolidation failed {self._consolidation_failures} times this "
                "turn. Stop retrying memory calls — leave memory unchanged and continue "
                "with your reply. The fact can be saved in a later turn."
            ),
        }

    def _success(self, target: str, message: str | None = None) -> dict[str, Any]:
        self._consolidation_failures = 0
        entries = self.entries_for(target)
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
        resp: dict[str, Any] = {
            "success": True,
            "done": True,
            "target": target,
            "usage": f"{pct}% — {current:,}/{limit:,} chars",
            "entry_count": len(entries),
            "current_entries": list(entries),
        }
        if message:
            resp["message"] = message
        resp["note"] = "Write saved. This update is complete — do not repeat it."
        return resp

    @staticmethod
    def _previews(entries: list[str], width: int = 80) -> list[str]:
        return [e[:width] + ("..." if len(e) > width else "") for e in entries]

    @staticmethod
    @contextmanager
    def _file_lock(path: Path) -> Iterator[None]:
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None:  # pragma: no cover - non-POSIX
            yield
            return
        fd = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:  # pragma: no cover
                pass
            fd.close()

    @staticmethod
    def _read_file(path: Path) -> list[str]:
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return []
        if not raw.strip():
            return []
        return [e for e in (part.strip() for part in raw.split(ENTRY_DELIMITER)) if e]

    @staticmethod
    def _write_file(path: Path, entries: list[str]) -> None:
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), suffix=".tmp", prefix=".mem_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:  # pragma: no cover
                pass
            raise
