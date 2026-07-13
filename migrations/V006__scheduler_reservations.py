"""V006 - scheduler reservation metadata.

Adds persisted reservation fields to ``scheduler_jobs`` so cron execution can
claim a job durably before handler execution. The in-process JobStore migration
also adds these columns for fresh or ad-hoc stores; this yoyo migration keeps
managed database upgrades in sync.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V005__agent_tasks"}

TABLE = "scheduler_jobs"
COLUMNS = [
    ("reservation_token", "TEXT NOT NULL DEFAULT ''"),
    ("reserved_at", "TEXT"),
    ("reserved_by", "TEXT NOT NULL DEFAULT ''"),
    ("reservation_source", "TEXT NOT NULL DEFAULT ''"),
    ("scheduled_run_at", "TEXT"),
]


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


def apply_step(conn) -> None:
    if not _table_exists(conn, TABLE):
        return
    cur = conn.cursor()
    for column, ddl in COLUMNS:
        if not _has_column(conn, TABLE, column):
            cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN {column} {ddl}")


def rollback_step(conn) -> None:
    if not _table_exists(conn, TABLE):
        return
    cur = conn.cursor()
    for column, _ddl in reversed(COLUMNS):
        if _has_column(conn, TABLE, column):
            cur.execute(f"ALTER TABLE {TABLE} DROP COLUMN {column}")


steps = [step(apply_step, rollback_step)]
