from __future__ import annotations

import asyncio
import time

import pytest

from agentos.application.approval_queue import ApprovalQueue


def _queue(tmp_path, **kw) -> ApprovalQueue:
    return ApprovalQueue(
        default_timeout=kw.pop("default_timeout", 300.0),
        db_path=str(tmp_path / "approval_queue.sqlite"),
        poll_interval=0.01,
        **kw,
    )


def test_reap_stale_denies_expired_approval_with_no_waiter(tmp_path) -> None:
    """An approval that aged out with nobody waiting must not stay pending forever.

    Previously only ``wait()`` denied a timed-out approval, so a row that
    aged out with no active waiter stayed ``resolved=0`` and the Web UI
    listed it as pending indefinitely.
    """
    queue = _queue(tmp_path, default_timeout=300.0)
    approval_id = queue.request("exec", {"toolName": "exec_command"})
    queue._conn.execute(
        "UPDATE approval_queue SET created_at = ? WHERE approval_id = ?",
        (time.time() - 1000.0, approval_id),
    )
    queue._conn.commit()

    reaped = queue.reap_stale()
    assert reaped == 1

    entry = queue.get(approval_id)
    assert entry.resolved is True
    assert entry.approved is False

    # The wait call after the reap observes the denial instead of hanging.

    assert asyncio.run(queue.wait(approval_id, timeout=0.05)) is False
    assert queue.list_pending() == []
    queue.close()


def test_reap_stale_wakes_an_active_waiter(tmp_path) -> None:
    """reap_stale sets the in-memory event so a concurrent wait() returns."""

    queue = _queue(tmp_path, default_timeout=300.0)
    approval_id = queue.request("exec", {"toolName": "exec_command"})
    queue._conn.execute(
        "UPDATE approval_queue SET created_at = ? WHERE approval_id = ?",
        (time.time() - 1000.0, approval_id),
    )
    queue._conn.commit()

    async def scenario() -> None:
        task = asyncio.create_task(queue.wait(approval_id, timeout=5.0))
        await asyncio.sleep(0.05)
        # Denied behind the waiter's back, as another thread would.
        reaped = await asyncio.to_thread(queue.reap_stale)
        assert reaped == 1
        assert await task is False

    asyncio.run(scenario())
    queue.close()


def test_list_pending_denies_expired_rows(tmp_path) -> None:
    """The Web UI feed itself reaps, so stale approvals never render as pending."""
    queue = _queue(tmp_path, default_timeout=300.0)
    stale_id = queue.request("exec", {"toolName": "exec_command"})
    queue._conn.execute(
        "UPDATE approval_queue SET created_at = ? WHERE approval_id = ?",
        (time.time() - 1000.0, stale_id),
    )
    queue._conn.commit()
    fresh_id = queue.request("exec", {"toolName": "exec_command"})

    pending = queue.list_pending()
    assert [p["id"] for p in pending] == [fresh_id]

    entry = queue.get(stale_id)
    assert entry.resolved is True
    assert entry.approved is False
    queue.close()


def test_purge_resolved_keeps_recent_tail(tmp_path) -> None:
    """Resolved history is trimmed to the most recent 32 rows."""
    queue = _queue(tmp_path, default_timeout=300.0)
    for _ in range(40):
        aid = queue.request("exec", {"toolName": "exec_command"})
        queue.resolve(aid, True)
    queue.purge_resolved(keep=32)

    rows = queue._conn.execute(
        "SELECT COUNT(*) AS n FROM approval_queue WHERE resolved = 1"
    ).fetchone()
    assert rows["n"] == 32
    queue.close()


def test_startup_reaps_and_purges(tmp_path) -> None:
    """A queue opened over a stale file denies the expired rows on startup."""
    queue = _queue(tmp_path, default_timeout=300.0)
    stale_id = queue.request("exec", {"toolName": "exec_command"})
    queue._conn.execute(
        "UPDATE approval_queue SET created_at = ? WHERE approval_id = ?",
        (time.time() - 1000.0, stale_id),
    )
    queue._conn.commit()
    queue.close()

    reopened = _queue(tmp_path, default_timeout=300.0)
    entry = reopened.get(stale_id)
    assert entry.resolved is True
    assert entry.approved is False
    reopened.close()


@pytest.mark.asyncio
async def test_wait_per_call_timeout_leaves_approval_pending_when_lifetime_not_expired(
    tmp_path,
) -> None:
    db_path = tmp_path / "approval_queue.sqlite"
    queue = ApprovalQueue(default_timeout=300.0, db_path=str(db_path), poll_interval=0.01)
    approval_id = queue.request("exec", {"toolName": "exec_command"})

    result = await queue.wait(approval_id, timeout=0.05)

    assert result is False
    entry = queue.get(approval_id)
    assert entry.resolved is False
    assert entry.approved is False

    # A human operator resolving after the caller's bounded wait returned
    # must still succeed -- it must not have been permanently denied.
    queue.resolve(approval_id, True)
    resolved_entry = queue.get(approval_id)
    assert resolved_entry.resolved is True
    assert resolved_entry.approved is True
    queue.close()


@pytest.mark.asyncio
async def test_wait_denies_once_the_overall_approval_lifetime_has_expired(tmp_path) -> None:
    db_path = tmp_path / "approval_queue.sqlite"
    queue = ApprovalQueue(default_timeout=300.0, db_path=str(db_path), poll_interval=0.01)
    approval_id = queue.request("exec", {"toolName": "exec_command"})

    # Simulate the approval having been created long ago, so its overall
    # lifespan (created_at + default_timeout) has already elapsed.
    queue._conn.execute(
        "UPDATE approval_queue SET created_at = ? WHERE approval_id = ?",
        (time.time() - 1000.0, approval_id),
    )
    queue._conn.commit()

    result = await queue.wait(approval_id, timeout=0.05)

    assert result is False
    entry = queue.get(approval_id)
    assert entry.resolved is True
    assert entry.approved is False

    with pytest.raises(ValueError, match="already resolved"):
        queue.resolve(approval_id, True)
    queue.close()


@pytest.mark.asyncio
async def test_wait_returns_immediately_once_resolved_during_the_call(tmp_path) -> None:
    db_path = tmp_path / "approval_queue.sqlite"
    queue = ApprovalQueue(default_timeout=300.0, db_path=str(db_path), poll_interval=0.01)
    approval_id = queue.request("exec", {"toolName": "exec_command"})
    queue.resolve(approval_id, True)

    result = await queue.wait(approval_id, timeout=5.0)

    assert result is True
    queue.close()


@pytest.mark.asyncio
async def test_wait_default_timeout_denies_even_when_the_wall_clock_does_not_tick(
    tmp_path, monkeypatch
) -> None:
    """Windows' time.time() advances in ~15.6ms steps, so after a short wait it
    can still read the approval as younger than its lifespan. A frozen wall
    clock is the extreme form of that: the wait deadline (monotonic) elapses
    while time.time() - created_at stays 0, and the approval must still be
    denied rather than left pending forever."""
    db_path = tmp_path / "approval_queue.sqlite"
    queue = ApprovalQueue(default_timeout=0.02, db_path=str(db_path), poll_interval=0.01)
    approval_id = queue.request("exec", {"toolName": "exec_command"})
    frozen = time.time()
    monkeypatch.setattr(time, "time", lambda: frozen)

    result = await queue.wait(approval_id)

    assert result is False
    entry = queue.get(approval_id)
    assert entry.resolved is True
    assert entry.approved is False
    queue.close()
