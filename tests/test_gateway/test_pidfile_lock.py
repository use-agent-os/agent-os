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
