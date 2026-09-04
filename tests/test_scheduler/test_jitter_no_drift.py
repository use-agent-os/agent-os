"""Regression: jitter must not drift into post-execution rescheduled next_run_at.

`_next_run` previously unconditionally added `job.jitter_seconds` to the
computed candidate, so every successful execution baked one extra jitter window
into `next_run_at`.  After N fires the job would run N x jitter_seconds later
than the cron expression specifies.

The fix: `apply_jitter` defaults to `False` in `_next_run`; only
`ops.add` passes `apply_jitter=True` for the very first scheduling.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentos.scheduler.jobs import _apply_result_state, _next_run
from agentos.scheduler.ops import SchedulerOps
from agentos.scheduler.persistence import JobStore
from agentos.scheduler.types import (
    CronJob,
    JobExecution,
    JobStatus,
    ScheduleKind,
    SessionTarget,
)


def _cron_job_with_jitter(jitter: float) -> CronJob:
    """Return a CRON job whose expression fires every minute."""
    return CronJob(
        id="jitter-test",
        name="jitter-test",
        cron_expr="* * * * *",
        handler_key="agent_run",
        payload={"kind": "agent_turn", "task": "noop", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
        schedule_kind=ScheduleKind.CRON,
        jitter_seconds=jitter,
        status=JobStatus.PENDING,
    )


def test_next_run_default_no_jitter() -> None:
    """_next_run with default apply_jitter=False must return the bare cron minute."""
    job = _cron_job_with_jitter(jitter=20.0)
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    result = _next_run(job, base)
    expected = datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)
    assert result == expected, (
        f"_next_run must not add jitter by default; got {result}, expected {expected}"
    )


def test_next_run_apply_jitter_true_adds_offset() -> None:
    """_next_run with apply_jitter=True must add jitter_seconds to the result."""
    jitter = 15.0
    job = _cron_job_with_jitter(jitter=jitter)
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    result = _next_run(job, base, apply_jitter=True)
    expected = datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC) + timedelta(seconds=jitter)
    assert result == expected


def test_reschedule_after_success_has_no_jitter_drift() -> None:
    """Repeated successful executions must not drift next_run_at forward."""
    job = _cron_job_with_jitter(jitter=20.0)
    now = datetime(2026, 1, 1, 0, 5, 30, tzinfo=UTC)

    for i in range(3):
        execution = JobExecution(job_id=job.id, success=True)
        _apply_result_state(job, execution, now)

        assert job.next_run_at is not None
        expected_minute = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        assert job.next_run_at == expected_minute, (
            f"Iteration {i}: expected {expected_minute}, got {job.next_run_at}. "
            "Jitter is accumulating into post-execution next_run_at."
        )
        now = job.next_run_at + timedelta(seconds=5)


@pytest.mark.asyncio
async def test_ops_add_initial_next_run_includes_jitter(tmp_path: Path) -> None:
    """ops.add must apply jitter_seconds in the first next_run_at."""
    db = tmp_path / "cron.db"
    async with JobStore(str(db)) as store:
        from agentos.scheduler.payloads import make_agent_turn_payload

        ops = SchedulerOps(store)
        jitter = 25.0
        job = await ops.add(
            name="jitter-initial",
            schedule_kind=ScheduleKind.CRON,
            schedule_value="* * * * *",
            handler_key="agent_run",
            payload=make_agent_turn_payload("noop"),
            session_target=SessionTarget.ISOLATED,
            jitter_seconds=jitter,
        )

        assert job.next_run_at is not None
        now = datetime.now(UTC)
        bare_next = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        expected_with_jitter = bare_next + timedelta(seconds=jitter)
        delta = abs((job.next_run_at - expected_with_jitter).total_seconds())
        assert delta < 5, (
            f"ops.add must bake jitter into initial next_run_at; "
            f"expected ~{expected_with_jitter}, got {job.next_run_at}"
        )
