"""Issue #2507: ``exec_command`` leaked the process tree on Windows, and on
cancellation everywhere.

Two gaps in ``src/agentos/tools/builtin/shell.py``:

1. **Timeout, Windows.** ``_signal_exec_process_tree`` called
   ``proc.terminate()`` / ``proc.kill()`` -- ``TerminateProcess`` on the one
   process asyncio tracks, the ``cmd.exe`` that ``create_subprocess_shell``
   launches through. Whatever ``cmd.exe`` spawned survived orphaned for the
   life of the gateway. POSIX had ``killpg`` on the session all along.
2. **Cancellation, every platform.** ``asyncio.CancelledError`` is a
   ``BaseException``; the ``except Exception`` around the exec block never
   saw it, so a turn deadline or session kill left the subprocess -- and its
   descendants -- running with no cleanup at all, not even on the one child.

The real-process tests below build a three-deep tree (``cmd``/``sh`` ->
parent -> grandchild) and rely on the grandchild to *do* something after a
delay: if cleanup reached it, the marker it would have written never appears.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from agentos.tools.builtin import shell

GRANDCHILD_DELAY = 0.7
SETTLE = 1.4


def _tree(tmp_path: Path) -> tuple[str, Path]:
    """A command that spawns a parent which spawns a grandchild.

    The grandchild sleeps, then writes *marker*. The parent keeps running so
    the whole tree is alive when cleanup fires. Returns the command and the
    marker path.
    """
    marker = tmp_path / "grandchild-survived"
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(
        "import pathlib, time\n"
        f"time.sleep({GRANDCHILD_DELAY})\n"
        f"pathlib.Path({str(marker)!r}).write_text('survived')\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(grandchild)!r}])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{parent}"', marker


# ── the issue, with real processes ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_timeout_kills_the_grandchild_not_just_the_shell(tmp_path: Path) -> None:
    """Gap 1. On Windows before the fix the grandchild outlived the timeout
    and wrote the marker; on POSIX ``killpg`` already covered this."""
    command, marker = _tree(tmp_path)

    result = await shell.exec_command(command, timeout=0.3)
    await asyncio.sleep(SETTLE)

    assert result.startswith("[timeout after 0.3s]")
    assert not marker.exists(), "the grandchild survived the timeout"


@pytest.mark.asyncio
async def test_cancellation_kills_the_whole_tree(tmp_path: Path) -> None:
    """Gap 2, on every platform: before the fix nothing was cleaned up."""
    command, marker = _tree(tmp_path)

    task = asyncio.ensure_future(shell.exec_command(command, timeout=30))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(SETTLE)

    assert not marker.exists(), "the grandchild survived the cancellation"


@pytest.mark.asyncio
async def test_cancellation_still_propagates_as_cancellation(tmp_path: Path) -> None:
    """Cleanup must not swallow the CancelledError -- the caller cancelled
    for a reason and expects the task to end cancelled, not to return."""
    command, _ = _tree(tmp_path)

    task = asyncio.ensure_future(shell.exec_command(command, timeout=30))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()


@pytest.mark.asyncio
async def test_cancellation_cleanup_finishes_promptly(tmp_path: Path) -> None:
    """The kill escalates on short timeouts; a cancelled call must not hang
    the caller for the remainder of the command's own timeout."""
    command, _ = _tree(tmp_path)
    loop = asyncio.get_running_loop()

    task = asyncio.ensure_future(shell.exec_command(command, timeout=30))
    await asyncio.sleep(0.2)
    started = loop.time()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert loop.time() - started < 5.0


@pytest.mark.asyncio
async def test_a_command_that_finishes_is_unaffected(tmp_path: Path) -> None:
    result = await shell.exec_command(f'"{sys.executable}" -c "print(42)"', timeout=10)

    assert result.startswith("exit_code=0\n")
    assert "42" in result


@pytest.mark.asyncio
async def test_the_grandchild_marker_technique_is_not_vacuous(tmp_path: Path) -> None:
    """Left alone, the tree does write the marker -- so its absence above
    means cleanup reached it, not that it never ran."""
    _, marker = _tree(tmp_path)
    grandchild = tmp_path / "grandchild.py"
    proc = await asyncio.create_subprocess_exec(sys.executable, str(grandchild))
    await proc.wait()

    assert marker.read_text(encoding="utf-8") == "survived"


# ── the Windows tree kill, on any runner ────────────────────────────────────


def test_the_tree_kill_command_walks_the_tree_and_forces() -> None:
    assert shell._windows_tree_kill_argv(4242) == ["taskkill", "/T", "/F", "/PID", "4242"]


class _FakeProc:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.calls: list[str] = []

    def kill(self) -> None:
        self.calls.append("kill")
        self.returncode = -9

    def terminate(self) -> None:
        self.calls.append("terminate")
        self.returncode = -15


class _FakeKiller:
    def __init__(self, *, hang: bool = False) -> None:
        self.hang = hang
        self.killed = False

    async def wait(self) -> int:
        if self.hang:
            await asyncio.sleep(60)
        return 0

    def kill(self) -> None:
        self.killed = True


@pytest.mark.asyncio
async def test_tree_kill_runs_taskkill_against_the_root_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[tuple[Any, ...]] = []

    async def fake_exec(*argv: Any, **kwargs: Any) -> _FakeKiller:
        spawned.append(argv)
        return _FakeKiller()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    assert await shell._kill_windows_process_tree(_FakeProc(pid=777)) is True
    assert spawned == [("taskkill", "/T", "/F", "/PID", "777")]


@pytest.mark.asyncio
async def test_tree_kill_reports_failure_when_taskkill_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(*argv: Any, **kwargs: Any) -> _FakeKiller:
        raise FileNotFoundError("taskkill")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    assert await shell._kill_windows_process_tree(_FakeProc()) is False


@pytest.mark.asyncio
async def test_tree_kill_gives_up_on_a_hanging_taskkill_and_kills_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killer = _FakeKiller(hang=True)

    async def fake_exec(*argv: Any, **kwargs: Any) -> _FakeKiller:
        return killer

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(shell, "_EXEC_KILL_TIMEOUT", 0.05)

    assert await shell._kill_windows_process_tree(_FakeProc()) is False
    assert killer.killed, "a hung taskkill is not left behind either"


@pytest.mark.asyncio
async def test_on_windows_termination_goes_through_the_tree_kill_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shell.os, "name", "nt")
    proc = _FakeProc()
    tree_kills: list[int] = []

    async def fake_tree_kill(target: Any) -> bool:
        tree_kills.append(target.pid)
        target.returncode = 1
        return True

    monkeypatch.setattr(shell, "_kill_windows_process_tree", fake_tree_kill)

    await shell._terminate_exec_process_tree(proc)

    assert tree_kills == [4242]
    assert proc.calls == [], "the one-process kill is the fallback, not the first move"


@pytest.mark.asyncio
async def test_on_windows_a_failed_tree_kill_falls_back_to_killing_the_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An incomplete kill is better than none."""
    monkeypatch.setattr(shell.os, "name", "nt")
    proc = _FakeProc()

    async def failing_tree_kill(target: Any) -> bool:
        return False

    monkeypatch.setattr(shell, "_kill_windows_process_tree", failing_tree_kill)

    await shell._terminate_exec_process_tree(proc)

    assert proc.calls == ["kill"]


@pytest.mark.asyncio
async def test_on_windows_an_already_exited_root_is_not_tree_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``taskkill`` on a dead PID only errors; do not spawn it for nothing."""
    monkeypatch.setattr(shell.os, "name", "nt")
    proc = _FakeProc()
    proc.returncode = 0
    called = False

    async def fake_tree_kill(target: Any) -> bool:
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(shell, "_kill_windows_process_tree", fake_tree_kill)

    await shell._terminate_exec_process_tree(proc)

    assert called is False


@pytest.mark.asyncio
async def test_on_posix_termination_still_escalates_on_the_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The POSIX path is unchanged: SIGTERM to the group, then SIGKILL."""
    monkeypatch.setattr(shell.os, "name", "posix")
    monkeypatch.setattr(shell, "_EXEC_TERMINATE_TIMEOUT", 0.02)
    monkeypatch.setattr(shell, "_EXEC_KILL_TIMEOUT", 0.02)
    signals: list[int] = []

    def fake_killpg(pid: int, sig: int) -> None:
        signals.append(int(sig))
        if len(signals) == 2:
            proc.returncode = -9

    proc = _FakeProc()
    monkeypatch.setattr(shell.os, "killpg", fake_killpg, raising=False)

    async def no_tree_kill(target: Any) -> bool:  # must not be used on POSIX
        raise AssertionError("taskkill is Windows-only")

    monkeypatch.setattr(shell, "_kill_windows_process_tree", no_tree_kill)

    await shell._terminate_exec_process_tree(proc)

    assert len(signals) == 2
    assert signals[0] == int(shell.signal.SIGTERM)


# ── the cancellation cleanup survives a second cancellation ─────────────────


@pytest.mark.asyncio
async def test_cleanup_runs_to_completion_when_cancelled_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kill is shielded: a second cancel interrupts the await, not the kill."""
    finished = asyncio.Event()

    async def slow_terminate(proc: Any) -> None:
        await asyncio.sleep(0.2)
        finished.set()

    monkeypatch.setattr(shell, "_terminate_exec_process_tree", slow_terminate)

    async def cancelled_twice() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await shell._cleanup_after_cancellation(_FakeProc())
            raise

    task = asyncio.ensure_future(cancelled_twice())
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()  # again, while the shielded cleanup is in flight
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.wait_for(finished.wait(), timeout=2.0)


@pytest.mark.asyncio
async def test_cleanup_completes_normally_when_not_cancelled_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[int] = []

    async def terminate(proc: Any) -> None:
        ran.append(proc.pid)

    monkeypatch.setattr(shell, "_terminate_exec_process_tree", terminate)

    await shell._cleanup_after_cancellation(_FakeProc(pid=5))

    assert ran == [5]


# ── the timeout report and the ordinary error path are unchanged ────────────


@pytest.mark.asyncio
async def test_the_timeout_message_is_unchanged(tmp_path: Path) -> None:
    command, _ = _tree(tmp_path)

    result = await shell.exec_command(command, timeout=0.2)

    assert result == f"[timeout after 0.2s]\ncommand: {command}"


@pytest.mark.asyncio
async def test_an_ordinary_exception_still_returns_an_error_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no shell today")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", boom)

    result = await shell.exec_command("echo hi", timeout=5)

    assert result == "[error] no shell today"


@pytest.mark.skipif(
    os.name == "nt", reason="the marker technique on POSIX shows main's killpg works"
)
@pytest.mark.asyncio
async def test_posix_timeout_path_covers_the_grandchild_as_before(tmp_path: Path) -> None:
    command, marker = _tree(tmp_path)

    await shell.exec_command(command, timeout=0.3)
    await asyncio.sleep(SETTLE)

    assert not marker.exists()
