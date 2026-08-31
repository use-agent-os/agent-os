"""Tests for GatewayPidLock: PID file placement (AC-C1) and mutual exclusion.

The lock is what stops two gateways from sharing one state directory, and
everything downstream trusts it: the CLI, `gateway start`, and the desktop
shell all assume that a second instance cannot come up against the same
AgentOS home. That guarantee is an OS-level `flock`/`msvcrt.locking` call, so
it can only be exercised by really running a second process — which is what
the cross-process tests below do.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentos.gateway.pidlock import GatewayPidLock

# Generous: a cold interpreter start on a loaded CI runner is the slow part,
# not the lock itself. The tests never wait this long when they pass.
_HOLDER_TIMEOUT = 30.0
_POLL = 0.02

# Runs in a second process: take the lock, announce it, then hold it until the
# test says to let go. The self-imposed deadline means a holder can never
# outlive the test session even if the parent dies without cleaning up.
_HOLD_SCRIPT = """
import sys
import time
from pathlib import Path

from agentos.gateway.pidlock import GatewayPidLock

state_dir, ready, stop = (Path(argument) for argument in sys.argv[1:4])

lock = GatewayPidLock(state_dir)
lock.acquire()
ready.touch()

deadline = time.monotonic() + 120
while not stop.exists() and time.monotonic() < deadline:
    time.sleep(0.02)

lock.release()
"""


@contextlib.contextmanager
def _gateway_holding(state_dir: Path, signals: Path) -> Iterator[int]:
    """Run a second process holding the lock on ``state_dir``; yield its pid."""
    ready = signals / f"ready-{state_dir.name}"
    stop = signals / f"stop-{state_dir.name}"

    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_SCRIPT, str(state_dir), str(ready), str(stop)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + _HOLDER_TIMEOUT
        while not ready.exists():
            if holder.poll() is not None:
                stdout, stderr = holder.communicate()
                raise AssertionError(
                    f"the holder exited before acquiring the lock "
                    f"(code {holder.returncode}): {stderr or stdout}"
                )
            if time.monotonic() > deadline:
                raise AssertionError("the holder never acquired the lock")
            time.sleep(_POLL)

        yield _recorded_pid(state_dir)
    finally:
        stop.touch()
        try:
            holder.wait(timeout=_HOLDER_TIMEOUT)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait()


def _recorded_pid(state_dir: Path) -> int:
    pid_path = state_dir / "gateway.pid"
    assert pid_path.exists(), f"no gateway.pid in {state_dir}"
    return int(json.loads(pid_path.read_bytes())["pid"])


def _a_dead_pid() -> int:
    """A pid that named a real process and no longer does."""
    finished = subprocess.Popen([sys.executable, "-c", ""])
    finished.wait()
    return finished.pid


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


def test_a_second_gateway_in_the_same_state_dir_is_refused(tmp_path: Path) -> None:
    """The guarantee the whole one-gateway-per-home design rests on."""
    state_dir = tmp_path / "state"

    with _gateway_holding(state_dir, tmp_path) as holder_pid:
        with pytest.raises(SystemExit) as refused:
            GatewayPidLock(state_dir).acquire()

        assert refused.value.code == 1
        # The loser must not have disturbed the winner on its way out: a
        # second instance that clears the pid file leaves the running gateway
        # invisible to everything that looks it up.
        assert _recorded_pid(state_dir) == holder_pid


def test_the_lock_refuses_a_second_gateway_even_without_a_pid_file(tmp_path: Path) -> None:
    """The pid file is a convenience; the OS lock is the actual guarantee.

    A crash between removing the pid file and releasing the lock, or an
    operator deleting the file to "fix" a stuck gateway, must not let a second
    instance in while the first is still running.
    """
    state_dir = tmp_path / "state"

    with _gateway_holding(state_dir, tmp_path):
        (state_dir / "gateway.pid").unlink()

        # Refusal is the contract; how it surfaces is platform-dependent.
        # POSIX reports the failed flock as SystemExit(1), while Windows may
        # refuse to reopen the locked file at all. Both mean "did not start".
        with pytest.raises((SystemExit, OSError)):
            GatewayPidLock(state_dir).acquire()


def test_a_stale_pid_file_from_a_dead_gateway_is_reclaimed(tmp_path: Path) -> None:
    """After a crash or a reboot the recorded pid is dead and must not strand the home."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "gateway.pid").write_bytes(
        json.dumps({"pid": _a_dead_pid(), "start_ts": "2026-01-01T00:00:00+00:00"}).encode()
    )

    lock = GatewayPidLock(state_dir)
    lock.acquire()
    try:
        assert lock.pid == os.getpid()
        assert _recorded_pid(state_dir) == os.getpid()
    finally:
        lock.release()


def test_release_hands_the_state_dir_to_the_next_gateway(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"

    first = GatewayPidLock(state_dir)
    first.acquire()
    first.release()
    assert not (state_dir / "gateway.pid").exists()

    second = GatewayPidLock(state_dir)
    second.acquire()
    try:
        assert _recorded_pid(state_dir) == os.getpid()
    finally:
        second.release()


def test_separate_state_dirs_do_not_block_each_other(tmp_path: Path) -> None:
    """The lock scopes one gateway per state directory, not one per machine.

    Two AgentOS homes are two independent installs. Anything that decides
    which home to use — `AGENTOS_STATE_DIR`, the desktop shell's environment
    resolution — is therefore choosing which gateway it can see, and this lock
    will not warn about the second one.
    """
    with _gateway_holding(tmp_path / "one", tmp_path):
        lock = GatewayPidLock(tmp_path / "two")
        lock.acquire()
        try:
            assert _recorded_pid(tmp_path / "two") == os.getpid()
        finally:
            lock.release()
