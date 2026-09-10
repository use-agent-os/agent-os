from __future__ import annotations

from pathlib import Path

import pytest

from agentos.scheduler.engine import SchedulerEngine
from agentos.scheduler.persistence import JobStore
from agentos.scheduler.types import (
    CronJob,
    JobExecution,
    ManualRunStatus,
    ScheduleKind,
    SessionTarget,
)


@pytest.mark.asyncio
async def test_run_job_now_executes_fresh_reserved_job_snapshot(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "scheduler.db"))
    await store.open()
    engine = SchedulerEngine(store)

    executed_jobs: list[CronJob] = []

    async def capture_handler(job: CronJob) -> JobExecution:
        executed_jobs.append(job)
        return JobExecution(
            id="run-1",
            job_id=job.id,
            status="ok",
            output="done",
        )

    engine.register_handler("test_handler", capture_handler)

    try:
        job = await engine.add_job(
            name="fresh-snapshot-test",
            handler_key="test_handler",
            payload={"version": 1},
            session_target=SessionTarget.ISOLATED,
            schedule_kind=ScheduleKind.CRON,
            schedule_value="*/5 * * * *",
        )

        # Hook reserve_manual_job so that between the initial get() and reservation,
        # the job is updated in the store with new payload and timeout.
        orig_reserve = store.reserve_manual_job

        async def updated_reserve(job_id, now, source="manual", owner="scheduler-manual"):
            j = await store.get(job_id)
            assert j is not None
            j.payload = {"version": 2}
            j.timeout_seconds = 120.0
            await store.save(j)
            return await orig_reserve(job_id, now, source=source, owner=owner)

        store.reserve_manual_job = updated_reserve  # type: ignore[method-assign]

        result = await engine.run_job_now(job.id)

        assert result.status == ManualRunStatus.ACCEPTED
        assert len(executed_jobs) == 1
        # Verify the executed job instance has the freshly reserved state
        assert executed_jobs[0].payload == {"version": 2}
        assert executed_jobs[0].timeout_seconds == 120.0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_run_job_now_finalizes_when_handler_missing_on_reserved_job(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path / "scheduler.db"))
    await store.open()
    engine = SchedulerEngine(store)

    async def dummy_handler(job: CronJob) -> JobExecution:
        return JobExecution(id="run-1", job_id=job.id, status="ok")

    engine.register_handler("valid_handler", dummy_handler)

    try:
        job = await engine.add_job(
            name="missing-handler-test",
            handler_key="valid_handler",
            schedule_kind=ScheduleKind.CRON,
            schedule_value="*/5 * * * *",
        )

        orig_reserve = store.reserve_manual_job

        async def updated_reserve(job_id, now, source="manual", owner="scheduler-manual"):
            j = await store.get(job_id)
            assert j is not None
            j.handler_key = "unregistered_handler"
            await store.save(j)
            return await orig_reserve(job_id, now, source=source, owner=owner)

        store.reserve_manual_job = updated_reserve  # type: ignore[method-assign]

        result = await engine.run_job_now(job.id)

        assert result.status == ManualRunStatus.NO_HANDLER
        assert result.error == "No handler registered for key 'unregistered_handler'"

        # Verify the reservation was cleanly finalized and the job is not stuck in reserved status
        persisted = await store.get(job.id)
        assert persisted is not None
        assert persisted.status.value != "reserved"
    finally:
        await store.close()
