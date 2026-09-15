"""Cancelling a cron handler must also cancel the runtime task it owns."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agentos.gateway.routing import RouteEnvelope, SourceKind
from agentos.gateway.task_runtime import TaskRuntime
from agentos.scheduler.delivery import DeliveryChain, DeliveryReport
from agentos.scheduler.handlers import make_agent_run_handler
from agentos.scheduler.jobs import execute_with_timeout
from agentos.scheduler.payloads import make_agent_turn_payload
from agentos.scheduler.types import CronJob, SessionTarget
from agentos.session.models import AgentTaskStatus
from agentos.session.storage import SessionStorage


@pytest.mark.parametrize("queued", [False, True], ids=["running", "queued"])
@pytest.mark.parametrize("outer_timeout", [False, True], ids=["cancel", "scheduler-timeout"])
async def test_handler_cancellation_stops_its_runtime_task(
    tmp_path, monkeypatch, queued, outer_timeout
):
    storage = SessionStorage(str(tmp_path / "sessions.db"))
    await storage.connect()
    release = asyncio.Event()
    started = asyncio.Event()
    enqueued = asyncio.Event()
    side_effects = []
    handles = []
    terminal_events = {}

    async def emit(_session_key, event, payload):
        if event in {"task.cancelled", "task.succeeded"}:
            terminal_events.setdefault(payload["task_id"], asyncio.Event()).set()

    async def turn(run):
        started.set()
        await release.wait()
        side_effects.append(run.message)

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=turn,
        event_emitter=emit,
        max_concurrency=1,
        running_heartbeat_interval_s=None,
    )
    session_key = "agent:main:cron-cancellation"
    delivery = AsyncMock(spec=DeliveryChain)
    handler = make_agent_run_handler(delivery, task_runtime_ref=lambda: runtime)
    job = CronJob(
        name="Cancellation regression",
        handler_key="agent_run",
        payload=make_agent_turn_payload("cron work"),
        session_target=SessionTarget.SESSION,
        session_key=session_key,
        timeout_seconds=1 if outer_timeout else 30,
    )
    pending = None
    try:
        blocker = None
        if queued:
            blocker = await runtime.enqueue(
                RouteEnvelope(
                    source_kind=SourceKind.WEB,
                    source_name="test",
                    agent_id="main",
                    session_key=session_key,
                ),
                "unrelated work",
            )
            await asyncio.wait_for(started.wait(), timeout=5)

        enqueue = runtime.enqueue

        async def capture_enqueue(*args, **kwargs):
            handle = await enqueue(*args, **kwargs)
            handles.append(handle)
            enqueued.set()
            return handle

        monkeypatch.setattr(runtime, "enqueue", capture_enqueue)
        pending = asyncio.create_task(
            execute_with_timeout(job, handler) if outer_timeout else handler(job)
        )
        await asyncio.wait_for(enqueued.wait(), timeout=5)
        if not queued:
            await asyncio.wait_for(started.wait(), timeout=5)
        if outer_timeout:
            execution = await asyncio.wait_for(pending, timeout=5)
            assert execution.success is False
            assert execution.error == "Timeout after 1s"
        else:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending

        # Permit any orphaned task to perform observable work. Cancellation
        # must have reached it before the scheduler releases the failed run.
        release.set()
        task_id = handles[0].task_id
        # Terminal persistence is asynchronous; wait for its public event
        # before reading the ledger, without a timing-dependent sleep.
        await asyncio.wait_for(
            terminal_events.setdefault(task_id, asyncio.Event()).wait(), timeout=5
        )
        record = await runtime.status(task_id)
        assert record.status == AgentTaskStatus.CANCELLED
        assert "cron work" not in side_effects
        delivery.deliver.assert_not_awaited()
        if blocker is not None:
            await asyncio.wait_for(
                terminal_events.setdefault(blocker.task_id, asyncio.Event()).wait(), timeout=5
            )
            other = await runtime.status(blocker.task_id)
            assert other.status == AgentTaskStatus.SUCCEEDED
            assert "unrelated work" in side_effects
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        release.set()
        await runtime.shutdown()
        await storage.close()


@pytest.mark.parametrize("cancel_support", ["available", "missing", "raises"])
async def test_cancellation_preserves_legacy_runtime_adapters(cancel_support):
    runtime = SimpleNamespace(
        enqueue=AsyncMock(return_value=SimpleNamespace(task_id="owned-task")),
        wait=AsyncMock(side_effect=asyncio.CancelledError),
    )
    if cancel_support != "missing":
        runtime.cancel = AsyncMock(
            side_effect=RuntimeError("cleanup failed") if cancel_support == "raises" else None
        )
    delivery = AsyncMock(spec=DeliveryChain)
    handler = make_agent_run_handler(delivery, task_runtime_ref=lambda: runtime)
    job = CronJob(payload=make_agent_turn_payload("cron work"))

    with pytest.raises(asyncio.CancelledError):
        await handler(job)

    delivery.deliver.assert_not_awaited()
    if cancel_support != "missing":
        runtime.cancel.assert_awaited_once_with(task_id="owned-task")


@pytest.mark.parametrize("timeout", [False, True], ids=["success", "inner-timeout"])
async def test_runtime_success_and_inner_timeout_keep_existing_contract(timeout):
    runtime = SimpleNamespace(
        enqueue=AsyncMock(return_value=SimpleNamespace(task_id="owned-task")),
        wait=AsyncMock(
            side_effect=TimeoutError if timeout else None,
            return_value=SimpleNamespace(status=AgentTaskStatus.SUCCEEDED),
        ),
        cancel=AsyncMock(),
    )
    delivery = AsyncMock(spec=DeliveryChain)
    delivery.deliver.return_value = DeliveryReport()
    handler = make_agent_run_handler(delivery, task_runtime_ref=lambda: runtime)
    job = CronJob(name="Example", payload=make_agent_turn_payload("cron work"), timeout_seconds=1)

    if timeout:
        with pytest.raises(RuntimeError, match="Cron job 'Example' timed out after 1s"):
            await handler(job)
        runtime.cancel.assert_awaited_once_with(task_id="owned-task")
    else:
        result = await handler(job)
        assert result.delivery_status == "skipped|ws:skipped|fwd:skipped"
        runtime.cancel.assert_not_awaited()
    runtime.wait.assert_awaited_once_with("owned-task", timeout=1)
    assert delivery.deliver.await_args.kwargs["success"] is not timeout
