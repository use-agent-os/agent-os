"""Issue #1948: a cron timeout must reach the runtime turn the handler submitted.

There are two timeout layers, and they do not fire in the order the code
assumed. ``execute_with_timeout`` wraps the whole handler in
``asyncio.wait_for(task, job.timeout_seconds)``; inside the handler,
``task_runtime.wait(task_id, timeout=job.timeout_seconds)`` uses the *same*
budget but starts later, after session setup and any pre-run script. The outer
deadline therefore always wins: the handler is cancelled rather than timing
out, its ``except TimeoutError`` branch never runs, and the enqueued turn keeps
running -- or, if it was still queued, starts afterwards.

No provider or network calls here: the runtime is a fake whose turn is gated on
an event the test controls.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agentos.scheduler.delivery import DeliveryChain
from agentos.scheduler.handlers import make_agent_run_handler
from agentos.scheduler.jobs import execute_with_timeout
from agentos.scheduler.payloads import make_agent_turn_payload
from agentos.scheduler.types import CronJob, DeliveryConfig, SessionTarget


class _FakeSessionManager:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict]] = {}

    async def get_or_create(self, **kwargs):
        return kwargs

    async def append_message(self, session_key, role, content):
        self.rows.setdefault(session_key, []).append({"role": role, "content": content})
        return SimpleNamespace(role=role, content=content)

    async def read_transcript(self, session_key):
        return list(self.rows.get(session_key, []))


class _GatedTaskRuntime:
    """A runtime whose turn does not finish until the test releases it.

    ``performed`` records the work an orphaned turn would do after the cron run
    has already been reported as timed out.
    """

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.cancelled: list[str] = []
        self.enqueued: list[str] = []
        self.performed: list[str] = []
        self._next_id = 0

    async def enqueue(self, envelope, task, *, mode=None, run_kind=None):
        self._next_id += 1
        task_id = f"task-{self._next_id}"
        self.enqueued.append(task_id)
        return SimpleNamespace(task_id=task_id)

    async def wait(self, task_id: str, timeout: float | None = None):
        from agentos.session.models import AgentTaskStatus

        await self.gate.wait()
        # Only a turn that was never cancelled gets to act.
        if task_id not in self.cancelled:
            self.performed.append(task_id)
            return SimpleNamespace(status=AgentTaskStatus.SUCCEEDED)
        return SimpleNamespace(status=AgentTaskStatus.CANCELLED)

    async def cancel(self, task_id: str):
        self.cancelled.append(task_id)


def _job(timeout_seconds: float = 0.05) -> CronJob:
    return CronJob(
        id="cron-1948",
        name="Watcher",
        handler_key="agent_run",
        payload=make_agent_turn_payload("Report anything odd.", "main"),
        session_target=SessionTarget.ISOLATED,
        timeout_seconds=timeout_seconds,
        delivery=DeliveryConfig(best_effort=True),
    )


def _handler(runtime: _GatedTaskRuntime, sm: _FakeSessionManager):
    return make_agent_run_handler(
        DeliveryChain(),
        session_manager_ref=lambda: sm,
        task_runtime_ref=lambda: runtime,
    )


@pytest.mark.asyncio
async def test_the_outer_timeout_cancels_the_submitted_runtime_task() -> None:
    """The whole bug in one run: the deadline passes, and the turn it was
    supposed to stop carries on and succeeds."""
    runtime = _GatedTaskRuntime()
    job = _job()

    execution = await execute_with_timeout(job, _handler(runtime, _FakeSessionManager()))

    assert execution.success is False
    assert runtime.enqueued == ["task-1"]
    assert runtime.cancelled == ["task-1"], "the submitted turn was left running"

    # Release the gate the way the real runtime would once its turn finishes.
    # A cancelled turn must not act after its cron run already failed.
    runtime.gate.set()
    await asyncio.sleep(0)
    assert runtime.performed == []


@pytest.mark.asyncio
async def test_a_queued_turn_is_cancelled_before_it_can_start() -> None:
    """A turn queued behind another in the same session never begins, so there
    is no 'running' task to stop -- it has to be cancelled by id anyway or it
    starts after its own deadline has passed."""
    runtime = _GatedTaskRuntime()

    execution = await execute_with_timeout(_job(), _handler(runtime, _FakeSessionManager()))

    assert execution.success is False
    assert runtime.cancelled == ["task-1"]

    runtime.gate.set()
    await asyncio.sleep(0)
    assert runtime.performed == []


@pytest.mark.asyncio
async def test_explicit_cancellation_of_the_handler_also_reaches_the_turn() -> None:
    """Not only the timeout path: anything that cancels the handler must take
    the submitted turn with it."""
    runtime = _GatedTaskRuntime()
    handler = _handler(runtime, _FakeSessionManager())

    task = asyncio.create_task(handler(_job(timeout_seconds=30.0)))
    while not runtime.enqueued:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runtime.cancelled == ["task-1"]


@pytest.mark.asyncio
async def test_cancellation_only_touches_the_task_this_handler_submitted() -> None:
    """Unrelated work in the same session must be left alone."""
    runtime = _GatedTaskRuntime()
    runtime.enqueued.append("someone-elses-task")  # another turn in this session

    await execute_with_timeout(_job(), _handler(runtime, _FakeSessionManager()))

    assert runtime.cancelled == ["task-1"], "exactly one task, the submitted one"
    assert "someone-elses-task" not in runtime.cancelled


@pytest.mark.asyncio
async def test_a_runtime_without_cancel_does_not_break_the_timeout() -> None:
    """`_cancel_runtime_task` probes for `cancel` with getattr, so an older or
    partial runtime adapter has none. Cancelling must still propagate and the
    run must still be recorded as a timeout, rather than dying on AttributeError
    inside the cleanup and masking the real outcome."""

    class _NoCancelRuntime(_GatedTaskRuntime):
        cancel = None  # type: ignore[assignment]

    runtime = _NoCancelRuntime()

    execution = await execute_with_timeout(_job(), _handler(runtime, _FakeSessionManager()))

    assert execution.success is False
    assert runtime.cancelled == []


@pytest.mark.asyncio
async def test_a_failing_cancel_does_not_mask_the_timeout() -> None:
    """The adapter swallows cancel errors on purpose. If it did not, a runtime
    that raises on cancel would replace the timeout with its own exception."""

    class _AngryRuntime(_GatedTaskRuntime):
        async def cancel(self, task_id: str):
            raise RuntimeError("runtime is unreachable")

    runtime = _AngryRuntime()

    execution = await execute_with_timeout(_job(), _handler(runtime, _FakeSessionManager()))

    assert execution.success is False
    assert execution.error and "timeout" in execution.error.lower()


@pytest.mark.asyncio
async def test_the_timeout_is_still_reported_to_the_scheduler() -> None:
    """Cancelling the turn must not swallow the cancellation: the scheduler
    still has to record the run as a timeout."""
    runtime = _GatedTaskRuntime()
    job = _job()

    execution = await execute_with_timeout(job, _handler(runtime, _FakeSessionManager()))

    assert execution.success is False
    assert execution.error and "timeout" in execution.error.lower()


@pytest.mark.asyncio
async def test_a_turn_that_finishes_in_time_is_never_cancelled() -> None:
    """The guard must not disturb the ordinary path."""
    runtime = _GatedTaskRuntime()
    runtime.gate.set()

    execution = await execute_with_timeout(
        _job(timeout_seconds=30.0), _handler(runtime, _FakeSessionManager())
    )

    assert runtime.cancelled == []
    assert runtime.performed == ["task-1"]
    assert execution.success is True
