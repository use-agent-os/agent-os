"""Memory lineage: where curated entries and note blocks came from, and forget.

Origins are recorded at the write (see :class:`~agentos.memory.evidence.
MemoryEvidenceStore`), keyed by the sha256 of the item's text:

* a **curated entry** is the entry text exactly as ``CuratedMemoryStore``
  stores it (stripped, one ``§``-delimited entry);
* a **note block** is what one ``memory_save`` call put into its note file, as
  a reader of the file sees it (newlines normalized the way Python's text mode
  reads them), *without* the ``"\\n\\n"`` separator an append writes before it.
  In ``replace`` mode, and when an append creates the file, the block is the
  whole file. Its length is stored with the hash so the block can be found
  again by hash alone.

A note block is "still present" when that exact text sits in the file as a
complete block: it starts the file or follows a blank line, and it ends the
file or is followed by a blank line. Text the user extended, or that now sits
inside a larger paragraph, is no longer the block that was written.

Forget removes a note block's copies only when every complete copy of that
exact text in the note is attributable to the selected sessions: no origin
from another session has the same text, there are no more complete copies
than the selected sessions' recorded writes of it, and no copy overlaps a
block another session wrote. Anything else is reported as ``unmatched``
(``changed`` or ``ambiguous``) and left in place; forget never guesses whose
copy is whose. Removing a block also removes one adjacent separator, so
appending a block and then removing it restores the file byte for byte.

Forget (:func:`plan_forget` / :func:`apply_forget`) selects sessions by full
session id or full session key and removes, whole, every item with an origin
from them. Items without an origin are "untracked" and never removed.

This module must not import ``agentos.session`` at module scope (the memory
package stays importable without it); session lookups are injected.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import structlog

from .evidence import (
    CURATED_TARGETS,
    MemoryEvidenceStore,
    Origin,
    SessionRef,
    SessionSelection,
    canonical_text,
)
from .evidence import item_hash as _item_hash

log = structlog.get_logger(__name__)

#: Fixed statement carried by every forget report.
FORGET_NOT_COVERED: Final[str] = (
    "Forget removes only what lineage attributes to the selected sessions. It does not "
    "remove session transcripts (use `agentos sessions delete`), turn-capture files, "
    "compaction checkpoints, or untracked text: curated entries and note text written "
    "before lineage existed, edited by hand since, or written by other tools."
)

CURATED_FILES: Final[Mapping[str, str]] = {"memory": "MEMORY.md", "user": "USER.md"}


# ---------------------------------------------------------------------------
# Item identity
# ---------------------------------------------------------------------------


def normalize_newlines(text: str) -> str:
    """Return *text* as Python's text mode reads it back (CRLF and CR become LF)."""
    return canonical_text(text)


def note_block_written(
    *,
    content: str,
    previous_text: str | None,
    written_text: str,
    mode: str,
) -> str:
    """Return the block one ``memory_save`` write put on disk.

    ``previous_text`` is the file as read before the write (``None`` when it
    did not exist) and ``written_text`` the file as read right after it. An
    append to an existing file wrote ``"\\n\\n" + content``, so the block is
    what follows that prefix; otherwise the write produced the whole file.
    Falls back to the normalized *content* when the file does not have the
    expected shape (another writer got in between).
    """
    if mode == "replace" or previous_text is None:
        return written_text
    prefix = previous_text + "\n\n"
    if written_text.startswith(prefix):
        return written_text[len(prefix) :]
    return normalize_newlines(content)


def find_note_block(text: str, item_hash: str, item_len: int) -> list[int]:
    """Start offsets of non-overlapping complete occurrences of a block in *text*.

    The block is identified by its hash and length, in canonical text
    (:func:`normalize_newlines`). An occurrence counts only as a complete
    block: it starts the text or follows a blank line, and it ends the text
    or is followed by a blank line (trailing whitespace at the end of the text
    is fine). A copy the user extended with another line is not one.
    """
    size = len(text)
    if item_len <= 0 or item_len > size:
        return []
    hits: list[int] = []
    start = 0  # always a line start
    while start + item_len <= size:
        end = start + item_len
        if (
            _starts_block(text, start)
            and _ends_block(text, end)
            and _item_hash(text[start:end]) == item_hash
        ):
            hits.append(start)
            if end == size:
                break
            # Next line start at or after the block's end.
            start = end if text[end - 1] == "\n" else end + 1
            continue
        newline = text.find("\n", start)
        if newline < 0:
            break
        start = newline + 1
    return hits


def _blank_line_at(text: str, start: int) -> bool:
    end = text.find("\n", start)
    return not text[start : len(text) if end < 0 else end].strip()


def _starts_block(text: str, start: int) -> bool:
    """*start* (a line start) begins the text or follows a blank line."""
    if start == 0:
        return True
    previous = text.rfind("\n", 0, start - 1) + 1
    return _blank_line_at(text[: start - 1], previous)


def _ends_block(text: str, end: int) -> bool:
    """*end* closes the text or the line before a blank line (or the text's
    trailing whitespace)."""
    size = len(text)
    if end == size or not text[end:].strip():
        return True
    if text[end - 1] == "\n":  # the block's own text ends with a line break
        return _blank_line_at(text, end)
    return text[end] == "\n" and _blank_line_at(text, end + 1)


def remove_spans(text: str, spans: Iterable[tuple[int, int]]) -> str:
    """Remove ``(start, end)`` spans from *text*, each with one adjacent separator.

    *text* may hold any line endings; the spans are offsets into its
    canonical form (:func:`normalize_newlines`), where blocks are found.
    Only the removed characters change: every other byte of *text*, its line
    endings included, is kept as it was.

    A blank-line separator (``"\\n\\n"``) before the span is preferred, then
    one after it, then a single newline on either side. Spans must not overlap.
    """
    norm, pieces = _canonical_pieces(text)
    for start, end in sorted(spans, reverse=True):
        a, b = start, end
        if norm[:a].endswith("\n\n"):
            a -= 2
        elif norm[b:].startswith("\n\n"):
            b += 2
        elif norm[:a].endswith("\n"):
            a -= 1
        elif norm[b:].startswith("\n"):
            b += 1
        norm = norm[:a] + norm[b:]
        del pieces[a:b]
    return "".join(pieces)


def _canonical_pieces(text: str) -> tuple[str, list[str]]:
    """*text*'s canonical form and, per canonical character, the original
    characters it stands for (``"\\r\\n"`` for a CRLF line break)."""
    pieces: list[str] = []
    i, size = 0, len(text)
    while i < size:
        if text[i] == "\r" and i + 1 < size and text[i + 1] == "\n":
            pieces.append("\r\n")
            i += 2
        else:
            pieces.append(text[i])
            i += 1
    return canonical_text(text), pieces


# ---------------------------------------------------------------------------
# Recording origins
# ---------------------------------------------------------------------------


def requested_curated_adds(
    *,
    action: str | None = None,
    content: str | None = None,
    operations: Sequence[Any] | None = None,
) -> list[str]:
    """The entry texts a ``memory`` call asked to add, stripped as the store strips them."""
    texts: list[str] = []
    if operations:
        for op in operations:
            if isinstance(op, dict) and op.get("action") == "add":
                value = op.get("content")
                if isinstance(value, str):
                    texts.append(value.strip())
    elif action == "add" and isinstance(content, str):
        texts.append(content.strip())
    return [t for t in dict.fromkeys(texts) if t]


def duplicate_curated_adds(
    requested: Iterable[str], changes: Sequence[Mapping[str, str]], entries_after: Iterable[str]
) -> list[str]:
    """Requested adds the store skipped because the entry already existed.

    The store records nothing in ``changes`` for a duplicate add, so a
    duplicate is an add that produced no change record and whose text is in
    the file after the write.
    """
    written = {c.get("entry") for c in changes if c.get("action") in {"add", "replace"}}
    present = set(entries_after)
    return [t for t in requested if t not in written and t in present]


async def record_curated_changes(
    evidence: MemoryEvidenceStore,
    *,
    target: str,
    changes: Sequence[Mapping[str, str]],
    writer: str,
    session: SessionRef | None = None,
    entries_after: Iterable[str] = (),
    duplicate_adds: Iterable[str] = (),
) -> None:
    """Update origins for one committed curated write.

    ``changes`` is the list the store filled for the write. An ``add`` gets
    an origin for ``session``; a ``replace`` carries the old entry's origins
    to the new entry and adds ``session``; a ``remove`` (and a replace) drops
    the old entry's origins once that text is no longer in the file.
    ``duplicate_adds`` (see :func:`duplicate_curated_adds`) adds ``session``
    to an existing entry's origins.
    """
    present = set(entries_after)
    for change in changes:
        action = change.get("action")
        if action == "add":
            entry = change.get("entry") or ""
            await _add_curated_origin(evidence, target, entry, writer, session)
        elif action == "replace":
            old, new = change.get("old_entry") or "", change.get("entry") or ""
            await evidence.copy_origins(
                kind="curated",
                target=target,
                from_hash=_item_hash(old),
                to_hash=_item_hash(new),
                to_len=len(new),
            )
            await _add_curated_origin(evidence, target, new, writer, session)
            if old not in present:
                await evidence.delete_origins("curated", target, _item_hash(old))
        elif action == "remove":
            old = change.get("old_entry") or ""
            if old not in present:
                await evidence.delete_origins("curated", target, _item_hash(old))
    for entry in duplicate_adds:
        if entry in present:
            await _add_curated_origin(evidence, target, entry, writer, session)


async def _add_curated_origin(
    evidence: MemoryEvidenceStore,
    target: str,
    entry: str,
    writer: str,
    session: SessionRef | None,
) -> None:
    if entry:
        await evidence.add_origin(
            kind="curated",
            target=target,
            item_hash=_item_hash(entry),
            item_len=len(entry),
            writer=writer,
            session=session,
        )


async def record_note_write(
    evidence: MemoryEvidenceStore,
    *,
    path: str,
    block: str,
    writer: str,
    session: SessionRef | None = None,
) -> None:
    """Record that *session* wrote *block* (see :func:`note_block_written`) to *path*."""
    if block:
        await evidence.add_origin(
            kind="note",
            target=path,
            item_hash=_item_hash(block),
            item_len=len(block),
            writer=writer,
            session=session,
            count_write=True,
        )


async def curated_lineage_counts(
    evidence: MemoryEvidenceStore, store: Any
) -> dict[str, dict[str, int]]:
    """Tracked / untracked entry counts per curated target of a loaded store."""
    counts: dict[str, dict[str, int]] = {}
    for target in CURATED_TARGETS:
        entries = store.entries_for(target)
        tracked_hashes = await evidence.tracked_item_hashes("curated", target)
        tracked = sum(1 for entry in entries if _item_hash(entry) in tracked_hashes)
        counts[target] = {"tracked": tracked, "untracked": len(entries) - tracked}
    return counts


# ---------------------------------------------------------------------------
# Session resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionMatch:
    """A session found in session storage for a selector (``matched_by`` is
    ``session_key`` or ``session_id``). ``agent_id`` must be normalized."""

    session_key: str
    session_id: str
    agent_id: str
    matched_by: str


SessionLookup = Callable[[str], Awaitable[list[SessionMatch]]]


@dataclass(frozen=True)
class ResolvedSession:
    selector: str
    matched_by: str  # "session_key" | "session_id"
    session_key: str
    session_ids: tuple[str, ...]
    already_forgotten: bool = False

    def wire(self) -> dict[str, Any]:
        return {
            "selector": self.selector,
            "matchedBy": self.matched_by,
            "sessionKey": self.session_key,
            "sessionIds": list(self.session_ids),
            "alreadyForgotten": self.already_forgotten,
        }


@dataclass(frozen=True)
class UnresolvedSelector:
    selector: str
    reason: str  # "empty" | "not_found" | "other_agent"
    detail: str = ""

    def wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"selector": self.selector, "reason": self.reason}
        if self.detail:
            payload["detail"] = self.detail
        return payload


async def resolve_sessions(
    selectors: Iterable[str],
    *,
    agent_id: str,
    evidence: MemoryEvidenceStore,
    lookup: SessionLookup | None = None,
) -> tuple[list[ResolvedSession], list[UnresolvedSelector]]:
    """Resolve forget selectors to sessions of *agent_id*.

    A selector must equal a session key or a session id exactly; prefixes
    never match. Sessions are looked up in session storage (``lookup``) and
    in this agent's evidence tables, so a deleted session can still be
    forgotten. A session that storage places under another agent does not
    resolve.
    """
    resolved: list[ResolvedSession] = []
    unresolved: list[UnresolvedSelector] = []
    seen: set[str] = set()
    for raw in selectors:
        selector = str(raw or "").strip()
        if not selector:
            unresolved.append(UnresolvedSelector(selector=str(raw or ""), reason="empty"))
            continue
        if selector in seen:
            continue
        seen.add(selector)
        matches = await lookup(selector) if lookup is not None else []
        own = [m for m in matches if m.agent_id == agent_id]
        foreign = sorted({m.agent_id for m in matches if m.agent_id != agent_id})
        known = await evidence.known_sessions(selector)

        key_matches = [m for m in own if m.matched_by == "session_key"]
        if key_matches or any(ref.session_key == selector for ref in known):
            key = key_matches[0].session_key if key_matches else selector
            ids = {m.session_id for m in key_matches} | {
                ref.session_id for ref in known if ref.session_key in {key, selector}
            }
            ids.discard("")
            resolved.append(
                ResolvedSession(
                    selector=selector,
                    matched_by="session_key",
                    session_key=key,
                    session_ids=tuple(sorted(ids)),
                    already_forgotten=await evidence.is_session_forgotten(session_key=key),
                )
            )
            continue

        id_matches = [m for m in own if m.matched_by == "session_id"]
        if id_matches or any(ref.session_id == selector for ref in known):
            keys = [m.session_key for m in id_matches] + [
                ref.session_key for ref in known if ref.session_id == selector and ref.session_key
            ]
            key = keys[0] if keys else ""
            resolved.append(
                ResolvedSession(
                    selector=selector,
                    matched_by="session_id",
                    session_key=key,
                    session_ids=(selector,),
                    already_forgotten=await evidence.is_session_forgotten(
                        session_id=selector, session_key=""
                    ),
                )
            )
            continue

        if foreign:
            unresolved.append(
                UnresolvedSelector(
                    selector=selector,
                    reason="other_agent",
                    detail=f"belongs to agent {', '.join(foreign)}",
                )
            )
        else:
            unresolved.append(UnresolvedSelector(selector=selector, reason="not_found"))
    return resolved, unresolved


def selection_for(sessions: Iterable[ResolvedSession]) -> SessionSelection:
    """Rows from a key-resolved session match by key; every resolved id matches."""
    sessions = list(sessions)
    return SessionSelection.of(
        keys=[s.session_key for s in sessions if s.matched_by == "session_key"],
        ids=[sid for s in sessions for sid in s.session_ids],
    )


# ---------------------------------------------------------------------------
# Forget: plan
# ---------------------------------------------------------------------------


@dataclass
class CuratedRemoval:
    target: str
    entry: str
    item_hash: str
    other_origins: list[Origin] = field(default_factory=list)

    @property
    def mixed(self) -> bool:
        return bool(self.other_origins)


@dataclass
class NoteRemoval:
    path: str
    item_hash: str
    item_len: int
    occurrences: int


@dataclass
class NoteUnmatched:
    path: str
    item_hash: str
    # "changed" | "ambiguous" | "file_missing" | "unreadable" | "invalid_path"
    reason: str
    # For "ambiguous": "other_sessions" | "untracked_copies" | "overlaps_other_block".
    detail: str = ""


@dataclass
class ForgetPlan:
    """What a forget would do, computed from current state; changes nothing."""

    agent_id: str
    sessions: list[ResolvedSession]
    selection: SessionSelection
    curated: list[CuratedRemoval] = field(default_factory=list)
    notes: list[NoteRemoval] = field(default_factory=list)
    unmatched: list[NoteUnmatched] = field(default_factory=list)
    stale_origin_ids: list[int] = field(default_factory=list)
    session_documents: list[str] = field(default_factory=list)
    untracked: dict[str, int] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)


def _workspace_root(manager: Any) -> Path | None:
    root = getattr(manager, "workspace_dir", None)
    if root is None:
        root = getattr(getattr(manager, "turn_capture", None), "_workspace_dir", None)
    return Path(root) if root is not None else None


def _note_file(root: Path, rel_path: str) -> Path | None:
    from .source_paths import is_memory_source_path

    if not is_memory_source_path(rel_path) or Path(rel_path).parts == ("MEMORY.md",):
        return None
    candidate = root / rel_path
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return candidate


def _session_document_path(agent_id: str, session_id: str) -> str:
    from .session_source import session_source_path

    return session_source_path(SimpleNamespace(agent_id=agent_id, session_id=session_id))


def _read_note(path: Path) -> tuple[str | None, str | None]:
    """``(text as stored, None)`` or ``(None, reason)``; line endings untranslated."""
    if not path.exists():
        return None, "file_missing"
    try:
        return path.read_bytes().decode("utf-8"), None
    except (OSError, UnicodeDecodeError):
        return None, "unreadable"


NoteIdent = tuple[str, int]  # (item hash, item length)


def _group_note_origins(origins: Iterable[Origin]) -> dict[NoteIdent, list[Origin]]:
    grouped: dict[NoteIdent, list[Origin]] = {}
    for origin in origins:
        grouped.setdefault((origin.item_hash, origin.item_len), []).append(origin)
    return grouped


def _note_ownership(
    text: str,
    origins: Mapping[NoteIdent, list[Origin]],
    selection: SessionSelection,
) -> tuple[dict[NoteIdent, list[tuple[int, int]]], dict[NoteIdent, tuple[str, str]]]:
    """Which selected note blocks of one note may go, and why the rest may not.

    *text* is the note's canonical text and *origins* every origin recorded
    for the note. Returns the removable blocks with the spans of their
    copies, and ``(reason, detail)`` for each selected block that stays (see
    the module docstring for the rule).
    """

    def selected(origin: Origin) -> bool:
        return selection.matches(session_key=origin.session_key, session_id=origin.session_id)

    copies = {
        ident: [(start, start + ident[1]) for start in find_note_block(text, *ident)]
        for ident in origins
    }
    foreign = [
        span
        for ident, rows in origins.items()
        if not all(selected(o) for o in rows)
        for span in copies[ident]
    ]
    removable: dict[NoteIdent, list[tuple[int, int]]] = {}
    kept: dict[NoteIdent, tuple[str, str]] = {}
    for ident, rows in origins.items():
        mine = [o for o in rows if selected(o)]
        if not mine:
            continue
        spans = copies[ident]
        if not spans:
            kept[ident] = ("changed", "")
        elif len(mine) < len(rows):
            kept[ident] = ("ambiguous", "other_sessions")
        elif len(spans) > sum(max(1, o.writes) for o in mine):
            kept[ident] = ("ambiguous", "untracked_copies")
        elif any(a < f_end and f_start < b for a, b in spans for f_start, f_end in foreign):
            kept[ident] = ("ambiguous", "overlaps_other_block")
        else:
            removable[ident] = spans
    return removable, kept


def _merge_spans(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union of overlapping or touching spans (all of them the selected
    sessions' own text), so removal never cuts one block in two."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


async def plan_forget(manager: Any, sessions: Sequence[ResolvedSession]) -> ForgetPlan:
    """Compute what forgetting *sessions* would remove. Writes nothing.

    *manager* is the agent's ``MemoryManager`` (``evidence``, ``store``,
    ``curated_store()``, ``workspace_dir``, ``agent_id``).
    """
    evidence: MemoryEvidenceStore = manager.evidence
    agent_id = str(getattr(manager, "agent_id", "main") or "main")
    selection = selection_for(sessions)
    plan = ForgetPlan(agent_id=agent_id, sessions=list(sessions), selection=selection)
    root = _workspace_root(manager)
    selected = await evidence.origins_for_sessions(selection)

    # 1. Curated entries.
    curated_store = None
    if root is None:
        if any(o.kind == "curated" for o in selected):
            plan.errors.append(
                {
                    "code": "no_workspace",
                    "stage": "curated",
                    "target": "",
                    "message": "workspace is not configured",
                }
            )
    else:
        curated_store = manager.curated_store()
        for target in CURATED_TARGETS:
            if curated_store.load_failed.get(target):
                # Unreadable is not empty: no origin here is stale.
                if any(o.kind == "curated" and o.target == target for o in selected):
                    plan.errors.append(
                        {
                            "code": "curated_unreadable",
                            "stage": "curated",
                            "target": target,
                            "message": _blocked_message(target, "unreadable"),
                        }
                    )
                continue
            entries = curated_store.entries_for(target)
            by_hash = {_item_hash(entry): entry for entry in entries}
            planned: set[str] = set()
            for origin in (o for o in selected if o.kind == "curated" and o.target == target):
                entry = by_hash.get(origin.item_hash)
                if entry is None:
                    plan.stale_origin_ids.append(origin.id)
                    continue
                if origin.item_hash in planned:
                    continue
                planned.add(origin.item_hash)
                every = await evidence.origins_for_item("curated", target, origin.item_hash)
                others = [
                    o
                    for o in every
                    if not selection.matches(session_key=o.session_key, session_id=o.session_id)
                ]
                plan.curated.append(
                    CuratedRemoval(
                        target=target, entry=entry, item_hash=origin.item_hash, other_origins=others
                    )
                )
            if any(r.target == target for r in plan.curated):
                blocked = curated_store.write_blocked_reason(target)
                if blocked is not None:
                    plan.errors.append(
                        {
                            "code": "curated_drift",
                            "stage": "curated",
                            "target": target,
                            "message": _blocked_message(target, blocked),
                        }
                    )
        plan.untracked = {
            target: counts["untracked"]
            for target, counts in (await curated_lineage_counts(evidence, curated_store)).items()
        }

    # 2. Note blocks, grouped by file.
    note_paths = sorted({o.target for o in selected if o.kind == "note"})
    for rel_path in note_paths:
        origins = _group_note_origins(await evidence.origins_for_note_path(rel_path))
        file_path = _note_file(root, rel_path) if root is not None else None
        text, reason = (None, "invalid_path") if file_path is None else _read_note(file_path)
        if text is None:
            removable: dict[NoteIdent, list[tuple[int, int]]] = {}
            kept = {
                ident: (reason or "unreadable", "")
                for ident, rows in origins.items()
                if any(
                    selection.matches(session_key=o.session_key, session_id=o.session_id)
                    for o in rows
                )
            }
        else:
            removable, kept = _note_ownership(normalize_newlines(text), origins, selection)
        for ident, (why, detail) in kept.items():
            plan.unmatched.append(
                # Its origins stay: forget deletes an origin only together
                # with the text it points to, so the same forget can be run
                # again once the note is readable, back or fixed by hand.
                NoteUnmatched(path=rel_path, item_hash=ident[0], reason=why, detail=detail)
            )
        for ident, spans in removable.items():
            plan.notes.append(
                NoteRemoval(
                    path=rel_path,
                    item_hash=ident[0],
                    item_len=ident[1],
                    occurrences=len(spans),
                )
            )

    # 3. Derived session documents still in the index.
    index_store: Any = getattr(manager, "store", None)
    list_paths = getattr(index_store, "list_paths", None)
    if callable(list_paths):
        from .types import MemorySource

        indexed = set(await list_paths(source=MemorySource.sessions))
        wanted = {_session_document_path(agent_id, sid) for sid in selection.session_ids}
        plan.session_documents = sorted(indexed & wanted)
    return plan


def _blocked_message(target: str, reason: str) -> str:
    name = CURATED_FILES.get(target, target)
    if reason == "unreadable":
        return f"{name} cannot be read; its entries cannot be removed until it can."
    return (
        f"{name} has content that does not round-trip through the memory store (edited "
        f"outside it). The memory store refuses to rewrite it until it is a clean list of "
        f"entries, so its entries cannot be removed yet."
    )


# ---------------------------------------------------------------------------
# Forget: apply and report
# ---------------------------------------------------------------------------


@dataclass
class _Applied:
    curated: list[CuratedRemoval] = field(default_factory=list)
    notes: list[NoteRemoval] = field(default_factory=list)
    unmatched: list[NoteUnmatched] = field(default_factory=list)
    session_documents: list[str] = field(default_factory=list)
    stale_origins: int = 0
    provider_mirrored: bool = False
    errors: list[dict[str, str]] = field(default_factory=list)


async def apply_forget(
    manager: Any,
    plan: ForgetPlan,
    *,
    on_memory_write: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Carry out *plan* and return the report of what was done.

    Records the sessions as forgotten first (so indexing stops at once), then
    removes curated entries through ``CuratedMemoryStore`` (lock, drift guard,
    atomic write), note blocks and session index documents. A step that fails
    is reported under ``errors`` and keeps its origins, so running the same
    forget again retries it. ``on_memory_write(agent_id)`` refreshes the prompt
    snapshot after a curated entry was removed, like a ``memory`` tool write
    does.
    """
    evidence: MemoryEvidenceStore = manager.evidence
    agent_id = plan.agent_id
    # Errors only the plan can see (nothing below retries them); a predicted
    # drift is left out because the curated write reports it for real.
    done = _Applied(
        unmatched=list(plan.unmatched),
        errors=[dict(e) for e in plan.errors if e.get("code") != "curated_drift"],
    )

    for session in plan.sessions:
        if session.matched_by == "session_key" and session.session_key:
            await evidence.add_forgotten_session(session_key=session.session_key)
        for session_id in session.session_ids:
            await evidence.add_forgotten_session(
                session_id=session_id, session_key=session.session_key
            )

    curated_changed = await _apply_curated(manager, plan, done)
    await _apply_notes(manager, plan, done)

    await _remove_session_documents(manager, plan, done)

    done.stale_origins = await evidence.delete_origins_by_id(plan.stale_origin_ids)

    # Only curated entries reach the prompt snapshot (daily notes are dropped
    # before injection), and a refresh also runs the runner's free-form
    # MEMORY.md migration, so refresh only when a curated entry went.
    if curated_changed and on_memory_write is not None:
        try:
            on_memory_write(agent_id)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "memory_forget.snapshot_refresh_failed",
                agent_id=agent_id,
                error_type=type(exc).__name__,
            )

    log.info(
        "memory_forget.applied",
        agent_id=agent_id,
        sessions=[s.selector for s in plan.sessions],
        curated=len(done.curated),
        notes=len(done.notes),
        unmatched=len(done.unmatched),
        session_documents=len(done.session_documents),
        errors=len(done.errors),
    )
    untracked = plan.untracked
    curated_store = manager.curated_store() if _workspace_root(manager) is not None else None
    if curated_store is not None:
        untracked = {
            target: counts["untracked"]
            for target, counts in (await curated_lineage_counts(evidence, curated_store)).items()
        }
    return _report(
        plan,
        dry_run=False,
        curated=done.curated,
        notes=done.notes,
        unmatched=done.unmatched,
        session_documents=done.session_documents,
        stale_origins=done.stale_origins,
        untracked=untracked,
        provider_mirrored=done.provider_mirrored,
        errors=done.errors,
    )


async def _remove_session_documents(manager: Any, plan: ForgetPlan, done: _Applied) -> None:
    """Remove each selected session's index document by its deterministic path.

    Not just the ones the plan saw: a session-index sync can publish one
    between plan and apply. Reported are the ones that were in the index.
    """
    index_store: Any = getattr(manager, "store", None)
    if index_store is None:
        return
    paths = sorted(
        {_session_document_path(plan.agent_id, sid) for sid in plan.selection.session_ids}
    )
    indexed = set(plan.session_documents)
    list_paths = getattr(index_store, "list_paths", None)
    if callable(list_paths):
        from .types import MemorySource

        indexed |= set(await list_paths(source=MemorySource.sessions))
    for doc_path in paths:
        try:
            await index_store.remove_file(doc_path)
        except Exception as exc:  # noqa: BLE001
            done.errors.append(
                {
                    "code": "session_document_failed",
                    "stage": "session_document",
                    "target": doc_path,
                    "message": type(exc).__name__,
                }
            )
            continue
        if doc_path in indexed:
            done.session_documents.append(doc_path)


async def _apply_curated(manager: Any, plan: ForgetPlan, done: _Applied) -> bool:
    if not plan.curated:
        return False
    evidence: MemoryEvidenceStore = manager.evidence
    store = manager.curated_store()
    changed = False
    for target in CURATED_TARGETS:
        removals = [r for r in plan.curated if r.target == target]
        if not removals:
            continue
        changes: list[dict[str, str]] = []
        result = await asyncio.to_thread(
            store.remove_exact, target, [r.entry for r in removals], changes=changes
        )
        if not result.get("success"):
            done.errors.append(
                {
                    "code": "curated_refused",
                    "stage": "curated",
                    "target": target,
                    "message": str(result.get("error") or "curated memory write refused"),
                }
            )
            continue
        removed = set(result.get("removed") or [])
        for removal in removals:
            # Removed now, or already gone since the plan: either way the
            # entry no longer exists, so none of its origins can stay.
            await evidence.delete_origins("curated", target, removal.item_hash)
            if removal.entry in removed:
                done.curated.append(removal)
        if changes:
            changed = True
            done.provider_mirrored = _mirror_removals(manager, result, target, changes) or (
                done.provider_mirrored
            )
    return changed


def _mirror_removals(
    manager: Any, result: dict[str, Any], target: str, changes: list[dict[str, str]]
) -> bool:
    provider_manager = getattr(manager, "provider_manager", None)
    if provider_manager is None:
        return False
    # The same mirror a ``memory`` tool write goes through; best effort by design.
    from agentos.tools.builtin.memory_tools import _mirror_memory_write

    try:
        _mirror_memory_write(provider_manager, tool_result=result, target=target, changes=changes)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "memory_forget.provider_mirror_failed", target=target, error_type=type(exc).__name__
        )
        return False
    return True


async def _apply_notes(manager: Any, plan: ForgetPlan, done: _Applied) -> bool:
    if not plan.notes:
        return False
    evidence: MemoryEvidenceStore = manager.evidence
    root = _workspace_root(manager)
    index_store: Any = getattr(manager, "store", None)
    changed = False
    by_path: dict[str, list[NoteRemoval]] = {}
    for removal in plan.notes:
        by_path.setdefault(removal.path, []).append(removal)
    for rel_path, removals in sorted(by_path.items()):
        file_path = _note_file(root, rel_path) if root is not None else None
        if file_path is None:
            _mark_unmatched(done, removals, "invalid_path")
            continue
        text, reason = _read_note(file_path)
        if text is None:
            _mark_unmatched(done, removals, reason or "unreadable")
            continue
        # Decide again on the note as it is now, with its current origins.
        origins = _group_note_origins(await evidence.origins_for_note_path(rel_path))
        removable, kept = _note_ownership(normalize_newlines(text), origins, plan.selection)
        present = [r for r in removals if (r.item_hash, r.item_len) in removable]
        for removal in removals:
            if removal not in present:
                why, detail = kept.get((removal.item_hash, removal.item_len), ("changed", ""))
                done.unmatched.append(
                    NoteUnmatched(
                        path=removal.path, item_hash=removal.item_hash, reason=why, detail=detail
                    )
                )
        if not present:
            continue
        counts = {
            (r.item_hash, r.item_len): len(removable[(r.item_hash, r.item_len)]) for r in present
        }
        spans = _merge_spans(span for r in present for span in removable[(r.item_hash, r.item_len)])
        # Cut from the text as stored: every byte outside the removed ranges,
        # line endings included, stays as it was.
        new_text = remove_spans(text, spans)
        try:
            if new_text.strip():
                from .atomic_write import atomic_write_text

                atomic_write_text(file_path, new_text)
            else:
                file_path.unlink()
        except OSError as exc:
            done.errors.append(
                {
                    "code": "note_write_failed",
                    "stage": "notes",
                    "target": rel_path,
                    # The OS reason only (no text from the note).
                    "message": exc.strerror or type(exc).__name__,
                }
            )
            continue
        changed = True
        for removal in present:
            removal.occurrences = counts[(removal.item_hash, removal.item_len)]
            await evidence.delete_origins("note", rel_path, removal.item_hash)
            done.notes.append(removal)
        try:
            from .types import MemorySource

            if new_text.strip():
                await index_store.index_file(
                    path=rel_path, content=normalize_newlines(new_text), source=MemorySource.memory
                )
            else:
                await index_store.remove_file(rel_path)
        except Exception as exc:  # noqa: BLE001
            done.errors.append(
                {
                    "code": "note_index_failed",
                    "stage": "notes_index",
                    "target": rel_path,
                    "message": f"note rewritten but not re-indexed: {type(exc).__name__}",
                }
            )
    return changed


def _mark_unmatched(done: _Applied, removals: Iterable[NoteRemoval], reason: str) -> None:
    # Present when planned, gone or unreadable now: the origins stay.
    for removal in removals:
        done.unmatched.append(
            NoteUnmatched(path=removal.path, item_hash=removal.item_hash, reason=reason)
        )


def forget_report(plan: ForgetPlan) -> dict[str, Any]:
    """The dry-run report: what :func:`apply_forget` would do with *plan*."""
    return _report(
        plan,
        dry_run=True,
        curated=plan.curated,
        notes=plan.notes,
        unmatched=plan.unmatched,
        session_documents=plan.session_documents,
        stale_origins=len(plan.stale_origin_ids),
        untracked=plan.untracked,
        provider_mirrored=False,
        errors=plan.errors,
    )


def _origin_wire(origin: Origin) -> dict[str, Any]:
    return {
        "sessionKey": origin.session_key,
        "sessionId": origin.session_id,
        "writer": origin.writer,
    }


def _report(
    plan: ForgetPlan,
    *,
    dry_run: bool,
    curated: Sequence[CuratedRemoval],
    notes: Sequence[NoteRemoval],
    unmatched: Sequence[NoteUnmatched],
    session_documents: Sequence[str],
    stale_origins: int,
    untracked: Mapping[str, int],
    provider_mirrored: bool,
    errors: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    return {
        "agentId": plan.agent_id,
        "dryRun": dry_run,
        "sessions": [s.wire() for s in plan.sessions],
        "curated": [{"target": r.target, "entry": r.entry, "mixed": r.mixed} for r in curated],
        "mixed": [
            {
                "target": r.target,
                "entry": r.entry,
                "otherOrigins": [_origin_wire(o) for o in r.other_origins],
            }
            for r in curated
            if r.mixed
        ],
        "notes": [
            {
                "path": r.path,
                "itemHash": r.item_hash,
                "chars": r.item_len,
                "occurrences": r.occurrences,
            }
            for r in notes
        ],
        "unmatched": [
            {"path": u.path, "itemHash": u.item_hash, "reason": u.reason}
            | ({"detail": u.detail} if u.detail else {})
            for u in unmatched
        ],
        "sessionDocuments": list(session_documents),
        "staleOrigins": int(stale_origins),
        "untracked": dict(untracked),
        "providerMirrored": provider_mirrored,
        "errors": [dict(e) for e in errors],
        "notCovered": FORGET_NOT_COVERED,
    }
