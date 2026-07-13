"""V004 - schema_version column on memory tables.

Older databases created by :mod:`agentos.memory.store` carry four
tables - ``files``, ``chunks``, ``embedding_cache``, ``meta`` - WITHOUT
the ``schema_version INTEGER NOT NULL DEFAULT 1`` column. Fresh installs
pick up the column via the module DDL directly, but on-disk databases
from prior releases must be migrated in place.

This migration is idempotent: it checks ``PRAGMA table_info`` on each
target table and adds the column only if missing. Fresh-install runs
therefore no-op on every table. Rollback drops the column where the
SQLite version supports ``DROP COLUMN`` (3.35+) and otherwise falls
back to a safe no-op so the migration does not hold back a rollback on
older runtimes - the column is idempotent-add on re-apply anyway.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V003__heartbeat_ticks"}


MEMORY_TABLES: tuple[str, ...] = ("files", "chunks", "embedding_cache", "meta")
COLUMN = "schema_version"
ADD_COLUMN_DDL = f"ALTER TABLE {{table}} ADD COLUMN {COLUMN} INTEGER NOT NULL DEFAULT 1"
DROP_COLUMN_DDL = f"ALTER TABLE {{table}} DROP COLUMN {COLUMN}"


def _table_exists(conn, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def _has_column(conn, table: str, column: str) -> bool:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def _sqlite_version(conn) -> tuple[int, int, int]:
    cur = conn.cursor()
    cur.execute("SELECT sqlite_version()")
    raw = cur.fetchone()[0]
    parts = raw.split(".")
    while len(parts) < 3:
        parts.append("0")
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def apply_step(conn) -> None:
    cur = conn.cursor()
    for table in MEMORY_TABLES:
        if not _table_exists(conn, table):
            continue
        if _has_column(conn, table, COLUMN):
            continue
        cur.execute(ADD_COLUMN_DDL.format(table=table))


def rollback_step(conn) -> None:
    version = _sqlite_version(conn)
    supports_drop = version >= (3, 35, 0)
    if not supports_drop:
        return
    cur = conn.cursor()
    for table in MEMORY_TABLES:
        if not _table_exists(conn, table):
            continue
        if not _has_column(conn, table, COLUMN):
            continue
        cur.execute(DROP_COLUMN_DDL.format(table=table))


steps = [step(apply_step, rollback_step)]
