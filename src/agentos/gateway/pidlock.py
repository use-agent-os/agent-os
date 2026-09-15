"""PID file + exclusive lock for the gateway process.

Prevents two gateway instances from sharing the same STATE_DIR.

Design:
- The pid file (``gateway.pid``) is always readable: written atomically then fsynced.
- The lock file (``gateway.pid.lock``) carries the OS exclusive byte-range lock so the
  pid file itself stays open for readers even while the lock is held.

Platform locking:
- Windows: msvcrt.locking(lock_fd, LK_NBLCK, 1) on gateway.pid.lock
- POSIX:   fcntl.flock(lock_fd, LOCK_EX | LOCK_NB) on gateway.pid.lock

Usage::

    lock = GatewayPidLock(state_dir)
    lock.acquire()          # raises SystemExit(1) if another live instance holds it
    # lock released automatically via the atexit hook registered in acquire()
"""

from __future__ import annotations

import atexit
import datetime
import json
import logging
import os
import sys
from pathlib import Path
from typing import IO, Any, cast

log = logging.getLogger(__name__)

_PID_FILENAME = "gateway.pid"
_LOCK_FILENAME = "gateway.pid.lock"


class GatewayPidLock:
    """Exclusive PID-file lock for one gateway instance per STATE_DIR."""

    def __init__(self, state_dir: str | Path) -> None:
        self._state_dir = Path(state_dir)
        self._pid_path = self._state_dir / _PID_FILENAME
        self._lock_path = self._state_dir / _LOCK_FILENAME
        self._lock_fh: IO[bytes] | None = None
        # Cached payload written by this instance (readable without reopening the file).
        self._written: dict | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self) -> None:
        """Acquire the PID file lock.

        Algorithm:
        1. Acquire exclusive OS lock on gateway.pid.lock (separate file so
           gateway.pid stays freely readable while the lock is held).
           - Lock fails → SystemExit(1), reporting the pid recorded by the
             holder. Nothing in the STATE_DIR is modified.
        2. Holding the lock, reconcile gateway.pid.
           - pid alive  → release the lock, SystemExit(1) with STATE_DIR + pid.
           - pid dead   → log warning (stale), remove pid file, continue.
        3. Write pid + start_ts (ISO 8601) to gateway.pid, fsync.
        4. Register atexit cleanup.

        The lock is taken before step 2 on purpose. A process that has not
        won exclusivity must not mutate shared state: reconciling first meant
        a losing starter deleted the running gateway's pid file and only then
        discovered it could not have the lock.
        """
        self._state_dir.mkdir(parents=True, exist_ok=True)

        # ── Step 1: exclusive OS lock on the lock file ────────────────
        # Taken first so a losing starter leaves the STATE_DIR exactly as it
        # found it. The pid file is still intact here, so the holder's pid is
        # available for the error message rather than reading back as
        # "unknown" because this process just deleted it.
        lock_fh = open(str(self._lock_path), "w+b")  # noqa: WPS515

        if not _try_lock(lock_fh):
            existing_pid = _read_pid_from_path(self._pid_path)
            lock_fh.close()
            pid_str = str(existing_pid) if existing_pid is not None else "unknown"
            log.error(
                "gateway.pidlock.already_running",
                extra={"pid": existing_pid, "state_dir": str(self._state_dir)},
            )
            print(
                f"ERROR: Another gateway is already running "
                f"(pid={pid_str}, state_dir={self._state_dir}). "
                f"Stop it first or remove {self._pid_path}.",
                file=sys.stderr,
            )
            sys.exit(1)

        # ── Step 2: holding the lock, reconcile the pid file ──────────
        if self._pid_path.exists():
            existing_pid = _read_pid_from_path(self._pid_path)
            if existing_pid is not None and _is_alive(existing_pid):
                log.error(
                    "gateway.pidlock.already_running",
                    extra={"pid": existing_pid, "state_dir": str(self._state_dir)},
                )
                print(
                    f"ERROR: Another gateway is already running "
                    f"(pid={existing_pid}, state_dir={self._state_dir}). "
                    f"Stop it first or remove {self._pid_path}.",
                    file=sys.stderr,
                )
                _unlock(lock_fh)
                lock_fh.close()
                sys.exit(1)
            elif existing_pid is not None:
                log.warning(
                    "gateway.pidlock.stale_overwritten",
                    extra={"stale_pid": existing_pid, "state_dir": str(self._state_dir)},
                )
            try:
                self._pid_path.unlink(missing_ok=True)
            except OSError:
                pass

        # ── Step 3: write pid + start_ts to the pid file ─────────────
        self._lock_fh = lock_fh
        self._write_pid()

        # ── Step 4: register cleanup ──────────────────────────────────
        self._register_cleanup()

    def release(self) -> None:
        """Release the lock and remove the PID file. Safe to call multiple times.

        The pid file is removed; ``gateway.pid.lock`` is left in place on
        purpose (see the comment below).
        """
        if self._lock_fh is None:
            return
        fh = self._lock_fh
        self._lock_fh = None
        try:
            _unlock(fh)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass
        try:
            self._pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        # The lock file is deliberately NOT unlinked. Both platform locks
        # (fcntl.flock and msvcrt.locking) are held on the open file, not on
        # the path, so removing the path destroys the rendezvous point: a
        # process that opened the old file before the unlink and one that
        # creates a fresh file after it end up holding two independent locks
        # and both conclude they own this STATE_DIR. A stale zero-byte anchor
        # costs nothing; removing it costs the guarantee this class provides.

    @property
    def pid(self) -> int | None:
        """The PID written to the pid file by this instance, or None before acquire()."""
        return self._written.get("pid") if self._written else None

    @property
    def start_ts(self) -> str | None:
        """The start_ts written to the pid file by this instance, or None before acquire()."""
        return self._written.get("start_ts") if self._written else None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_pid(self) -> None:
        self._written = {
            "pid": os.getpid(),
            "start_ts": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        payload = json.dumps(self._written).encode()
        # Write to the pid file (not the lock file) so readers can open it freely.
        with open(str(self._pid_path), "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())

    def _register_cleanup(self) -> None:
        """Release the lock on normal interpreter exit.

        Deliberately does *not* install SIGTERM/SIGINT handlers. ``acquire()``
        runs during gateway startup, well before ``uvicorn.Server.serve()``
        calls ``install_signal_handlers()`` and overwrites both slots, so any
        handler registered here never fired in the shipped ``run=True`` path.

        It would have been actively harmful if it had won the slot: the old
        handler reset the disposition to ``SIG_DFL`` and re-raised, killing
        the process outright with no lifespan shutdown, no DB close and no
        WebSocket drain. uvicorn's graceful shutdown runs this ``atexit`` hook
        on the way out, which is the behaviour that was actually in effect.

        A gateway embedded in a host that does not install its own handlers
        should register :meth:`release` as a shutdown hook rather than have
        this class race for the slot.
        """
        atexit.register(self.release)


# ---------------------------------------------------------------------------
# Module-level helpers (no self state needed)
# ---------------------------------------------------------------------------


def _try_lock(fh: IO[bytes]) -> bool:
    if os.name == "nt":
        import msvcrt

        msvcrt_mod = cast(Any, msvcrt)
        try:
            fh.seek(0)
            msvcrt_mod.locking(fh.fileno(), msvcrt_mod.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    else:
        import fcntl

        fcntl_mod = cast(Any, fcntl)
        try:
            fcntl_mod.flock(fh.fileno(), fcntl_mod.LOCK_EX | fcntl_mod.LOCK_NB)
            return True
        except OSError:
            return False


def _unlock(fh: IO[bytes]) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt_mod = cast(Any, msvcrt)
        try:
            fh.seek(0)
            msvcrt_mod.locking(fh.fileno(), msvcrt_mod.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl

        fcntl_mod = cast(Any, fcntl)
        try:
            fcntl_mod.flock(fh.fileno(), fcntl_mod.LOCK_UN)
        except OSError:
            pass


def _read_pid_from_path(path: Path) -> int | None:
    try:
        info = json.loads(path.read_bytes())
        return int(info["pid"])
    except Exception:  # noqa: BLE001
        return None


def _is_alive(pid: int) -> bool:
    if os.name == "nt":
        try:
            import ctypes

            ctypes_mod = cast(Any, ctypes)
            synchronize = 0x00100000
            handle = ctypes_mod.windll.kernel32.OpenProcess(synchronize, False, pid)
            if handle == 0:
                return False
            ctypes_mod.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:  # noqa: BLE001
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but not owned by us
