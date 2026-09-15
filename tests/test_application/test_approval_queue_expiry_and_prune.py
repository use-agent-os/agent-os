"""Issue #1987: aged-out approvals are denied without a waiter, and resolved
rows are pruned so ``approval_queue.sqlite`` stops growing without bound.

``wait()`` was the only path that denied an approval once its lifespan
(``created_at + default_timeout``) elapsed. When no caller was waiting -- the
tool's bounded wait had already returned, or the process restarted -- the row
stayed ``resolved = 0`` forever and the Web UI's pending feed showed a prompt
that could never do anything. Nothing ever deleted a resolved row either.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from agentos.application.approval_queue import ApprovalQueue


def _backdate(queue: ApprovalQueue, approval_id: str, *, seconds: float) -> None:
    queue._conn.execute(
        "UPDATE approval_queue SET created_at = ? WHERE approval_id = ?",
        (time.time() - seconds, approval_id),
    )
    queue._conn.commit()


def _row_count(queue: ApprovalQueue) -> int:
    return int(queue._conn.execute("SELECT COUNT(*) FROM approval_queue").fetchone()[0])


def test_list_pending_denies_expired_approvals_without_a_waiter(tmp_path) -> None:
    queue = ApprovalQueue(default_timeout=300.0, db_path=str(tmp_path / "aq.sqlite"))
    expired = queue.request("exec", {"toolName": "exec_command"})
    fresh = queue.request("exec", {"toolName": "exec_command"})
    _backdate(queue, expired, seconds=1000.0)

    pending = queue.list_pending()

    assert [p["id"] for p in pending] == [fresh]
    entry = queue.get(expired)
    assert entry.resolved is True
    assert entry.approved is False
    # The dead prompt can no longer be approved after the fact.
    with pytest.raises(ValueError, match="already resolved"):
        queue.resolve(expired, True)
    queue.close()


def test_list_pending_with_namespace_also_reaps(tmp_path) -> None:
    queue = ApprovalQueue(default_timeout=300.0, db_path=str(tmp_path / "aq.sqlite"))
    expired = queue.request("plugin", {"name": "x"})
    _backdate(queue, expired, seconds=1000.0)

    assert queue.list_pending("plugin") == []
    assert queue.get(expired).resolved is True
    queue.close()


def test_request_reaps_expired_approvals(tmp_path) -> None:
    queue = ApprovalQueue(default_timeout=300.0, db_path=str(tmp_path / "aq.sqlite"))
    expired = queue.request("exec", {"toolName": "exec_command"})
    _backdate(queue, expired, seconds=1000.0)

    queue.request("exec", {"toolName": "exec_command"})

    entry = queue.get(expired)
    assert entry.resolved is True
    assert entry.approved is False
    queue.close()


def test_startup_reaps_approvals_that_expired_while_the_process_was_down(tmp_path) -> None:
    db_path = tmp_path / "aq.sqlite"
    queue = ApprovalQueue(default_timeout=300.0, db_path=str(db_path))
    expired = queue.request("exec", {"toolName": "exec_command"})
    _backdate(queue, expired, seconds=1000.0)
    queue.close()

    reopened = ApprovalQueue(default_timeout=300.0, db_path=str(db_path))

    entry = reopened.get(expired)
    assert entry.resolved is True
    assert entry.approved is False
    assert reopened.list_pending() == []
    reopened.close()


def test_approval_within_its_lifespan_stays_pending(tmp_path) -> None:
    queue = ApprovalQueue(default_timeout=300.0, db_path=str(tmp_path / "aq.sqlite"))
    approval_id = queue.request("exec", {"toolName": "exec_command"})
    _backdate(queue, approval_id, seconds=100.0)

    assert [p["id"] for p in queue.list_pending()] == [approval_id]
    assert queue.get(approval_id).resolved is False
    # A human can still resolve it after the tool's bounded wait returned.
    queue.resolve(approval_id, True)
    assert queue.get(approval_id).approved is True
    queue.close()


@pytest.mark.asyncio
async def test_reaping_wakes_an_in_process_waiter(tmp_path) -> None:
    # A poll interval far longer than the assertion budget, so the waiter can
    # only return in time if the sweep set its event the way a resolve would.
    queue = ApprovalQueue(
        default_timeout=300.0, db_path=str(tmp_path / "aq.sqlite"), poll_interval=30.0
    )
    approval_id = queue.request("exec", {"toolName": "exec_command"})
    waiter = asyncio.ensure_future(queue.wait(approval_id, timeout=60.0))
    await asyncio.sleep(0.02)

    _backdate(queue, approval_id, seconds=1000.0)
    queue.list_pending()

    assert queue._pending[approval_id]._event.is_set()
    assert await asyncio.wait_for(waiter, timeout=1.0) is False
    queue.close()


def test_sweep_is_a_plain_read_when_nothing_is_due(tmp_path) -> None:
    queue = ApprovalQueue(default_timeout=300.0, db_path=str(tmp_path / "aq.sqlite"))
    approval_id = queue.request("exec", {"toolName": "exec_command"})

    # Another connection holding the write lock must not stall a poll.
    other = sqlite3.connect(str(tmp_path / "aq.sqlite"), timeout=0.0)
    other.execute("BEGIN IMMEDIATE")
    try:
        assert [p["id"] for p in queue.list_pending()] == [approval_id]
    finally:
        other.rollback()
        other.close()
    queue.close()


class _FailingWrite:
    """Connection proxy whose expiry UPDATE fails once, mid-transaction."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
        if sql.startswith("UPDATE approval_queue SET resolved = 1, approved = 0"):
            raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)


def test_sweep_failure_rolls_the_transaction_back(tmp_path) -> None:
    queue = ApprovalQueue(default_timeout=300.0, db_path=str(tmp_path / "aq.sqlite"))
    expired = queue.request("exec", {"toolName": "exec_command"})
    _backdate(queue, expired, seconds=1000.0)

    conn = queue._conn
    queue._conn = _FailingWrite(conn)  # type: ignore[assignment]
    try:
        with pytest.raises(sqlite3.OperationalError):
            queue.list_pending()
    finally:
        queue._conn = conn

    # The connection is not stuck inside a transaction: the next write path
    # (which opens its own BEGIN IMMEDIATE) still works and the sweep completes.
    fresh = queue.request("exec", {"toolName": "exec_command"})
    assert [p["id"] for p in queue.list_pending()] == [fresh]
    assert queue.get(expired).resolved is True
    queue.close()


def test_resolved_rows_older_than_the_retention_window_are_pruned(tmp_path) -> None:
    queue = ApprovalQueue(
        default_timeout=300.0,
        db_path=str(tmp_path / "aq.sqlite"),
        resolved_retention=3600.0,
    )
    old_approved = queue.request("exec", {"toolName": "exec_command"})
    queue.resolve(old_approved, True)
    queue.consume(old_approved)
    old_denied = queue.request("exec", {"toolName": "exec_command"})
    queue.resolve(old_denied, False)
    recent = queue.request("exec", {"toolName": "exec_command"})
    queue.resolve(recent, True)
    _backdate(queue, old_approved, seconds=7200.0)
    _backdate(queue, old_denied, seconds=7200.0)

    queue.list_pending()

    with pytest.raises(KeyError):
        queue.get(old_approved)
    with pytest.raises(KeyError):
        queue.get(old_denied)
    assert queue.get(recent).approved is True
    assert _row_count(queue) == 1
    queue.close()


def test_expired_pending_row_past_retention_is_denied_and_pruned(tmp_path) -> None:
    # A forgotten prompt from days ago: denied by the reap, then gone.
    queue = ApprovalQueue(
        default_timeout=300.0,
        db_path=str(tmp_path / "aq.sqlite"),
        resolved_retention=3600.0,
    )
    stale = queue.request("exec", {"toolName": "exec_command"})
    _backdate(queue, stale, seconds=7200.0)

    assert queue.list_pending() == []
    with pytest.raises(KeyError):
        queue.get(stale)
    assert _row_count(queue) == 0
    queue.close()


def test_retention_never_undercuts_the_approval_lifespan(tmp_path) -> None:
    # A row resolved late in its lifespan must survive long enough to be
    # consumed, so the window can never be shorter than default_timeout.
    queue = ApprovalQueue(
        default_timeout=300.0,
        db_path=str(tmp_path / "aq.sqlite"),
        resolved_retention=1.0,
    )
    approval_id = queue.request("exec", {"toolName": "exec_command"})
    queue.resolve(approval_id, True)
    _backdate(queue, approval_id, seconds=200.0)

    queue.list_pending()

    queue.consume(approval_id)
    assert queue.get(approval_id).consumed is True
    queue.close()
