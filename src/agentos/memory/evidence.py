"""Memory lineage evidence kept beside the search index in ``memory.db``.

Two tables live here, per agent:

``memory_origins``
    Which session (and which writer) produced a curated entry or a note block,
    keyed by the sha256 of the item's text.
``forgotten_sessions``
    Sessions an operator asked memory to forget; kept out of session indexing
    from then on.

They share the per-agent ``memory.db`` with :class:`~agentos.memory.store.
LongTermMemoryStore` but are not index data: nothing in the store's
schema-version reset, embedding-change reset or ``rebuild()`` names them, so
they survive all three. This store opens its own connection to the same file
(the database runs in WAL mode with a busy timeout), so its short transactions
never interleave with the index store's ``BEGIN IMMEDIATE`` blocks on a shared
connection.

Every public method is safe to call concurrently from one event loop: a lock
serialises them. The connection autocommits, so a single-statement write is
complete the moment it runs and a cancelled caller cannot leave SQLite's
writer lock held; a multi-statement write runs in one explicit transaction
that is rolled back on any exception, cancellation included. The connection is
opened lazily on first use; :meth:`MemoryEvidenceStore.initialize` opens it
eagerly so the tables exist from boot.

Session identity is a :class:`SessionRef` pair. An empty string means
"unknown"; ``NULL`` is never stored, so uniqueness constraints hold for rows
without a session (operator writes).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import structlog

from agentos.compat import aiosqlite

log = structlog.get_logger(__name__)

ORIGIN_KINDS: Final[frozenset[str]] = frozenset({"curated", "note"})
ORIGIN_WRITERS: Final[frozenset[str]] = frozenset({"tool", "review", "operator"})
CURATED_TARGETS: Final[tuple[str, ...]] = ("memory", "user")
EVIDENCE_TABLES: Final[tuple[str, ...]] = ("memory_origins", "forgotten_sessions")

_DDL: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS memory_origins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        target TEXT NOT NULL,
        item_hash TEXT NOT NULL,
        item_len INTEGER NOT NULL DEFAULT 0,
        session_key TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        writer TEXT NOT NULL,
        at REAL NOT NULL,
        writes INTEGER NOT NULL DEFAULT 1,
        UNIQUE (kind, target, item_hash, session_key, session_id, writer)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_memory_origins_item ON memory_origins(kind, target, item_hash)",
    "CREATE INDEX IF NOT EXISTS idx_memory_origins_session_id ON memory_origins(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_memory_origins_session_key ON memory_origins(session_key)",
    """
    CREATE TABLE IF NOT EXISTS forgotten_sessions (
        session_id TEXT NOT NULL DEFAULT '',
        session_key TEXT NOT NULL DEFAULT '',
        at REAL NOT NULL,
        PRIMARY KEY (session_id, session_key)
    )
    """,
)

# A forgotten_sessions row covering (session_id, session_key) bound in that
# order: one forgotten by id, or a whole key forgotten (empty session_id).
_FORGOTTEN_MATCH: Final[str] = (
    "SELECT 1 FROM forgotten_sessions WHERE "
    "(session_id != '' AND session_id = ?) OR "
    "(session_id = '' AND session_key != '' AND session_key = ?)"
)


def text_hash(text: str) -> str:
    """Return the sha256 hex digest of *text* encoded as UTF-8.

    The one hash used for origins (``item_hash``) and anywhere else this
    module keys on content. Callers hash the exact stored text: the curated
    entry as the store keeps it, or the note block as defined in
    :func:`agentos.memory.lineage.note_block`.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_text(text: str) -> str:
    """*text* with line endings canonical: ``\\r\\n`` and ``\\r`` become ``\\n``.

    The form a memory file has once read back (Python's text mode). The one
    canonicalisation lineage uses, through :func:`item_hash`, so an item hashes
    the same whether it was hashed as submitted or as loaded from disk.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def item_hash(text: str) -> str:
    """The ``memory_origins.item_hash`` of a curated entry or note block:
    :func:`text_hash` of :func:`canonical_text`. Every origin is keyed by it."""
    return text_hash(canonical_text(text))


@dataclass(frozen=True)
class SessionRef:
    """A session as lineage sees it: a key and an id.

    ``session_key`` is the stable conversation key; ``session_id`` is the
    transcript identity, which a reset rotates under the same key. Either may
    be ``""`` when unknown.
    """

    session_key: str = ""
    session_id: str = ""

    @property
    def empty(self) -> bool:
        return not self.session_key and not self.session_id


@dataclass(frozen=True)
class SessionSelection:
    """Which sessions a query or a forget applies to.

    A row matches when its ``session_key`` is in :attr:`session_keys` or its
    ``session_id`` is in :attr:`session_ids`. Empty strings are never matched.
    """

    session_keys: frozenset[str] = frozenset()
    session_ids: frozenset[str] = frozenset()

    @classmethod
    def of(cls, *, keys: Iterable[str] = (), ids: Iterable[str] = ()) -> SessionSelection:
        return cls(
            session_keys=frozenset(k for k in keys if k),
            session_ids=frozenset(i for i in ids if i),
        )

    @property
    def empty(self) -> bool:
        return not self.session_keys and not self.session_ids

    def matches(self, *, session_key: str = "", session_id: str = "") -> bool:
        return bool(
            (session_key and session_key in self.session_keys)
            or (session_id and session_id in self.session_ids)
        )

    def _where(self) -> tuple[str, list[str]]:
        """SQL predicate over ``session_key`` / ``session_id`` columns."""
        clauses: list[str] = []
        params: list[str] = []
        if self.session_keys:
            keys = sorted(self.session_keys)
            clauses.append(f"session_key IN ({','.join('?' * len(keys))})")
            params.extend(keys)
        if self.session_ids:
            ids = sorted(self.session_ids)
            clauses.append(f"session_id IN ({','.join('?' * len(ids))})")
            params.extend(ids)
        if not clauses:
            return "0", []
        return "(" + " OR ".join(clauses) + ")", params


@dataclass(frozen=True)
class Origin:
    """One recorded origin of a curated entry or a note block.

    ``kind`` is ``curated`` (``target`` is ``memory`` or ``user``) or ``note``
    (``target`` is the workspace-relative note path). ``item_len`` is the
    length in characters of the hashed text; it lets a note block be found in
    its file by hash alone. ``writes`` counts how many times this session and
    writer wrote the same note block to the same note (one for curated
    entries), so forget knows how many copies are theirs.
    """

    id: int
    kind: str
    target: str
    item_hash: str
    item_len: int
    session_key: str
    session_id: str
    writer: str
    at: float
    writes: int = 1

    @property
    def session(self) -> SessionRef:
        return SessionRef(session_key=self.session_key, session_id=self.session_id)


@dataclass(frozen=True)
class ForgottenSessions:
    """Snapshot of ``forgotten_sessions`` for fast membership checks.

    A forget by session id excludes that transcript only. A forget by session
    key excludes every transcript under the key, including ones a later reset
    creates; such rows are stored with an empty ``session_id``.
    """

    session_ids: frozenset[str] = frozenset()
    whole_keys: frozenset[str] = frozenset()

    def contains(self, *, session_id: str = "", session_key: str = "") -> bool:
        return bool(
            (session_id and session_id in self.session_ids)
            or (session_key and session_key in self.whole_keys)
        )

    def __len__(self) -> int:
        return len(self.session_ids) + len(self.whole_keys)


async def _add_missing_columns(db: aiosqlite.Connection) -> None:
    """Bring a table created by an earlier version up to the current DDL."""
    async with db.execute("PRAGMA table_info(memory_origins)") as cur:
        columns = {row[1] for row in await cur.fetchall()}
    if "writes" not in columns:
        # Rows from before the counter count as one write: the conservative
        # reading for forget, which never removes more copies than writes.
        await db.execute("ALTER TABLE memory_origins ADD COLUMN writes INTEGER NOT NULL DEFAULT 1")


def _validate_choice(value: str, allowed: frozenset[str], name: str) -> None:
    if value not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}, got {value!r}")


class MemoryEvidenceStore:
    """Typed access to the two lineage tables in one agent's ``memory.db``."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db_path = str(db_path)
        self._clock = clock
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def db_path(self) -> str:
        return self._db_path

    @property
    def is_open(self) -> bool:
        return self._db is not None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Open the connection and create the tables if they do not exist."""
        async with self._lock:
            await self._conn()

    async def close(self) -> None:
        """Close the connection. Idempotent; a later call reopens lazily."""
        async with self._lock:
            if self._db is not None:
                db, self._db = self._db, None
                await db.close()

    async def _conn(self) -> aiosqlite.Connection:
        if self._db is not None:
            return self._db
        # Autocommit (no implicit transactions): see the module docstring.
        db = await aiosqlite.connect(self._db_path, isolation_level=None)
        try:
            await db.execute("PRAGMA busy_timeout = 5000")
            await db.execute("PRAGMA journal_mode = WAL")
            for statement in _DDL:
                await db.execute(statement)
            await _add_missing_columns(db)
            await db.commit()
        except BaseException:
            await db.close()
            raise
        self._db = db
        return db

    @asynccontextmanager
    async def _transaction(self, db: aiosqlite.Connection) -> AsyncIterator[None]:
        """One explicit transaction; the caller holds the lock.

        Rolled back on any exception, cancellation included. The rollback and
        the commit are shielded so a second cancellation cannot interrupt
        them; statements on the connection run in order, so the rollback
        always follows whatever was already queued.
        """
        try:
            await db.execute("BEGIN IMMEDIATE")
            yield
            await asyncio.shield(db.commit())
        except BaseException:
            await asyncio.shield(db.rollback())
            raise

    def _now(self, at: float | None) -> float:
        return float(self._clock() if at is None else at)

    # ------------------------------------------------------------------
    # Origins
    # ------------------------------------------------------------------

    async def add_origin(
        self,
        *,
        kind: str,
        target: str,
        item_hash: str,
        writer: str,
        session: SessionRef | None = None,
        item_len: int = 0,
        at: float | None = None,
        count_write: bool = False,
    ) -> bool:
        """Record that ``session`` (via ``writer``) produced an item.

        Idempotent per ``(kind, target, item_hash, session, writer)``: a
        repeat returns ``False`` and keeps the first row's time -- unless
        ``count_write`` is set (a note write), in which case the repeat adds
        one to the row's ``writes`` and returns ``True``.
        """
        if count_write:
            return await self._count_write(
                kind=kind,
                target=target,
                item_hash=item_hash,
                item_len=int(item_len),
                session=session or SessionRef(),
                writer=writer,
                at=self._now(at),
            )
        return (
            await self.add_origins(
                [
                    Origin(
                        id=0,
                        kind=kind,
                        target=target,
                        item_hash=item_hash,
                        item_len=int(item_len),
                        session_key=(session or SessionRef()).session_key,
                        session_id=(session or SessionRef()).session_id,
                        writer=writer,
                        at=self._now(at),
                    )
                ]
            )
            > 0
        )

    async def _count_write(
        self,
        *,
        kind: str,
        target: str,
        item_hash: str,
        item_len: int,
        session: SessionRef,
        writer: str,
        at: float,
    ) -> bool:
        _validate_choice(kind, ORIGIN_KINDS, "kind")
        _validate_choice(writer, ORIGIN_WRITERS, "writer")
        async with self._lock:
            db = await self._conn()
            await db.execute(
                """
                INSERT INTO memory_origins
                    (kind, target, item_hash, item_len, session_key, session_id, writer, at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (kind, target, item_hash, session_key, session_id, writer)
                DO UPDATE SET writes = writes + 1
                """,
                (
                    kind,
                    target,
                    item_hash,
                    item_len,
                    session.session_key,
                    session.session_id,
                    writer,
                    at,
                ),
            )
        return True

    async def add_origins(self, origins: Iterable[Origin]) -> int:
        """Insert origins as given (``id`` is ignored). Returns rows inserted."""
        rows = list(origins)
        for origin in rows:
            _validate_choice(origin.kind, ORIGIN_KINDS, "kind")
            _validate_choice(origin.writer, ORIGIN_WRITERS, "writer")
        if not rows:
            return 0
        inserted = 0
        async with self._lock:
            db = await self._conn()
            async with self._transaction(db):
                for origin in rows:
                    cur = await db.execute(
                        """
                        INSERT OR IGNORE INTO memory_origins
                            (kind, target, item_hash, item_len, session_key, session_id,
                             writer, at, writes)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            origin.kind,
                            origin.target,
                            origin.item_hash,
                            int(origin.item_len),
                            origin.session_key,
                            origin.session_id,
                            origin.writer,
                            float(origin.at),
                            max(1, int(origin.writes)),
                        ),
                    )
                    inserted += max(0, cur.rowcount or 0)
        return inserted

    async def origins_for_item(self, kind: str, target: str, item_hash: str) -> list[Origin]:
        """All origins of one item, oldest first."""
        return await self._origins(
            "kind = ? AND target = ? AND item_hash = ?",
            [
                kind,
                target,
                item_hash,
            ],
        )

    async def origins_for_target(self, kind: str, target: str) -> list[Origin]:
        """All origins under one target: ``("curated", "memory")`` or ``("note", path)``."""
        return await self._origins("kind = ? AND target = ?", [kind, target])

    async def origins_for_note_path(self, path: str) -> list[Origin]:
        """All origins of note blocks written to *path*."""
        return await self.origins_for_target("note", path)

    async def origins_for_sessions(
        self, sessions: SessionSelection, *, kind: str | None = None
    ) -> list[Origin]:
        """All origins recorded for *sessions*, optionally of one kind."""
        if sessions.empty:
            return []
        where, params = sessions._where()
        if kind is not None:
            where += " AND kind = ?"
            params = [*params, kind]
        return await self._origins(where, params)

    async def tracked_item_hashes(self, kind: str, target: str) -> set[str]:
        """Hashes of items under a target that have at least one origin."""
        async with self._lock:
            db = await self._conn()
            async with db.execute(
                "SELECT DISTINCT item_hash FROM memory_origins WHERE kind = ? AND target = ?",
                (kind, target),
            ) as cur:
                return {r[0] for r in await cur.fetchall()}

    async def copy_origins(
        self,
        *,
        kind: str,
        target: str,
        from_hash: str,
        to_hash: str,
        to_len: int = 0,
        writer: str | None = None,
        to_target: str | None = None,
    ) -> int:
        """Carry the origins of one item over to another.

        Copies every origin of ``(kind, target, from_hash)`` onto ``to_hash``
        (under ``to_target`` when given), keeping session and time; ``writer``
        overrides the copied rows' writer when given. Does not delete the
        source rows. Returns the number of rows inserted.
        """
        sources = await self.origins_for_item(kind, target, from_hash)
        return await self.add_origins(
            Origin(
                id=0,
                kind=kind,
                target=to_target if to_target is not None else target,
                item_hash=to_hash,
                item_len=int(to_len),
                session_key=o.session_key,
                session_id=o.session_id,
                writer=writer or o.writer,
                at=o.at,
                writes=o.writes,
            )
            for o in sources
        )

    async def delete_origins(self, kind: str, target: str, item_hash: str) -> int:
        """Delete every origin of one item."""
        return await self._execute_count(
            "DELETE FROM memory_origins WHERE kind = ? AND target = ? AND item_hash = ?",
            (kind, target, item_hash),
        )

    async def delete_origins_by_id(self, ids: Iterable[int]) -> int:
        id_list = sorted({int(i) for i in ids})
        if not id_list:
            return 0
        return await self._delete_in("memory_origins", "id", id_list)

    async def _origins(self, where: str, params: list[Any]) -> list[Origin]:
        async with self._lock:
            db = await self._conn()
            async with db.execute(
                "SELECT id, kind, target, item_hash, item_len, session_key, session_id, "
                f"writer, at, writes FROM memory_origins WHERE {where} ORDER BY at, id",
                params,
            ) as cur:
                rows = await cur.fetchall()
        return [
            Origin(
                id=int(r[0]),
                kind=r[1],
                target=r[2],
                item_hash=r[3],
                item_len=int(r[4]),
                session_key=r[5],
                session_id=r[6],
                writer=r[7],
                at=float(r[8]),
                writes=int(r[9]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Forgotten sessions
    # ------------------------------------------------------------------

    async def add_forgotten_session(
        self,
        *,
        session_id: str = "",
        session_key: str = "",
        at: float | None = None,
    ) -> bool:
        """Record a forgotten session; ``False`` when it was already recorded.

        Pass ``session_id`` to forget one transcript. Pass only
        ``session_key`` (empty ``session_id``) to forget every transcript under
        the key, now and after future resets.
        """
        if not session_id and not session_key:
            raise ValueError("session_id or session_key is required")
        async with self._lock:
            db = await self._conn()
            cur = await db.execute(
                "INSERT OR IGNORE INTO forgotten_sessions (session_id, session_key, at) "
                "VALUES (?, ?, ?)",
                (session_id, session_key, self._now(at)),
            )
            inserted = (cur.rowcount or 0) > 0
            await db.commit()
        return inserted

    async def forgotten_sessions(self) -> ForgottenSessions:
        """Snapshot of every forgotten session, for repeated membership checks."""
        async with self._lock:
            db = await self._conn()
            async with db.execute("SELECT session_id, session_key FROM forgotten_sessions") as cur:
                rows = await cur.fetchall()
        ids = frozenset(r[0] for r in rows if r[0])
        keys = frozenset(r[1] for r in rows if not r[0] and r[1])
        return ForgottenSessions(session_ids=ids, whole_keys=keys)

    async def is_session_forgotten(self, *, session_id: str = "", session_key: str = "") -> bool:
        """Whether a session (by id, or by a key forgotten as a whole) is forgotten."""
        if not session_id and not session_key:
            return False
        return (
            await self._scalar(
                f"SELECT COUNT(*) FROM ({_FORGOTTEN_MATCH})", [session_id, session_key]
            )
            > 0
        )

    async def known_sessions(self, value: str) -> list[SessionRef]:
        """Sessions this store has seen whose id or key equals *value* exactly.

        Looks through origins and forgotten sessions. Used to resolve
        a forget selector for a session that no longer exists in session
        storage. Never matches a prefix.
        """
        if not value:
            return []
        found: set[tuple[str, str]] = set()
        async with self._lock:
            db = await self._conn()
            for table in ("memory_origins", "forgotten_sessions"):
                async with db.execute(
                    f"SELECT DISTINCT session_key, session_id FROM {table} "
                    "WHERE session_key = ? OR session_id = ?",
                    (value, value),
                ) as cur:
                    found.update((r[0], r[1]) for r in await cur.fetchall())
        return [SessionRef(session_key=k, session_id=i) for k, i in sorted(found)]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def table_counts(self) -> dict[str, int]:
        """Row count per evidence table (diagnostics and tests)."""
        counts: dict[str, int] = {}
        for table in EVIDENCE_TABLES:
            counts[table] = await self._scalar(f"SELECT COUNT(*) FROM {table}", [])
        return counts

    async def _scalar(self, sql: str, params: Iterable[Any]) -> int:
        async with self._lock:
            db = await self._conn()
            async with db.execute(sql, list(params)) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    async def _execute_count(self, sql: str, params: Iterable[Any]) -> int:
        async with self._lock:
            db = await self._conn()
            cur = await db.execute(sql, list(params))
            count = max(0, cur.rowcount or 0)
            await db.commit()
        return count

    async def _delete_in(self, table: str, column: str, values: list[Any]) -> int:
        deleted = 0
        # Stay well under SQLite's bound-parameter limit.
        for start in range(0, len(values), 500):
            chunk = values[start : start + 500]
            deleted += await self._execute_count(
                f"DELETE FROM {table} WHERE {column} IN ({','.join('?' * len(chunk))})", chunk
            )
        return deleted
