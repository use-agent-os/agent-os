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

import errno
import os
import shutil
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

try:  # pragma: no cover - Windows only
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

ENTRY_DELIMITER = "\n§\n"

# After this many failed consolidation attempts (overflow / zero-match) in ONE
# turn, stop instructing the model to retry and return a terminal result so a
# fragile replace/add can't loop the turn to budget exhaustion.
_MAX_CONSOLIDATION_FAILURES_PER_TURN = 3

# Sentinel returned by _reload_target when the memory file exists but could
# not be read (transient lock, permission change, I/O error, corrupt UTF-8).
# It is deliberately NOT a valid .bak path string: every write path checks for
# it by identity before touching disk. Treating an unreadable file as "empty"
# and then flushing would silently erase every stored entry.
_READ_FAILED = "\x00__read_failed__"

# How long a consolidation-failure streak stays "current". The store is cached
# per workspace for the whole process, so failures must expire on their own --
# otherwise three failures spread across three unrelated turns would latch the
# tool into its terminal error state permanently. Comfortably longer than any
# single turn's retry loop, far shorter than a session.
_CONSOLIDATION_FAILURE_WINDOW_S = 300.0


def _scan(content: str) -> str | None:
    from agentos.tools.builtin.memory_tools import _scan_memory_content

    return _scan_memory_content(content)


def _atomic_replace(tmp_path: str | Path, target: Path) -> None:
    """Move *tmp_path* onto *target* atomically, preserving symlinks.

    A plain ``os.replace`` swaps the SYMLINK itself for a regular file, which
    silently detaches deployments that symlink MEMORY.md / USER.md into a
    dotfiles repo or a managed profile package. Resolving the link first makes
    the rename land on the real file so the symlink survives.

    ``EXDEV`` (tmp and target on different filesystems) and ``EBUSY`` (target
    is a bind mount or held open) cannot be renamed across; fall back to a
    copy + fsync so those deployments still get a durable write instead of an
    exception. The copy is not atomic, but the alternative is no write at all.
    """
    target_str = str(target)
    real_path = os.path.realpath(target_str) if os.path.islink(target_str) else target_str
    tmp_str = str(tmp_path)
    try:
        os.replace(tmp_str, real_path)
        return
    except OSError as exc:
        if exc.errno not in (errno.EXDEV, errno.EBUSY):
            raise
        log.debug(
            "curated_memory_atomic_replace_fallback",
            tmp=tmp_str,
            target=real_path,
            errno=errno.errorcode.get(exc.errno, exc.errno),
        )
    shutil.copyfile(tmp_str, real_path)
    try:
        fd = os.open(real_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:  # pragma: no cover - best effort durability
        pass
    try:
        os.unlink(tmp_str)
    except OSError:  # pragma: no cover
        pass


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
        self._first_failure_at = 0.0
        # Targets whose last load_from_disk() could not read the file. Their
        # in-memory entries are NOT the file's contents, so writes must refuse
        # rather than flush emptiness over real data.
        self.load_failed: dict[str, bool] = {}
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

        # An unreadable file is not an empty one. Loading it as [] would put
        # the agent into a silent memory blackout for the whole session --
        # the file still holds every entry on disk, but the snapshot is empty
        # and nothing anywhere says why. Worse, the store then holds [] in
        # memory while disk holds real entries, and `add` (which skips the
        # drift check) would flush that emptiness back over them.
        #
        # Record the failure instead: writes refuse, and the caller can tell
        # "no memory" apart from "could not read memory".
        self.load_failed = {}
        memory_raw = self._read_raw_checked(self._path_for("memory"))
        user_raw = self._read_raw_checked(self._path_for("user"))
        if memory_raw is None:
            self.load_failed["memory"] = True
            log.error(
                "curated_memory_load_failed", target="memory", path=str(self._path_for("memory"))
            )
        if user_raw is None:
            self.load_failed["user"] = True
            log.error("curated_memory_load_failed", target="user", path=str(self._path_for("user")))

        self.memory_entries = list(dict.fromkeys(self._parse_entries(memory_raw or "")))
        self.user_entries = list(dict.fromkeys(self._parse_entries(user_raw or "")))

        sanitized_memory = self._sanitize_entries_for_snapshot(self.memory_entries, "MEMORY.md")
        sanitized_user = self._sanitize_entries_for_snapshot(self.user_entries, "USER.md")
        self._snapshot = {
            "memory": self._render_block("memory", sanitized_memory),
            "user": self._render_block("user", sanitized_user),
        }

    def reset_consolidation_failures(self) -> None:
        """Clear the consolidation-failure streak.

        Callers that know a turn boundary was crossed can call this to reset
        immediately; otherwise the streak expires on its own after
        ``_CONSOLIDATION_FAILURE_WINDOW_S`` (see ``_consolidation_failure``).
        """
        self._consolidation_failures = 0
        self._first_failure_at = 0.0

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
        if self._would_not_round_trip(content):
            return self._delimiter_error(content)

        with self._file_lock(self._path_for(target)):
            reload_signal = self._reload_target(target, skip_drift=True)
            if reload_signal is _READ_FAILED:
                return self._read_failed_error(self._path_for(target))
            entries = self.entries_for(target)
            limit = self._char_limit(target)
            if content in entries:
                return self._success(target, "Entry already exists (no duplicate added).")
            new_total = len(ENTRY_DELIMITER.join([*entries, content]))
            if new_total > limit:
                current = self._char_count(target)
                return self._consolidation_failure(
                    {
                        "success": False,
                        "error": (
                            f"Memory at {current:,}/{limit:,} chars. Adding this entry "
                            f"({len(content)} chars) would exceed the limit. Consolidate now: "
                            f"use 'replace' to merge overlapping entries or 'remove' stale "
                            f"ones (see current_entries), then retry — all in this turn."
                        ),
                        "current_entries": list(entries),
                        "usage": f"{current:,}/{limit:,}",
                    }
                )
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
        if self._would_not_round_trip(new_content):
            return self._delimiter_error(new_content)

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target, skip_drift=False)
            if bak is _READ_FAILED:
                return self._read_failed_error(self._path_for(target))
            if bak:
                return self._drift_error(self._path_for(target), bak)
            entries = self.entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]
            if not matches:
                return self._consolidation_failure(
                    {
                        "success": False,
                        "error": (
                            f"No entry matched '{old_text}'. Check current_entries and retry "
                            f"with the exact text of the entry you want to replace."
                        ),
                        "current_entries": list(entries),
                    }
                )
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
                return self._consolidation_failure(
                    {
                        "success": False,
                        "error": (
                            f"Replacement would put memory at {new_total:,}/{limit:,} chars. "
                            f"Shorten the new content or 'remove' stale entries first, then "
                            f"retry — all in this turn."
                        ),
                        "current_entries": list(entries),
                        "usage": f"{current:,}/{limit:,}",
                    }
                )
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
            if bak is _READ_FAILED:
                return self._read_failed_error(self._path_for(target))
            if bak:
                return self._drift_error(self._path_for(target), bak)
            entries = self.entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]
            if not matches:
                return self._consolidation_failure(
                    {
                        "success": False,
                        "error": (
                            f"No entry matched '{old_text}'. Check current_entries and retry "
                            f"with the exact text of the entry you want to remove."
                        ),
                        "current_entries": list(entries),
                    }
                )
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
            if bak is _READ_FAILED:
                return self._read_failed_error(self._path_for(target))
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
                        return self._batch_error(target, f"{pos}: no entry matched '{old_text}'.")
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
                        return self._batch_error(target, f"{pos}: no entry matched '{old_text}'.")
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
                return self._consolidation_failure(
                    {
                        "success": False,
                        "error": (
                            f"After applying all {len(operations)} operations, memory would be "
                            f"at {new_total:,}/{limit:,} chars -- over the limit. Remove or "
                            f"shorten more entries in the same batch (see current_entries "
                            f"below), then retry."
                        ),
                        "current_entries": list(self.entries_for(target)),
                        "usage": f"{current:,}/{limit:,}",
                    }
                )

            # Commit.
            self._set_entries(target, working)
            self._save(target)

        return self._success(target, f"Applied {len(operations)} operation(s).")

    def _batch_error(self, target: str, message: str) -> dict[str, Any]:
        """Build a batch-abort error that reports live (uncommitted) state."""
        current = self._char_count(target)
        limit = self._char_limit(target)
        return self._consolidation_failure(
            {
                "success": False,
                "error": message + " No operations were applied (batch is all-or-nothing).",
                "current_entries": list(self.entries_for(target)),
                "usage": f"{current:,}/{limit:,}",
            }
        )

    # -- internals ---------------------------------------------------------

    def _path_for(self, target: str) -> Path:
        return self._memory_dir / ("USER.md" if target == "user" else "MEMORY.md")

    def _set_entries(self, target: str, entries: list[str]) -> None:
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def char_count(self, target: str) -> int:
        """Return the total character count of formatted entries for *target*."""
        entries = self.entries_for(target)
        return len(ENTRY_DELIMITER.join(entries)) if entries else 0

    def char_limit(self, target: str) -> int:
        """Return the configured character limit for *target*."""
        return self.user_char_limit if target == "user" else self.memory_char_limit

    def _char_count(self, target: str) -> int:
        return self.char_count(target)

    def _char_limit(self, target: str) -> int:
        return self.char_limit(target)

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
                log.warning("memory_entry_blocked_at_load", filename=filename, threat=threat)
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
        Reads the file EXACTLY ONCE and feeds those bytes to both the drift
        check and the parse, so no external writer can slip between the two
        (a second read would reopen the TOCTOU window this closes).

        Returns:
          - ``_READ_FAILED`` when the file could not be read at all. In-memory
            state is left untouched and the caller MUST abort the mutation.
          - the ``.bak`` path string when external drift was detected.
          - ``None`` on a clean reload.

        When *skip_drift* is True the round-trip / entry-size check is
        bypassed. Used by ``add``, which appends without rewriting, so
        existing content is never clobbered.
        """
        path = self._path_for(target)
        raw = self._read_raw_checked(path)
        if raw is None:
            # Unreadable != empty. Leave in-memory entries alone so a caller
            # that ignores this signal still cannot flush [] over real data.
            return _READ_FAILED
        bak = None if skip_drift else self._detect_external_drift(target, raw)
        fresh = list(dict.fromkeys(self._parse_entries(raw)))
        self._set_entries(target, fresh)
        return bak

    def _detect_external_drift(self, target: str, raw: str) -> str | None:
        """Return a backup-path string if on-disk content shows external drift.

        *raw* is the already-read file content -- callers pass the same bytes
        they will parse, so this check and the parse can never disagree about
        what is on disk.

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
    def _read_failed_error(path: Path) -> dict[str, Any]:
        """Build the error returned when the memory file could not be read.

        Refusing the write is the whole point: the alternative is to treat an
        unreadable file as an empty one and flush, which deletes every entry
        the store already held. The write is dropped, disk is untouched, and
        the model is told to move on rather than retry into the same error.
        """
        return {
            "success": False,
            "done": True,
            "error": (
                f"Refusing to write {path.name}: the existing file could not be "
                f"read (locked by another process, permission change, I/O error, "
                f"or invalid UTF-8). Writing now would overwrite its current "
                f"contents with an empty store and lose every saved entry. "
                f"Memory is unchanged — continue with your reply; the fact can "
                f"be saved in a later turn."
            ),
            "remediation": (
                f"Check that {path} is readable (permissions, encoding) and that "
                f"no other process is holding it open, then retry."
            ),
        }

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
        """Count a failed consolidation and cut the model off after N in a row.

        The budget is scoped to a WINDOW rather than a literal turn: this
        store is cached per workspace for the life of the process, so a plain
        counter would accumulate failures across unrelated turns and then
        refuse every future write forever -- the tool would go permanently
        dead with no way back. Any failure older than the window is stale
        evidence about a turn that already ended, so the count restarts.

        A successful write also resets it (see ``_success``).
        """
        now = time.monotonic()
        if now - self._first_failure_at > _CONSOLIDATION_FAILURE_WINDOW_S:
            self._consolidation_failures = 0
            self._first_failure_at = now
        if self._consolidation_failures == 0:
            self._first_failure_at = now
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
        """Build the terminal success response for a committed write.

        Deliberately does NOT echo the entries list. Handing the model the
        full store right after a successful write invites it to spot "one
        more thing to fix" and re-issue the same operations -- observed as a
        correct batch on call 1 followed by several redundant repeats. The
        entries only appear on the error paths, where the model genuinely
        needs them to decide what to consolidate.
        """
        self._consolidation_failures = 0
        self._first_failure_at = 0.0
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
        """Hold an exclusive lock across a read-modify-write of *path*.

        The lock lives in a sibling ``.lock`` file so the memory file itself
        stays free to be swapped by atomic replace.

        Windows has no ``fcntl``; without the ``msvcrt`` branch the lock was a
        bare ``yield``, so two concurrent sessions on Windows interleaved
        their read-modify-write and one of the writes was lost. Only a
        platform with neither primitive falls through unlocked.
        """
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None and msvcrt is None:  # pragma: no cover - exotic platform
            yield
            return
        fd = open(lock_path, "a+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)  # type: ignore[attr-defined]
            else:  # pragma: no cover - Windows only
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
                else:  # pragma: no cover - Windows only
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            except OSError:  # pragma: no cover
                pass
            fd.close()

    @staticmethod
    def _read_raw_checked(path: Path) -> str | None:
        """Return the file's raw text, or ``None`` when the read FAILED.

        ``None`` means "we could not determine the current contents" -- it is
        NOT the same as "the file is empty". Callers must refuse to write on
        ``None``: treating an unreadable file as ``[]`` and then flushing
        would erase every existing entry (see ``_read_failed_error``).

        A genuinely absent or empty file returns ``""``, which is a known
        state and safe to write against.
        """
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    @classmethod
    def _read_file(cls, path: Path) -> list[str]:
        """Parse *path* into entries. Returns [] for both empty AND unreadable.

        Prefer ``_read_raw_checked`` + ``_parse_entries`` on any write path so
        an unreadable file can be distinguished from an empty one.
        """
        raw = cls._read_raw_checked(path)
        return cls._parse_entries(raw or "")

    #: Probe text for :meth:`_would_not_round_trip`. Any two distinct strings
    #: that are themselves clean entries work; NULs cannot occur in one.
    _ROUND_TRIP_PROBE = ("\x00head", "\x00tail")

    @classmethod
    def _would_not_round_trip(cls, content: str) -> bool:
        """Whether storing *content* as one entry would not survive the read.

        Asked of the real serializer and the real parser rather than by
        pattern-matching the delimiter, because two different shapes corrupt
        the file and only one of them is visible in the entry itself: a line
        that is just ``§`` inside the entry splits it in two, and an entry
        *ending* in ``\n§`` supplies the missing newline to the delimiter that
        follows it and steals the opening of the **next** entry. Joining the
        candidate between two known-clean entries and re-parsing catches both,
        and keeps passing the ``§`` uses that are harmless -- inline
        (``5§ per unit``), indented, or an entry that is a bare ``§``.
        """
        head, tail = cls._ROUND_TRIP_PROBE
        probe = ENTRY_DELIMITER.join([head, content, tail])
        return cls._parse_entries(probe) != [head, content, tail]

    @classmethod
    def _delimiter_error(cls, content: str) -> dict[str, Any]:
        """Refusal for content that cannot be stored as a single entry.

        Rejected rather than rewritten, the same call
        :func:`agentos.env_policy.sanitize_value` makes for a line break in a
        ``.env`` value: silently editing the text would put words in the
        agent's memory that it did not write, and silently splitting it -- what
        happened before this check -- left an entry that ``remove`` could no
        longer match by the text that created it.
        """
        return {
            "success": False,
            "error": (
                "Entry contains a line that is just '§', which is the entry "
                "delimiter, so it would be stored as several entries and could "
                "not be removed or replaced by the text you sent. Reword or drop "
                "that line — a '§' inside a line is fine."
            ),
            "content_preview": content[:200],
        }

    @staticmethod
    def _parse_entries(raw: str) -> list[str]:
        if not raw.strip():
            return []
        return [e for e in (part.strip() for part in raw.split(ENTRY_DELIMITER)) if e]

    @staticmethod
    def _write_file(path: Path, entries: list[str]) -> None:
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".mem_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            _atomic_replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:  # pragma: no cover
                pass
            raise
