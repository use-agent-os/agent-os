"""V003 - heartbeat_ticks table for the S8 heartbeat runner.

Creates the ``heartbeat_ticks`` table that :class:`HeartbeatStore` writes into.
The table carries ``schema_version`` per S-MIGRATE discipline so any future
shape change goes through a migration rather than an in-product ALTER TABLE.

Rollback drops the table outright (no data preservation - ticks are
observability records, not source of truth).
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V002__scheduler_session_fields"}


TABLE = "heartbeat_ticks"
CREATE_DDL = f"""
CREATE TABLE {TABLE} (
    id TEXT PRIMARY KEY,
    emitted_at TEXT NOT NULL,
    priority_band TEXT NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 0,
    schema_version INTEGER NOT NULL DEFAULT 1,
    payload TEXT NOT NULL DEFAULT '{{}}'
)
"""


def _table_exists(conn, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def apply_step(conn) -> None:
    if _table_exists(conn, TABLE):
        return
    cur = conn.cursor()
    cur.execute(CREATE_DDL)


def rollback_step(conn) -> None:
    if not _table_exists(conn, TABLE):
        return
    cur = conn.cursor()
    cur.execute(f"DROP TABLE {TABLE}")


steps = [step(apply_step, rollback_step)]
