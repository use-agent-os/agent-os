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
import time
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
        # Frozen snapshot for system-prompt injection -- set once per
        # load_from_disk() call and never mutated by mid-session writes.
        self._snapshot: dict[str, str] = {}

    # -- loading ----------------------------------------------------------

    def load_from_disk(self) -> None:
        """Load entries from disk and capture a frozen system-prompt snapshot.

        The snapshot is what enters the system prompt. Each entry is scanned
        for injection/exfil threat patterns at snapshot-build time -- any hit
        replaces the entry text in the snapshot with a ``[BLOCKED: ...]``
        placeholder, so a poisoned on-disk memory file (supply chain,
        compromised tool, sister-session write) cannot inject into the
        system prompt. The live ``memory_entries`` / ``user_entries`` lists
        keep the original text so the user can inspect and remove poisoned
        entries via the memory tool.

        Scanning is deterministic from disk bytes, so the snapshot remains
        stable for the entire session (prefix-cache invariant holds).
        """
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self.memory_entries = list(dict.fromkeys(self._read_file(self._path_for("memory"))))
        self.user_entries = list(dict.fromkeys(self._read_file(self._path_for("user"))))

        sanitized_memory = self._sanitize_entries_for_snapshot(self.memory_entries, "MEMORY.md")
        sanitized_user = self._sanitize_entries_for_snapshot(self.user_entries, "USER.md")
        self._snapshot = {
            "memory": self._render_block("memory", sanitized_memory),
            "user": self._render_block("user", sanitized_user),
        }

    def reset_consolidation_failures(self) -> None:
        self._consolidation_failures = 0

    # -- public state -----------------------------------------------------

    def entries_for(self, target: str) -> list[str]:
        # Return a copy to prevent callers from mutating internal state
        return list(self.user_entries if target == "user" else self.memory_entries)

    def usage_for(self, target: str) -> str:
        """Return the ``"{current:,}/{limit:,}"`` char-usage string for *target*."""
        current = self._char_count(target)
        limit = self._char_limit(target)
        return f"{current:,}/{limit:,}"

    def snapshot_block(self, target: str) -> str | None:
        """Return the frozen system-prompt snapshot for *target*.

        Captured at ``load_from_disk()`` time; mid-session writes never
        change it. Returns None when the snapshot is empty (no entries at
        load time).
        """
        block = self._snapshot.get(target, "")
        return block if block else None

    # -- mutations --------------------------------------------------------

    def add(self, target: str, content: str) -> dict[str, Any]:
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}
        scan_error = _scan(content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            self._reload_target(target, skip_drift=True)
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
            bak = self._reload_target(target, skip_drift=False)
            if bak:
                return self._drift_error(self._path_for(target), bak)
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
            bak = self._reload_target(target, skip_drift=False)
            if bak:
                return self._drift_error(self._path_for(target), bak)
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

    def apply_batch(self, target: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
        """Apply a sequence of add/replace/remove ops to one target atomically.

        All operations are validated and applied against the FINAL budget --
        intermediate overflow is irrelevant. This lets the model free space
        (remove/replace) and add new entries in a SINGLE call instead of the
        multi-turn consolidate-then-retry dance that re-sends the whole
        conversation context several times.

        Semantics: all-or-nothing. If any op is malformed, doesn't match, or
        the net result would exceed the char limit, NOTHING is written and an
        error is returned describing the first failure plus the live state.
        """
        if not operations:
            return {"success": False, "error": "operations list is empty."}

        # Scan every add/replace content for injection/exfil BEFORE touching
        # disk -- a single poisoned op rejects the whole batch.
        for i, op in enumerate(operations):
            act = (op or {}).get("action")
            new_content = (op or {}).get("content")
            if act in {"add", "replace"} and new_content:
                scan_error = _scan(new_content)
                if scan_error:
                    return {"success": False, "error": f"Operation {i + 1}: {scan_error}"}

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target, skip_drift=False)
            if bak:
                return self._drift_error(self._path_for(target), bak)

            # Work on a copy; only commit if the whole batch validates.
            working: list[str] = self.entries_for(target)
            limit = self._char_limit(target)

            for i, op in enumerate(operations):
                op = op or {}
                act = op.get("action")
                content = (op.get("content") or "").strip()
                old_text = (op.get("old_text") or "").strip()
                pos = f"Operation {i + 1} ({act or 'unknown'})"

                if act == "add":
                    if not content:
                        return self._batch_error(target, f"{pos}: content is required.")
                    if content in working:
                        continue  # idempotent -- skip duplicate, don't fail the batch
                    working.append(content)

                elif act == "replace":
                    if not old_text:
                        return self._batch_error(target, f"{pos}: old_text is required.")
                    if not content:
                        return self._batch_error(
                            target,
                            f"{pos}: content is required (use action='remove' to delete).",
                        )
                    matches = [j for j, e in enumerate(working) if old_text in e]
                    if not matches:
                        return self._batch_error(
                            target, f"{pos}: no entry matched '{old_text}'."
                        )
                    if len({working[j] for j in matches}) > 1:
                        return self._batch_error(
                            target,
                            f"{pos}: '{old_text}' matched multiple distinct entries -- "
                            f"be more specific.",
                        )
                    working[matches[0]] = content

                elif act == "remove":
                    if not old_text:
                        return self._batch_error(target, f"{pos}: old_text is required.")
                    matches = [j for j, e in enumerate(working) if old_text in e]
                    if not matches:
                        return self._batch_error(
                            target, f"{pos}: no entry matched '{old_text}'."
                        )
                    if len({working[j] for j in matches}) > 1:
                        return self._batch_error(
                            target,
                            f"{pos}: '{old_text}' matched multiple distinct entries -- "
                            f"be more specific.",
                        )
                    working.pop(matches[0])

                else:
                    return self._batch_error(
                        target, f"{pos}: unknown action. Use add, replace, or remove."
                    )

            # Budget check against the FINAL state only.
            new_total = len(ENTRY_DELIMITER.join(working)) if working else 0
            if new_total > limit:
                current = self._char_count(target)
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"After applying all {len(operations)} operations, memory would be "
                        f"at {new_total:,}/{limit:,} chars -- over the limit. Remove or "
                        f"shorten more entries in the same batch (see current_entries "
                        f"below), then retry."
                    ),
                    "current_entries": list(self.entries_for(target)),
                    "usage": f"{current:,}/{limit:,}",
                })

            # Commit.
            self._set_entries(target, working)
            self._save(target)

        return self._success(target, f"Applied {len(operations)} operation(s).")

    def _batch_error(self, target: str, message: str) -> dict[str, Any]:
        """Build a batch-abort error that reports live (uncommitted) state."""
        current = self._char_count(target)
        limit = self._char_limit(target)
        return self._consolidation_failure({
            "success": False,
            "error": message + " No operations were applied (batch is all-or-nothing).",
            "current_entries": list(self.entries_for(target)),
            "usage": f"{current:,}/{limit:,}",
        })

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

    @staticmethod
    def _sanitize_entries_for_snapshot(entries: list[str], filename: str) -> list[str]:
        """Return *entries* with any threat-matching entry replaced by a placeholder.

        Each entry is scanned with ``_scan``. On a hit, the entry is
        replaced in the returned list with a ``[BLOCKED: ...]`` placeholder
        -- the placeholder enters the snapshot, the original entry stays in
        live state for the user to inspect and delete.

        Empty or already-block-marker entries pass through unchanged.
        """
        sanitized: list[str] = []
        for entry in entries:
            if not entry or entry.startswith("[BLOCKED:"):
                sanitized.append(entry)
                continue
            threat = _scan(entry)
            if threat:
                log.warning(
                    "memory_entry_blocked_at_load", filename=filename, threat=threat
                )
                sanitized.append(
                    f"[BLOCKED: {filename} entry contained threat pattern(s): "
                    f"{threat}. Removed from system prompt; use the memory "
                    f"tool remove action to delete the original.]"
                )
            else:
                sanitized.append(entry)
        return sanitized

    def _render_block(self, target: str, entries: list[str]) -> str:
        """Render a system prompt block with header and usage indicator."""
        if not entries:
            return ""

        limit = self._char_limit(target)
        content = ENTRY_DELIMITER.join(entries)
        current = len(content)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        if target == "user":
            header = f"USER PROFILE (who the user is) [{pct}% — {current:,}/{limit:,} chars]"
        else:
            header = f"MEMORY (your personal notes) [{pct}% — {current:,}/{limit:,} chars]"

        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"

    def _reload_target(self, target: str, skip_drift: bool = True) -> str | None:
        """Re-read entries from disk into in-memory state.

        Called under the file lock to get the latest state before mutating.
        Returns the backup path if external drift was detected (the on-disk
        file contains content that wouldn't round-trip through our
        parser/serializer, OR an entry larger than the store's char limit).
        When drift is detected the caller must abort the mutation — flushing
        would discard the un-roundtrippable content. Returns None on clean
        reload.

        When *skip_drift* is True the round-trip / entry-size check is
        bypassed. Used by ``add``, which appends without rewriting, so
        existing content is never clobbered.
        """
        bak = None if skip_drift else self._detect_external_drift(target)
        fresh = list(dict.fromkeys(self._read_file(self._path_for(target))))
        self._set_entries(target, fresh)
        return bak

    def _detect_external_drift(self, target: str) -> str | None:
        """Return a backup-path string if on-disk content shows external drift.

        The memory file is supposed to be a list of small entries the store
        wrote, joined by §. Detect drift via two signals:

        1. Round-trip mismatch — re-parsing and re-serializing the file
           doesn't produce identical bytes (rare; would catch oddly-encoded
           delimiters).
        2. Entry-size overflow — any single parsed entry exceeds the store's
           whole-file char limit. The store budgets the ENTIRE file against
           that limit; no single tool-written entry can exceed it. When we
           see one entry larger than the limit, an external writer (patch
           tool, shell append, manual edit, sister session) appended
           free-form content into what the store will treat as one entry.
           Flushing would then truncate that entry to the model's new
           content, discarding the appended bytes.

        Returns the absolute path of the .bak file when drift was found and
        backed up; returns None when the file looks tool-shaped.
        """
        path = self._path_for(target)
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        if not raw.strip():
            return None

        parsed = [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]
        roundtrip = ENTRY_DELIMITER.join(parsed)

        char_limit = self._char_limit(target)
        max_entry_len = max((len(e) for e in parsed), default=0)

        drift_detected = (raw.strip() != roundtrip) or (max_entry_len > char_limit)
        if not drift_detected:
            return None

        # Drift confirmed — snapshot the file so the operator can recover
        # whatever the external writer added, then return the .bak path so
        # the caller can refuse the mutation.
        ts = int(time.time())
        bak_path = path.with_suffix(path.suffix + f".bak.{ts}")
        try:
            bak_path.write_text(raw, encoding="utf-8")
        except OSError:
            return str(bak_path) + " (BACKUP FAILED — file unchanged on disk)"
        return str(bak_path)

    def _save(self, target: str) -> None:
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._write_file(self._path_for(target), self.entries_for(target))

    @staticmethod
    def _drift_error(path: Path, bak_path: str) -> dict[str, Any]:
        """Build the error dict returned when external drift is detected.

        The on-disk memory file contains content that wouldn't round-trip
        through the store's parser/serializer — flushing would discard the
        appended/edited content from a patch tool, shell append, manual
        edit, or sister-session write. We refuse the mutation, point the
        operator at the .bak.<ts> snapshot we took, and tell them what to do
        next.
        """
        return {
            "success": False,
            "error": (
                f"Refusing to write {path.name}: file on disk has content that "
                f"wouldn't round-trip through the memory tool (likely added by "
                f"the patch tool, a shell append, a manual edit, or a "
                f"concurrent session). A snapshot was saved to {bak_path}. "
                f"Resolve the drift first — either rewrite the file as a clean "
                f"§-delimited list of entries, or move the extra content out — "
                f"then retry."
            ),
            "drift_backup": bak_path,
            "remediation": (
                "Open the .bak file, integrate the missing entries into memory "
                "one at a time via add, then remove or rewrite the original "
                "file to a clean state."
            ),
        }

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
            fcntl.flock(fd, fcntl.LOCK_EX)  # type: ignore[attr-defined]
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
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
