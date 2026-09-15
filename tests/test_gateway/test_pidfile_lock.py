"""Tests for GatewayPidLock PID file placement, exclusion, and lifecycle."""

from __future__ import annotations

import atexit
from pathlib import Path
from unittest.mock import patch

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


def test_losing_starter_does_not_delete_the_running_pid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A starter that cannot win the lock must leave the STATE_DIR alone.

    Simulates a false-negative liveness probe -- a PID namespace boundary, a
    container, or a restricted Windows process handle. The running gateway's
    pid looks dead to the starter, which previously unlinked the pid file and
    only then discovered it could not have the lock. The running gateway kept
    running with no pid file, so `gateway status` and the next start attempt
    both lost track of it.
    """
    state_dir = tmp_path / "state"
    running = GatewayPidLock(state_dir)
    running.acquire()
    pid_path = state_dir / "gateway.pid"
    recorded = pid_path.read_bytes()

    monkeypatch.setattr("agentos.gateway.pidlock._is_alive", lambda _pid: False)
    try:
        starter = GatewayPidLock(state_dir)
        with pytest.raises(SystemExit) as excinfo:
            starter.acquire()
        assert excinfo.value.code == 1
        assert pid_path.exists(), "losing starter must not delete the pid file"
        assert pid_path.read_bytes() == recorded, "pid file must be untouched"
    finally:
        running.release()


def test_losing_starter_reports_the_holder_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The error names the real holder instead of 'unknown'."""
    state_dir = tmp_path / "state"
    running = GatewayPidLock(state_dir)
    running.acquire()

    monkeypatch.setattr("agentos.gateway.pidlock._is_alive", lambda _pid: False)
    try:
        with pytest.raises(SystemExit):
            GatewayPidLock(state_dir).acquire()
        message = capsys.readouterr().err
        assert f"pid={running.pid}" in message
        assert "pid=unknown" not in message
    finally:
        running.release()


def test_stale_pid_file_is_still_reclaimed(tmp_path: Path) -> None:
    """A genuinely dead predecessor must not block startup."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "gateway.pid").write_text('{"pid": 999999999, "start_ts": "x"}')

    lock = GatewayPidLock(state_dir)
    lock.acquire()
    try:
        assert lock.pid is not None
    finally:
        lock.release()


def test_acquire_does_not_install_signal_handlers(tmp_path: Path) -> None:
    """The pid lock must leave SIGINT/SIGTERM to whoever owns the loop.

    acquire() runs during startup, well before uvicorn.Server.serve() calls
    install_signal_handlers(), so any handler registered here was overwritten
    moments later and never fired. It would have been harmful if it had won:
    it reset the disposition to SIG_DFL and re-raised, killing the process
    with no lifespan shutdown, no DB close and no WebSocket drain.
    """
    import signal

    watched = [signal.SIGTERM, signal.SIGINT]
    before = {sig: signal.getsignal(sig) for sig in watched}

    lock = GatewayPidLock(tmp_path / "state")
    lock.acquire()
    try:
        after = {sig: signal.getsignal(sig) for sig in watched}
        assert after == before, "acquire() must not replace signal dispositions"
    finally:
        lock.release()


def test_release_still_runs_via_atexit_registration(tmp_path: Path) -> None:
    """atexit remains the cleanup path, so the lock is still released."""
    with patch.object(atexit, "register") as register:
        lock = GatewayPidLock(tmp_path / "state")
        lock.acquire()
        try:
            assert register.called, "release must still be registered with atexit"
            assert register.call_args.args[0] == lock.release
        finally:
            lock.release()
