"""V016 - pinned sessions.

Adds the nullable ``sessions.pinned_at`` column: the epoch-ms time a session
was pinned to the top of the session lists, NULL when it is not pinned. The
pin lives on the row so every client (Web UI, desktop app, CLI, the agent's
``session_pin`` tool) shares it. Existing sessions need no backfill: NULL is
"not pinned".

``agentos.session.storage`` also adds the column on connect.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V015__subagent_wakes"}


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
    if _table_exists(conn, "sessions") and not _has_column(conn, "sessions", "pinned_at"):
        conn.cursor().execute("ALTER TABLE sessions ADD COLUMN pinned_at INTEGER")


def rollback_step(conn) -> None:
    # Left in place, as V011 leaves ``project_id``: a stray nullable column is
    # harmless to prior code, and DROP COLUMN rewrites the table.
    return None


steps = [step(apply_step, rollback_step)]
