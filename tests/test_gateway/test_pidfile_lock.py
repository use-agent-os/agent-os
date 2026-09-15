"""Tests for GatewayPidLock PID file placement (AC-C1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos.gateway.pidlock import GatewayPidLock


def test_pid_file_in_state_dir_not_parent(tmp_path: Path) -> None:
    """AC-C1-1/AC-C1-2: PID file must land in state_dir, not state_dir.parent."""
    state_dir = tmp_path / "state"
    lock = GatewayPidLock(state_dir)
    lock.acquire()
    try:
        # PID file must be inside state_dir
        assert (state_dir / "gateway.pid").exists(), f"gateway.pid not found in {state_dir}"
        # PID file must NOT be in the parent directory
        assert not (tmp_path / "gateway.pid").exists(), (
            f"gateway.pid incorrectly written to parent {tmp_path}"
        )
    finally:
        lock.release()


def test_release_keeps_lock_anchor_so_exclusion_survives(tmp_path: Path) -> None:
    """release() must not unlink gateway.pid.lock.

    Both platform locks (fcntl.flock, msvcrt.locking) are held on the open
    file rather than on the path. If release() removes the path, a process
    holding the old file and a process that creates a fresh one at the same
    path end up with two independent locks, and both conclude they own this
    STATE_DIR. Keeping the anchor is what makes the lock a rendezvous point.
    """
    state_dir = tmp_path / "state"
    lock = GatewayPidLock(state_dir)
    lock.acquire()
    lock_path = state_dir / "gateway.pid.lock"
    assert lock_path.exists()

    lock.release()

    assert not (state_dir / "gateway.pid").exists(), "pid file should be removed"
    assert lock_path.exists(), "lock anchor must survive release()"


def test_lock_is_reusable_across_acquire_release_cycles(tmp_path: Path) -> None:
    """Keeping the anchor must not break a normal restart."""
    state_dir = tmp_path / "state"
    for _ in range(3):
        lock = GatewayPidLock(state_dir)
        lock.acquire()
        assert (state_dir / "gateway.pid").exists()
        lock.release()


def test_second_lock_is_denied_while_first_is_held(tmp_path: Path) -> None:
    """The lock still excludes a second holder in-process."""
    state_dir = tmp_path / "state"
    first = GatewayPidLock(state_dir)
    first.acquire()
    try:
        second = GatewayPidLock(state_dir)
        with pytest.raises(SystemExit) as excinfo:
            second.acquire()
        assert excinfo.value.code == 1
    finally:
        first.release()
