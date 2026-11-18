"""``LongTermMemoryStore`` serialises its writes on its single connection.

``index_file`` holds an explicit transaction across several awaits. Without one
lock over every write, a second ``index_file`` failed its ``BEGIN`` ("cannot
start a transaction within a transaction" -- seen live from the session
indexer), and a ``remove_file`` (forget, ``sessions.delete``) committed half of
another coroutine's index update.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

from agentos.memory.store import LongTermMemoryStore
from agentos.memory.types import MemorySource


async def test_concurrent_index_writes_do_not_collide(tmp_path: Path) -> None:
    store = LongTermMemoryStore(tmp_path / "memory.db")
    await store.initialize()
    try:
        await asyncio.gather(
            *(
                store.index_file(f"memory/n{i}.md", f"note number {i}", source=MemorySource.memory)
                for i in range(6)
            ),
            *(store.remove_file(f"memory/gone{i}.md") for i in range(3)),
        )
        assert await store.file_count() == 6
    finally:
        await store.close()


def _committed_chunks(db_path: Path, path: str) -> int:
    conn = sqlite3.connect(db_path, timeout=1)
    try:
        return conn.execute("SELECT COUNT(*) FROM chunks WHERE path = ?", (path,)).fetchone()[0]
    finally:
        conn.close()


async def test_remove_file_cannot_commit_an_open_index_transaction(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    store = LongTermMemoryStore(db_path)
    await store.initialize()
    entered, gate = asyncio.Event(), asyncio.Event()
    db: Any = store._db
    original = db.execute

    def execute(sql: str, params: Any = ()) -> Any:
        awaitable = original(sql, params)
        if "INTO chunks_fts" not in sql:
            return awaitable

        async def held() -> Any:
            cursor = await awaitable
            entered.set()
            await gate.wait()
            return cursor

        return held()

    db.execute = execute
    try:
        indexing = asyncio.create_task(
            store.index_file("memory/a.md", "alpha note", source=MemorySource.memory)
        )
        await entered.wait()
        removing = asyncio.create_task(store.remove_file("memory/other.md"))
        await asyncio.sleep(0.05)

        # Nothing of the half-written index update is visible to others yet.
        assert await asyncio.to_thread(_committed_chunks, db_path, "memory/a.md") == 0

        gate.set()
        await asyncio.gather(indexing, removing)
        assert await asyncio.to_thread(_committed_chunks, db_path, "memory/a.md") == 1
    finally:
        gate.set()
        await store.close()
