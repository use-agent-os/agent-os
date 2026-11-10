"""V015 - outbox of parent wakes for subagent spawn groups.

One row per spawn group (``parent_session_key::parent_task_id``): ``awaiting``
when the parent closed the group while children still ran (so a crash before
the last child's announce cannot lose the wake; boot reconciles these rows),
``pending`` until the task runtime accepted the wake, then ``delivered``;
``dropped`` when delivery was given up on or must not happen (the parent was
aborted). The delivery worker in ``agentos.gateway.subagent_wakes`` retries
pending rows with backoff and drains them at startup, so a wake survives a
transient enqueue failure and a restart; a delivered row keeps a group from
waking its parent twice.

``agentos.session.storage`` also creates the table on connect
(``CREATE TABLE IF NOT EXISTS``, same DDL).
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V014__gateway_boots"}

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS subagent_wakes (
    group_key TEXT PRIMARY KEY,
    parent_session_key TEXT NOT NULL,
    parent_task_id TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    message TEXT NOT NULL DEFAULT '',
    provenance TEXT NOT NULL DEFAULT '{}',
    payloads TEXT NOT NULL DEFAULT '[]',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
)
"""
CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_subagent_wakes_due ON subagent_wakes(state, next_attempt_at)"
)


def apply_step(conn) -> None:
    cur = conn.cursor()
    cur.execute(CREATE_TABLE)
    cur.execute(CREATE_INDEX)


def rollback_step(conn) -> None:
    cur = conn.cursor()
    cur.execute("DROP INDEX IF EXISTS idx_subagent_wakes_due")
    cur.execute("DROP TABLE IF EXISTS subagent_wakes")


steps = [step(apply_step, rollback_step)]
