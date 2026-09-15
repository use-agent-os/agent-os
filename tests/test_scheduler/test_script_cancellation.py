"""Scheduler cancellation must reap a script before reporting completion."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agentos.scheduler.delivery import DeliveryChain
from agentos.scheduler.handlers import make_agent_run_handler, make_script_run_handler
from agentos.scheduler.jobs import execute_with_timeout
from agentos.scheduler.payloads import make_agent_turn_payload, make_script_payload
from agentos.scheduler.scripts import run_job_script
from agentos.scheduler.types import CronJob


@pytest.mark.parametrize("kind", ["script", "prerun", "direct-cancel", "direct-timeout"])
async def test_cancelled_script_cannot_write_after_scheduler_returns(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "gated.py").write_text(
        "from pathlib import Path\n"
        "import time\n"
        "Path('ready').touch()\n"
        "while not Path('release').exists():\n"
        "    time.sleep(0.01)\n"
        "Path('late-write').write_text('unexpected')\n",
        encoding="utf-8",
    )
    processes = []
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

    async def wait_for_file(path: Path):
        while not path.exists():
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_for_file(scripts / "ready"), timeout=5)
        if kind == "direct-cancel":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        elif kind == "direct-timeout":
            success, output = await asyncio.wait_for(pending, timeout=5)
            assert success is False
            assert output == "Script timed out after 1s: gated.py"
        else:
            execution = await asyncio.wait_for(pending, timeout=5)
            assert execution.success is False
            assert execution.error == "Timeout after 1s"
        assert len(processes) == 1
        proc = processes[0]
        reaped_before_return = proc.returncode is not None
        (scripts / "release").touch()
        await asyncio.wait_for(proc.wait(), timeout=5)
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
    tmp_path, monkeypatch, cancelled
):
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "exited.py").write_text("pass", encoding="utf-8")
    proc = SimpleNamespace(
        kill=Mock(side_effect=ProcessLookupError),
        communicate=AsyncMock(
            side_effect=[asyncio.CancelledError if cancelled else TimeoutError, (b"", b"")]
        ),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))

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
