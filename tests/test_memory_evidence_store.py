"""``MemoryEvidenceStore``: the two lineage tables beside the memory index.

The tables share ``memory.db`` with ``LongTermMemoryStore`` but are not index
data, so the index's three reset paths (``rebuild()``, a ``SCHEMA_VERSION``
mismatch, an embedding change) must leave every row in place.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from agentos.memory.embedding import NullEmbeddingProvider
from agentos.memory.evidence import (
    EVIDENCE_TABLES,
    MemoryEvidenceStore,
    Origin,
    SessionRef,
    SessionSelection,
    text_hash,
)
from agentos.memory.store import META_KEY, LongTermMemoryStore
from agentos.memory.types import MemorySource

S1 = SessionRef(session_key="agent:main:webchat:direct:s1", session_id="id-s1")
S2 = SessionRef(session_key="agent:main:webchat:direct:s2", session_id="id-s2")
T0 = 1_763_400_000.0  # fixed clock


class _OtherEmbedding(NullEmbeddingProvider):
    """An FTS-only provider that reports a different model, forcing a reindex."""

    @property
    def provider_id(self) -> str:
        return "other"

    @property
    def model(self) -> str:
        return "other-model"


async def _seed(evidence: MemoryEvidenceStore) -> None:
    await evidence.add_origin(
        kind="curated", target="memory", item_hash=text_hash("fact"), writer="tool", session=S1
    )
    await evidence.add_forgotten_session(session_id="id-old", session_key="k-old")


async def _counts(db_path: Path) -> dict[str, int]:
    evidence = MemoryEvidenceStore(db_path)
    try:
        return await evidence.table_counts()
    finally:
        await evidence.close()


SEEDED = {"memory_origins": 1, "forgotten_sessions": 1}


@pytest.fixture()
async def evidence(tmp_path: Path):
    store = MemoryEvidenceStore(tmp_path / "memory.db", clock=lambda: T0)
    await store.initialize()
    yield store
    await store.close()


# -- creation --------------------------------------------------------------


async def test_initialize_creates_the_evidence_tables(tmp_path: Path) -> None:
    store = MemoryEvidenceStore(tmp_path / "memory.db")
    await store.initialize()
    await store.close()

    conn = sqlite3.connect(tmp_path / "memory.db")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert set(EVIDENCE_TABLES) <= names


async def test_store_opens_lazily_and_close_is_idempotent(tmp_path: Path) -> None:
    store = MemoryEvidenceStore(tmp_path / "memory.db")
    assert not store.is_open
    assert await store.known_sessions("x") == []
    assert store.is_open
    await store.close()
    await store.close()
    assert not store.is_open


# -- survival across the index's reset paths --------------------------------


async def test_rows_survive_store_rebuild(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    index = LongTermMemoryStore(db)
    await index.initialize()
    evidence = MemoryEvidenceStore(db)
    try:
        await index.index_file("memory/a.md", "alpha note", source=MemorySource.memory)
        await _seed(evidence)

        await index.rebuild()

        assert await index.file_count() == 0
        assert await evidence.table_counts() == SEEDED
    finally:
        await evidence.close()
        await index.close()


async def test_rows_survive_a_schema_version_reset(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    index = LongTermMemoryStore(db)
    await index.initialize()
    await index.index_file("memory/a.md", "alpha note", source=MemorySource.memory)
    await index.close()
    evidence = MemoryEvidenceStore(db)
    await _seed(evidence)
    await evidence.close()

    conn = sqlite3.connect(db)
    conn.execute("UPDATE meta SET value = 'v0-old' WHERE key = ?", (META_KEY,))
    conn.commit()
    conn.close()

    reopened = LongTermMemoryStore(db)
    await reopened.initialize()  # drops and recreates the index tables
    try:
        assert await reopened.file_count() == 0
    finally:
        await reopened.close()
    assert await _counts(db) == SEEDED


async def test_rows_survive_an_embedding_change_reset(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    index = LongTermMemoryStore(db)
    await index.initialize()
    await index.index_file("memory/a.md", "alpha note", source=MemorySource.memory)
    await index.close()
    evidence = MemoryEvidenceStore(db)
    await _seed(evidence)
    await evidence.close()

    reopened = LongTermMemoryStore(db, embedding_provider=_OtherEmbedding())
    await reopened.initialize()  # meta mismatch clears chunks/files
    try:
        assert await reopened.file_count() == 0
    finally:
        await reopened.close()
    assert await _counts(db) == SEEDED


async def test_writes_interleave_with_an_index_transaction(tmp_path: Path) -> None:
    """Own connection: evidence writes never land inside index_file's transaction.

    On a shared connection an evidence commit between index_file's
    ``BEGIN IMMEDIATE`` and its commit would commit half an index update, and
    an evidence write left open would make index_file's ``BEGIN`` fail.
    """
    db = tmp_path / "memory.db"
    index = LongTermMemoryStore(db)
    await index.initialize()
    evidence = MemoryEvidenceStore(db)

    async def _index() -> None:
        for i in range(5):
            await index.index_file(
                f"memory/n{i}.md", f"note number {i}", source=MemorySource.memory
            )

    async def _record() -> None:
        for i in range(20):
            await evidence.add_origin(
                kind="note", target=f"memory/n{i}.md", item_hash=f"h{i}", writer="tool"
            )

    try:
        await asyncio.gather(_index(), _record())
        assert await index.file_count() == 5
        assert (await evidence.table_counts())["memory_origins"] == 20
    finally:
        await evidence.close()
        await index.close()


# -- origins -----------------------------------------------------------------


async def test_origins_are_idempotent_per_session_and_writer(
    evidence: MemoryEvidenceStore,
) -> None:
    h = text_hash("fact")
    assert await evidence.add_origin(
        kind="curated", target="memory", item_hash=h, item_len=4, writer="tool", session=S1
    )
    assert not await evidence.add_origin(
        kind="curated", target="memory", item_hash=h, item_len=4, writer="tool", session=S1
    )
    assert await evidence.add_origin(
        kind="curated", target="memory", item_hash=h, writer="review", session=S1
    )
    # Operator rows have no session and still dedupe (no NULLs are stored).
    assert await evidence.add_origin(
        kind="curated", target="memory", item_hash=h, writer="operator"
    )
    assert not await evidence.add_origin(
        kind="curated", target="memory", item_hash=h, writer="operator"
    )

    origins = await evidence.origins_for_item("curated", "memory", h)
    assert [(o.writer, o.session_id) for o in origins] == [
        ("tool", S1.session_id),
        ("review", S1.session_id),
        ("operator", ""),
    ]
    assert await evidence.tracked_item_hashes("curated", "memory") == {h}


async def test_origin_validation(evidence: MemoryEvidenceStore) -> None:
    with pytest.raises(ValueError):
        await evidence.add_origin(kind="other", target="memory", item_hash="h", writer="tool")
    with pytest.raises(ValueError):
        await evidence.add_origin(kind="curated", target="memory", item_hash="h", writer="bot")


async def test_copy_delete_and_lookup_origins(evidence: MemoryEvidenceStore) -> None:
    await evidence.add_origin(
        kind="curated", target="memory", item_hash="old", writer="tool", session=S1
    )
    await evidence.add_origin(
        kind="note", target="memory/n.md", item_hash="blk", item_len=9, writer="review", session=S2
    )

    copied = await evidence.copy_origins(
        kind="curated", target="memory", from_hash="old", to_hash="new", to_len=3
    )
    assert copied == 1
    new = await evidence.origins_for_item("curated", "memory", "new")
    assert [(o.session, o.writer, o.item_len) for o in new] == [(S1, "tool", 3)]

    by_session = await evidence.origins_for_sessions(SessionSelection.of(keys=[S2.session_key]))
    assert [(o.kind, o.target) for o in by_session] == [("note", "memory/n.md")]
    assert await evidence.origins_for_sessions(SessionSelection()) == []
    notes = await evidence.origins_for_note_path("memory/n.md")
    assert [o.item_len for o in notes] == [9]

    assert await evidence.delete_origins("curated", "memory", "old") == 1
    assert await evidence.origins_for_item("curated", "memory", "old") == []
    assert await evidence.delete_origins_by_id([notes[0].id]) == 1
    assert await evidence.origins_for_note_path("memory/n.md") == []


# -- forgotten sessions ------------------------------------------------------


async def test_forgotten_by_id_and_by_whole_key(evidence: MemoryEvidenceStore) -> None:
    assert await evidence.add_forgotten_session(session_id="id-1", session_key="key-1")
    assert not await evidence.add_forgotten_session(session_id="id-1", session_key="key-1")
    assert await evidence.add_forgotten_session(session_key="key-2")
    with pytest.raises(ValueError):
        await evidence.add_forgotten_session()

    assert await evidence.is_session_forgotten(session_id="id-1")
    # Forgetting one transcript does not forget its key's later transcripts.
    assert not await evidence.is_session_forgotten(session_id="id-new", session_key="key-1")
    # Forgetting a key covers every transcript under it.
    assert await evidence.is_session_forgotten(session_id="any", session_key="key-2")
    assert not await evidence.is_session_forgotten()

    snapshot = await evidence.forgotten_sessions()
    assert snapshot.contains(session_id="id-1")
    assert snapshot.contains(session_id="x", session_key="key-2")
    assert not snapshot.contains(session_id="x", session_key="key-1")
    assert len(snapshot) == 2


async def test_known_sessions_match_exactly(evidence: MemoryEvidenceStore) -> None:
    await evidence.add_origin(
        kind="curated", target="memory", item_hash="h", writer="tool", session=S1
    )
    await evidence.add_forgotten_session(session_id=S2.session_id, session_key=S2.session_key)
    assert await evidence.known_sessions(S1.session_id) == [S1]
    assert await evidence.known_sessions(S2.session_key) == [S2]
    assert await evidence.known_sessions(S1.session_id[:3]) == []
    assert await evidence.known_sessions("") == []


# -- cancellation leaves no SQLite lock behind ----------------------------------


def _external_write(db_path: Path) -> None:
    """A write from another connection that must not wait for a stale lock."""
    conn = sqlite3.connect(db_path, timeout=0.3)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS probe (x INTEGER)")
        conn.execute("INSERT INTO probe VALUES (1)")
        conn.commit()
    finally:
        conn.close()


def _hold_after(db: Any, predicate: Any, entered: asyncio.Event, gate: asyncio.Event) -> None:
    """Make *db* suspend right after executing a statement matching *predicate*."""
    original = db.execute

    def execute(sql: str, params: Any = ()) -> Any:
        awaitable = original(sql, params)
        if not predicate(sql):
            return awaitable

        async def held() -> Any:
            cursor = await awaitable
            entered.set()
            await gate.wait()  # the task is cancelled here
            return cursor

        return held()

    db.execute = execute


async def test_a_cancelled_single_write_leaves_no_lock_behind(tmp_path: Path) -> None:
    store = MemoryEvidenceStore(tmp_path / "memory.db")
    await store.initialize()
    entered, gate = asyncio.Event(), asyncio.Event()
    _hold_after(store._db, lambda sql: "INSERT" in sql, entered, gate)
    try:
        task = asyncio.create_task(store.add_forgotten_session(session_id="x", session_key="k"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await asyncio.to_thread(_external_write, tmp_path / "memory.db")
    finally:
        gate.set()
        await store.close()


async def test_a_cancelled_batch_rolls_back_and_leaves_no_lock(tmp_path: Path) -> None:
    store = MemoryEvidenceStore(tmp_path / "memory.db")
    await store.initialize()
    entered, gate = asyncio.Event(), asyncio.Event()
    calls = {"n": 0}

    def second_insert(sql: str) -> bool:
        if "INSERT" not in sql:
            return False
        calls["n"] += 1
        return calls["n"] == 2

    _hold_after(store._db, second_insert, entered, gate)
    origins = [
        Origin(0, "note", f"memory/{i}.md", f"h{i}", 1, "k", "i", "tool", T0) for i in range(3)
    ]
    try:
        task = asyncio.create_task(store.add_origins(origins))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await asyncio.to_thread(_external_write, tmp_path / "memory.db")
        gate.set()
        assert (await store.table_counts())["memory_origins"] == 0
    finally:
        gate.set()
        await store.close()


# -- note writes are counted (finding 1) --------------------------------------------


async def test_note_writes_are_counted_per_session_and_writer(
    evidence: MemoryEvidenceStore,
) -> None:
    common = dict(kind="note", target="memory/n.md", item_hash="h", item_len=3, writer="tool")
    for _ in range(3):
        assert await evidence.add_origin(session=S1, count_write=True, **common)
    await evidence.add_origin(session=S2, count_write=True, **common)
    assert not await evidence.add_origin(session=S2, **common)  # plain add: idempotent

    rows = await evidence.origins_for_note_path("memory/n.md")
    assert [(o.session_id, o.writes) for o in rows] == [(S1.session_id, 3), (S2.session_id, 1)]


async def test_an_older_database_gains_the_writes_column(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE memory_origins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL, target TEXT NOT NULL, item_hash TEXT NOT NULL,
            item_len INTEGER NOT NULL DEFAULT 0,
            session_key TEXT NOT NULL DEFAULT '', session_id TEXT NOT NULL DEFAULT '',
            writer TEXT NOT NULL, at REAL NOT NULL,
            UNIQUE (kind, target, item_hash, session_key, session_id, writer)
        )
        """
    )
    conn.execute(
        "INSERT INTO memory_origins (kind, target, item_hash, item_len, session_key,"
        " session_id, writer, at) VALUES ('note', 'memory/n.md', 'h', 3, 'k', 'i', 'tool', 1)"
    )
    conn.commit()
    conn.close()

    store = MemoryEvidenceStore(db)
    try:
        rows = await store.origins_for_note_path("memory/n.md")
        assert [o.writes for o in rows] == [1]
        await store.add_origin(
            kind="note",
            target="memory/n.md",
            item_hash="h",
            item_len=3,
            writer="tool",
            session=SessionRef(session_key="k", session_id="i"),
            count_write=True,
        )
        assert [o.writes for o in await store.origins_for_note_path("memory/n.md")] == [2]
    finally:
        await store.close()
