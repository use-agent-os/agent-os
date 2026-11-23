"""Scheduler cancellation must reap a cron script before reporting completion.

Regression for #1949: ``execute_with_timeout`` cancels the handler on the
job's outer timeout, and ``run_job_script`` only killed its child on its own
inner ``TimeoutError`` — a cancelled script kept running and could write after
the run had already been recorded as failed.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agentos.scheduler import scripts as scripts_module
from agentos.scheduler.delivery import DeliveryChain
from agentos.scheduler.handlers import make_agent_run_handler, make_script_run_handler
from agentos.scheduler.jobs import execute_with_timeout
from agentos.scheduler.payloads import make_agent_turn_payload, make_script_payload
from agentos.scheduler.scripts import run_job_script
from agentos.scheduler.types import CronJob

_GATED_SCRIPT = (
    "from pathlib import Path\n"
    "import time\n"
    "Path('ready').touch()\n"
    "while not Path('release').exists():\n"
    "    time.sleep(0.01)\n"
    "Path('late-write').write_text('unexpected')\n"
)


async def _wait_for_file(path: Path) -> None:
    while not path.exists():
        await asyncio.sleep(0.01)


@pytest.mark.parametrize("kind", ["script", "prerun", "direct-cancel", "direct-timeout"])
async def test_cancelled_script_cannot_write_after_scheduler_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "gated.py").write_text(_GATED_SCRIPT, encoding="utf-8")

    processes: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def capture_spawn(*args, **kwargs):
        proc = await spawn(*args, **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_spawn)
    delivery = AsyncMock(spec=DeliveryChain)
    turn_runner = AsyncMock()

    if kind.startswith("direct-"):
        pending = asyncio.create_task(
            run_job_script("gated.py", timeout=1 if kind == "direct-timeout" else 30)
        )
    else:
        if kind == "script":
            payload = make_script_payload("gated.py")
            handler = make_script_run_handler(delivery)
        else:
            payload = make_agent_turn_payload("report", script="gated.py")
            handler = make_agent_run_handler(delivery, turn_runner_ref=lambda: turn_runner)
        job = CronJob(name="Script cancellation", payload=payload, timeout_seconds=1)
        pending = asyncio.create_task(execute_with_timeout(job, handler))

    try:
        await asyncio.wait_for(_wait_for_file(scripts / "ready"), timeout=10)
        if kind == "direct-cancel":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        elif kind == "direct-timeout":
            success, output = await asyncio.wait_for(pending, timeout=10)
            assert success is False
            assert output == "Script timed out after 1s: gated.py"
        else:
            execution = await asyncio.wait_for(pending, timeout=10)
            assert execution.success is False
            assert execution.error == "Timeout after 1s"

        assert len(processes) == 1
        proc = processes[0]
        reaped_before_return = proc.returncode is not None
        (scripts / "release").touch()
        await asyncio.wait_for(proc.wait(), timeout=10)
        assert not (scripts / "late-write").exists()
        assert reaped_before_return
        delivery.deliver.assert_not_awaited()
        turn_runner.run.assert_not_called()
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        for proc in processes:
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()


@pytest.mark.parametrize("cancelled", [False, True], ids=["timeout", "cancellation"])
async def test_script_exit_racing_with_cleanup_preserves_original_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    """A child that exits between the kill and the reap must not mask the outcome."""
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "exited.py").write_text("pass", encoding="utf-8")
    proc = SimpleNamespace(
        returncode=0,
        kill=Mock(side_effect=ProcessLookupError),
        communicate=AsyncMock(
            side_effect=[asyncio.CancelledError if cancelled else TimeoutError, (b"", b"")]
        ),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    # The process-group sweep that follows the reap wants a real pid; this
    # double has none, and what is under test here is the outcome the caller
    # reports, not the sweep (covered by the live-subprocess tests above).
    monkeypatch.setattr(scripts_module, "_kill_process_group", AsyncMock())

    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await run_job_script("exited.py", timeout=30)
    else:
        assert await run_job_script("exited.py", timeout=30) == (
            False,
            "Script timed out after 30s: exited.py",
        )
    proc.kill.assert_called_once_with()
    assert proc.communicate.await_count == 2


class _SlowToDie:
    """A live child whose kill takes effect only when the test lands it.

    ``TerminateProcess`` only starts a termination and returns, and a loaded
    Windows runner took longer than the reap grace to finish one. This keeps
    the child running after ``kill()`` until :meth:`land_kill`, and stands in
    for the run's own deadline with :meth:`expire`, so neither depends on how
    fast the runner is.
    """

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.pid = proc.pid
        self.kill_requested = False
        self.reap_gave_up = asyncio.Event()
        self._deadline = asyncio.Event()

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode

    def expire(self) -> None:
        self._deadline.set()

    def kill(self) -> None:
        self.kill_requested = True

    def land_kill(self) -> None:
        self.proc.kill()

    async def communicate(self) -> tuple[bytes, bytes]:
        if not self.kill_requested:
            # The run's own wait: only the test's deadline or a cancel ends it.
            await self._deadline.wait()
            raise TimeoutError
        try:
            return await self.proc.communicate()
        except asyncio.CancelledError:
            self.reap_gave_up.set()
            raise


@pytest.mark.parametrize("ending", ["timeout", "cancellation"])
async def test_run_does_not_end_until_a_slow_kill_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    """The reap grace bounds the wait for the child's pipes, which an orphaned
    grandchild can hold open; it must not bound the wait for the child itself.
    Until the child has exited it can still write, so the run may not end."""
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "gated.py").write_text(_GATED_SCRIPT, encoding="utf-8")
    # Only how soon the drain gives up; nothing below waits on this clock.
    monkeypatch.setattr(scripts_module, "_REAP_GRACE_S", 0.01)
    # Windows has no process-group sweep. On POSIX its SIGTERM would land the
    # kill itself and hide the gap this test is about.
    monkeypatch.setattr(scripts_module, "_kill_process_group", AsyncMock())

    children: list[_SlowToDie] = []
    spawn = asyncio.create_subprocess_exec

    async def spawn_slow_to_die(*args, **kwargs):
        children.append(_SlowToDie(await spawn(*args, **kwargs)))
        return children[-1]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_slow_to_die)
    pending = asyncio.create_task(run_job_script("gated.py", timeout=30))
    try:
        await asyncio.wait_for(_wait_for_file(scripts / "ready"), timeout=10)
        (child,) = children
        if ending == "timeout":
            child.expire()
        else:
            pending.cancel()
        await asyncio.wait_for(child.reap_gave_up.wait(), timeout=10)

        assert child.kill_requested
        assert not pending.done(), "the run ended while the script it killed was still running"

        child.land_kill()
        if ending == "timeout":
            outcome = await asyncio.wait_for(pending, timeout=10)
            assert outcome == (False, "Script timed out after 30s: gated.py")
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, timeout=10)
        assert child.returncode is not None
        (scripts / "release").touch()
        assert not (scripts / "late-write").exists()
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        for child in children:
            if child.returncode is None:
                child.proc.kill()
            await child.proc.communicate()


_WRAPPER_SCRIPT = (
    "import subprocess, sys, time\n"
    "from pathlib import Path\n"
    "# A foreground grandchild that inherits stdout/stderr and outlives us.\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
    "Path('grandchild.pid').write_text(str(child.pid))\n"
    "Path('ready').touch()\n"
    "child.wait()\n"
)


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
@pytest.mark.parametrize("kind", ["direct-timeout", "direct-cancel"])
async def test_reap_is_bounded_when_a_grandchild_holds_the_pipes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """``communicate()`` only resolves once every pipe hits EOF, and a
    grandchild that inherited them keeps them open after the direct child is
    killed. The reap must give up after a short grace so the job deadline and
    scheduler shutdown are not held hostage by an orphan."""
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "wrap.py").write_text(_WRAPPER_SCRIPT, encoding="utf-8")

    pending = asyncio.create_task(
        run_job_script("wrap.py", timeout=1 if kind == "direct-timeout" else 30)
    )
    try:
        await asyncio.wait_for(_wait_for_file(scripts / "ready"), timeout=10)
        started = time.monotonic()
        if kind == "direct-cancel":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            success, output = await asyncio.wait_for(pending, timeout=10)
            assert success is False
            assert output == "Script timed out after 1s: wrap.py"
        assert time.monotonic() - started < 5, "reap waited for the orphaned grandchild"
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        pid_file = scripts / "grandchild.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
