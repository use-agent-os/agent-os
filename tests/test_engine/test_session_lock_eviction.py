"""Regression tests for SessionWriteLock memory leak fix (issue #966).

Before the fix, ``SessionWriteLock._locks`` grew without bound: every
session_key that was ever acquired stayed in the dict forever, even after
the lock was released.  The fix evicts an entry as soon as it has no
pending waiters, so the dict is bounded by the number of *active* sessions,
not by all sessions ever seen.

Two properties are pinned:
1. Leak regression: sequential acquire/release of N distinct keys leaves the
   dict empty so it does not grow with session count.
2. Concurrent safety: concurrent acquire() calls for the same session_key are
   serialized — no two tasks hold the lock simultaneously (no split-brain).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agentos.engine.session_lock import SessionWriteLock


@pytest.mark.asyncio
async def test_sequential_acquire_release_evicts_all_keys():
    """After sequential acquire/release of N distinct session_keys, the
    internal dict must be empty so it does not grow with session count."""
    lock_mgr = SessionWriteLock()
    for i in range(100):
        await lock_mgr.acquire(f"session-{i}")
        lock_mgr.release(f"session-{i}")
    assert len(lock_mgr._locks) == 0, (
        f"expected 0 locks after sequential release, got {len(lock_mgr._locks)}"
    )


@pytest.mark.asyncio
async def test_concurrent_acquires_are_serialized():
    """Two concurrent tasks acquiring the same session_key must not run their
    critical sections simultaneously — the lock serializes them (no split-brain).

    Strategy: task A acquires the lock and holds it; task B calls acquire()
    and is queued as a waiter.  The critical-section timestamp of B must be
    strictly after A's, proving B was blocked while A held the lock.
    """
    lock_mgr = SessionWriteLock()
    timestamps: list[float] = []

    async def task_a() -> None:
        await lock_mgr.acquire("shared-key")
        timestamps.append(time.monotonic())
        await asyncio.sleep(0.05)  # hold lock long enough for task_b to queue
        lock_mgr.release("shared-key")

    async def task_b() -> None:
        await asyncio.sleep(0.01)  # ensure task_a acquires first
        await lock_mgr.acquire("shared-key")
        timestamps.append(time.monotonic())
        lock_mgr.release("shared-key")

    await asyncio.gather(task_a(), task_b())
    assert len(timestamps) == 2, f"expected 2 timestamps, got {len(timestamps)}"
    assert timestamps[1] > timestamps[0] + 0.04, (
        f"task_b ran before task_a released the lock (split-brain): {timestamps}"
    )


@pytest.mark.asyncio
async def test_context_manager_evicts_after_release():
    """Using the lock via the context manager also evicts idle entries."""
    lock_mgr = SessionWriteLock()
    for i in range(50):
        async with lock_mgr.context(f"ctx-session-{i}"):
            pass
    assert len(lock_mgr._locks) == 0


@pytest.mark.asyncio
async def test_double_release_is_safe():
    """Calling release() twice on the same key must not raise."""
    lock_mgr = SessionWriteLock()
    await lock_mgr.acquire("session-double")
    lock_mgr.release("session-double")
    lock_mgr.release("session-double")  # must not raise RuntimeError
    assert len(lock_mgr._locks) == 0


@pytest.mark.asyncio
async def test_many_sessions_all_evicted():
    """1000 sequential sessions must all be evicted, bounding dict to O(1)."""
    lock_mgr = SessionWriteLock()
    for i in range(1000):
        await lock_mgr.acquire(f"key-{i}")
        lock_mgr.release(f"key-{i}")
    assert len(lock_mgr._locks) == 0
    # Even mid-batch, dict should never exceed a small constant.
    assert len(lock_mgr._locks) <= 1


@pytest.mark.asyncio
async def test_waiter_cancelled_before_release_does_not_leak_lock():
    """When a queued waiter is cancelled before the current holder releases,
    the idle lock must still be evicted from _locks upon release (issue #3007)."""
    lock_mgr = SessionWriteLock()
    await lock_mgr.acquire("session-cancel-1")

    async def waiter() -> None:
        await lock_mgr.acquire("session-cancel-1")
        lock_mgr.release("session-cancel-1")

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.01)

    # Cancel waiter while holder holds lock
    task.cancel()

    # Release holder
    lock_mgr.release("session-cancel-1")

    with pytest.raises(asyncio.CancelledError):
        await task

    assert "session-cancel-1" not in lock_mgr._locks, (
        "Idle lock was not evicted after cancelled waiter"
    )
    assert len(lock_mgr._locks) == 0


@pytest.mark.asyncio
async def test_waiter_cancelled_during_acquire_does_not_leak_lock():
    """When a waiting task is cancelled while blocked on acquire(), it must
    cleanly evict the idle lock on exit if no other waiters exist."""
    lock_mgr = SessionWriteLock()
    await lock_mgr.acquire("session-cancel-2")

    async def waiter() -> None:
        await lock_mgr.acquire("session-cancel-2")
        lock_mgr.release("session-cancel-2")

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.01)
    lock_mgr.release("session-cancel-2")
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert "session-cancel-2" not in lock_mgr._locks, (
        "Idle lock was not evicted after waiter cancellation"
    )
    assert len(lock_mgr._locks) == 0


@pytest.mark.asyncio
async def test_multiple_waiters_one_cancelled_one_succeeds():
    """When multiple waiters are queued and one is cancelled, the remaining
    waiter must still acquire the lock safely and evict it when done."""
    lock_mgr = SessionWriteLock()
    await lock_mgr.acquire("session-multi")

    order: list[str] = []

    async def waiter1() -> None:
        await lock_mgr.acquire("session-multi")
        order.append("w1")
        lock_mgr.release("session-multi")

    async def waiter2() -> None:
        await lock_mgr.acquire("session-multi")
        order.append("w2")
        lock_mgr.release("session-multi")

    t1 = asyncio.create_task(waiter1())
    t2 = asyncio.create_task(waiter2())
    await asyncio.sleep(0.01)

    # Cancel waiter1
    t1.cancel()

    # Release initial holder
    lock_mgr.release("session-multi")

    with pytest.raises(asyncio.CancelledError):
        await t1

    await t2
    assert order == ["w2"], f"expected w2 to acquire lock, got {order}"
    assert "session-multi" not in lock_mgr._locks
    assert len(lock_mgr._locks) == 0

