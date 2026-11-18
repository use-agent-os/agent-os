"""Derived memory-source documents for persisted session transcripts."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .redaction import redact_memory_text
from .types import MemorySource

_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_INVALID_AGENT_CHARS_RE = re.compile(r"[^a-z0-9_-]")
_LEADING_TRAILING_DASHES_RE = re.compile(r"^-+|-+$")
_ROLE_LABELS = {
    "user": "User",
    "assistant": "Assistant",
}


@dataclass(frozen=True)
class SessionSourceDocument:
    path: str
    content: str
    mtime: float


@dataclass(frozen=True)
class SessionSourceSyncResult:
    indexed: int = 0
    removed: int = 0
    skipped: int = 0


def _safe_segment(value: str, *, fallback: str) -> str:
    cleaned = _SAFE_SEGMENT_RE.sub("-", value.strip()).strip("-._")
    return cleaned or fallback


def _normalize_agent_id(agent_id: object) -> str:
    raw = str(agent_id or "").strip().lower()
    if not raw or raw == "default":
        return "main"
    normalized = _INVALID_AGENT_CHARS_RE.sub("-", raw)
    normalized = _LEADING_TRAILING_DASHES_RE.sub("", normalized)[:64]
    if not normalized or normalized == "default":
        return "main"
    return normalized


def _redact_transcript_text(text: str) -> str:
    return redact_memory_text(text)


def _format_timestamp(ms: int | None) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


def session_source_path(session: Any) -> str:
    """Return the derived document path for a session.

    Split out of :func:`build_session_source_document` so the freshness
    pre-check in :meth:`SessionSourceIndexer.sync` addresses the same row the
    document is later written to. Deriving it twice would let the two spellings
    drift and the check would silently target nothing.
    """
    agent_id = _normalize_agent_id(getattr(session, "agent_id", None) or "main")
    safe_agent = _safe_segment(agent_id, fallback="main")
    safe_session_id = _safe_segment(session.session_id, fallback="session")
    return f"sessions/{safe_agent}/{safe_session_id}.md"


def session_source_mtime(session: Any) -> float:
    """Return the document mtime for a session, in seconds.

    Taken from ``updated_at`` alone, which is why it can be read before the
    transcript is: ``SessionManager.append_message`` touches ``updated_at`` on
    every append, so a session that gained a message always compares newer.
    """
    return (getattr(session, "updated_at", None) or 0) / 1000


def build_session_source_document(
    session: Any,
    entries: list[Any],
) -> SessionSourceDocument:
    """Render a session transcript into a deterministic derived source document."""
    agent_id = _normalize_agent_id(getattr(session, "agent_id", None) or "main")
    path = session_source_path(session)

    lines = [
        "---",
        "source: sessions",
        f"session_id: {session.session_id}",
        f"agent_id: {agent_id}",
    ]
    updated_at = _format_timestamp(getattr(session, "updated_at", None))
    if updated_at:
        lines.append(f"updated_at: {updated_at}")
    label_value = (
        getattr(session, "label", None) or getattr(session, "display_name", None) or ""
    )
    label = label_value.strip()
    if label:
        lines.append(f"label: {_redact_transcript_text(label)}")
    lines.extend(["---", ""])

    rendered_entries = 0
    for entry in entries:
        label_for_role = _ROLE_LABELS.get(str(entry.role).lower())
        content = (entry.content or "").strip()
        if label_for_role is None or not content:
            continue
        rendered_entries += 1
        entry_id = entry.id if entry.id is not None else entry.message_id
        safe_content = _redact_transcript_text(content)
        lines.append(f"[entry {entry_id}] {label_for_role}: {safe_content}")

    if rendered_entries == 0:
        lines.append("(no user or assistant transcript content)")

    mtime = session_source_mtime(session)
    return SessionSourceDocument(
        path=path,
        content="\n".join(lines).rstrip() + "\n",
        mtime=mtime,
    )


class SessionSourceIndexer:
    """Synchronize persisted session transcripts into the memory index."""

    def __init__(
        self,
        *,
        storage: Any,
        store: Any,
        agent_id: str = "main",
        max_sessions: int = 1000,
        forgotten: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        """``forgotten`` returns the agent's forgotten sessions (an object with
        ``contains(session_id=..., session_key=...)``, see
        ``MemoryEvidenceStore.forgotten_sessions``). Those sessions are never
        indexed, and their existing documents fall to the stale-path sweep.
        It is read before the transcripts, again right before each document
        is published, and once more after publishing: a forget or a session
        delete that lands while this sync is reading or embedding is caught by
        the last read, and the document it would have brought back is
        withdrawn. When it raises, the sync raises too
        rather than index a session that may have been forgotten.
        """
        self._storage: Any = storage
        self._store = store
        self._agent_id = _normalize_agent_id(agent_id)
        self._max_sessions = max(1, max_sessions)
        self._forgotten = forgotten

    async def sync(self, *, force: bool = False) -> SessionSourceSyncResult:
        sessions, complete_scan = await self._list_sessions()
        expected_paths: set[str] = set()
        indexed = 0
        skipped = 0

        if self._forgotten is not None:
            forgotten = await self._forgotten()
            kept = [
                session
                for session in sessions
                if not forgotten.contains(
                    session_id=str(getattr(session, "session_id", "") or ""),
                    session_key=str(getattr(session, "session_key", "") or ""),
                )
            ]
            skipped += len(sessions) - len(kept)
            sessions = kept

        unchanged = set() if force else await self._unchanged_document_paths(sessions)
        published: list[tuple[Any, str]] = []

        for session in sessions:
            if not force:
                path = session_source_path(session)
                if path in unchanged:
                    # Still expected, or the stale-path sweep below would
                    # delete a document that is simply up to date.
                    expected_paths.add(path)
                    continue
            entries = await self._storage.get_transcript(session.session_id)
            if not entries:
                skipped += 1
                continue
            document = build_session_source_document(session, entries)
            if await self._forgotten_now(session):
                # Forgotten while this sync was reading it: do not publish.
                skipped += 1
                continue
            expected_paths.add(document.path)
            chunks = await self._store.index_file(
                path=document.path,
                content=document.content,
                source=MemorySource.sessions,
                mtime=document.mtime,
            )
            published.append((session, document.path))
            if chunks > 0 or force:
                indexed += 1

        removed = 0
        for path in await self._lost_races(published):
            await self._store.remove_file(path)
            expected_paths.discard(path)
            removed += 1
        list_paths = getattr(self._store, "list_paths", None)
        if complete_scan and callable(list_paths):
            existing_paths = set(await list_paths(source=MemorySource.sessions))
            for stale_path in sorted(existing_paths - expected_paths):
                await self._store.remove_file(stale_path)
                removed += 1

        return SessionSourceSyncResult(indexed=indexed, removed=removed, skipped=skipped)

    async def _forgotten_now(self, session: Any) -> bool:
        """A fresh read of the forgotten set for one session, right before publishing."""
        if self._forgotten is None:
            return False
        forgotten = await self._forgotten()
        return bool(
            forgotten.contains(
                session_id=str(getattr(session, "session_id", "") or ""),
                session_key=str(getattr(session, "session_key", "") or ""),
            )
        )

    async def _lost_races(self, published: list[tuple[Any, str]]) -> list[str]:
        """Documents this sync published for sessions forgotten or deleted since.

        Read after every publish, so a forget or delete that removed a
        document before this sync wrote it back is seen here; one that lands
        later removes the document itself.
        """
        if not published:
            return []
        forgotten = await self._forgotten() if self._forgotten is not None else None
        get_session = getattr(self._storage, "get_session", None)
        lost: list[str] = []
        for session, path in published:
            session_id = str(getattr(session, "session_id", "") or "")
            session_key = str(getattr(session, "session_key", "") or "")
            if forgotten is not None and forgotten.contains(
                session_id=session_id, session_key=session_key
            ):
                lost.append(path)
                continue
            if callable(get_session):
                current = await get_session(session_key)
                if current is None or str(getattr(current, "session_id", "")) != session_id:
                    lost.append(path)
        return lost

    async def _unchanged_document_paths(self, sessions: list[Any]) -> set[str]:
        """Document paths already indexed at or past their session's mtime.

        ``index_file`` decides "unchanged" by hashing the rendered document, so
        reaching that decision used to cost a full transcript read, a redaction
        pass over every entry and the whole document render -- per session, for
        every session, on every sync. One message in one session was enough to
        pay it for all of them, and ``sync`` runs on session start, on the
        timer, on watch events and ahead of memory searches.

        The mtime that ``index_file`` records comes from ``updated_at`` alone
        (:func:`session_source_mtime`), so the same comparison can be made from
        the session rows in a single batched query. ``get_file_mtimes`` is the
        existing accessor for it -- ``memory/retrieval.py`` already reads the
        table this way.

        Conservative in both directions: a session with no ``updated_at`` is
        never skipped, and a store without the accessor skips nothing. Opening
        a session also touches ``updated_at``, so an untouched transcript can
        still be re-read -- that is the old behaviour, not a regression.
        """
        get_file_mtimes = getattr(self._store, "get_file_mtimes", None)
        if not callable(get_file_mtimes):
            return set()

        candidates: dict[str, float] = {}
        for session in sessions:
            mtime = session_source_mtime(session)
            if mtime > 0:
                candidates[session_source_path(session)] = mtime
        if not candidates:
            return set()

        known = await get_file_mtimes(list(candidates))
        return {
            path
            for path, mtime in candidates.items()
            if known.get(path) is not None and mtime <= known[path]
        }

    async def _list_sessions(self) -> tuple[list[Any], bool]:
        page_size = min(100, self._max_sessions)
        offset = 0
        sessions: list[Any] = []
        while len(sessions) < self._max_sessions:
            limit = min(page_size, self._max_sessions - len(sessions))
            page = await self._storage.list_sessions(
                agent_id=self._agent_id,
                limit=limit,
                offset=offset,
            )
            sessions.extend(page)
            offset += len(page)
            if len(page) < limit:
                return sessions, True
            if not page:
                return sessions, True
        return sessions, False
