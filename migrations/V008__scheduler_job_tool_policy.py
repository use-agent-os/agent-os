"""V008 - scheduler job tool policy.

Adds a persisted per-job tool policy column for cron jobs. JobStore also
performs an idempotent in-process migration for ad-hoc stores; this yoyo
migration keeps managed database upgrades in sync.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V007__session_cost_source_rollup"}

TABLE = "scheduler_jobs"
COLUMN = "tool_policy_json"
DDL = "TEXT NOT NULL DEFAULT '{}'"


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
    if not _has_column(conn, TABLE, COLUMN):
        conn.cursor().execute(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} {DDL}")


def rollback_step(conn) -> None:
    if not _table_exists(conn, TABLE):
        return
    if _has_column(conn, TABLE, COLUMN):
        conn.cursor().execute(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}")


steps = [step(apply_step, rollback_step)]
