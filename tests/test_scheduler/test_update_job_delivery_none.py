"""Regression tests: clearing or passing delivery=None during job updates.

Passing delivery=None when updating a job should safely reset delivery to a default
DeliveryConfig instance rather than raising AttributeError during serialization or
execution.
"""

from __future__ import annotations

import json

import pytest

from agentos.gateway.rpc import RpcContext
from agentos.gateway.rpc_cron import _handle_cron_update
from agentos.scheduler.jobs import execute_with_timeout
from agentos.scheduler.ops import SchedulerOps, _normalize_delivery_for_target
from agentos.scheduler.persistence import (
    JobStore,
    _effective_delivery_for_target,
    _serialize_delivery,
)
from agentos.scheduler.types import (
    CronJob,
    DeliveryConfig,
    DeliveryMode,
    ScheduleKind,
    SessionTarget,
)


@pytest.fixture
async def scheduler_ops(tmp_path) -> SchedulerOps:
    store = JobStore(str(tmp_path / "scheduler.db"))
    await store.open()
    ops = SchedulerOps(store)
    yield ops
    await store.close()


@pytest.mark.asyncio
async def test_update_job_delivery_none_resets_to_default_delivery(
    scheduler_ops: SchedulerOps,
) -> None:
    """Updating a job with delivery=None clears delivery config and saves cleanly."""
    job = await scheduler_ops.add(
        name="test-delivery-reset",
        schedule_kind=ScheduleKind.CRON,
        schedule_value="*/5 * * * *",
        session_target=SessionTarget.ISOLATED,
        delivery=DeliveryConfig(
            mode=DeliveryMode.CHANNEL,
            channel_name="slack",
            channel_id="C12345",
        ),
    )
    assert job.delivery.mode == DeliveryMode.CHANNEL
    assert job.delivery.channel_name == "slack"

    # Patch job with delivery=None to reset/clear delivery
    updated = await scheduler_ops.update(job.id, delivery=None)
    assert updated is not None
    assert updated.delivery is not None
    assert updated.delivery.mode == DeliveryMode.NONE
    assert updated.delivery.channel_name == ""

    # Verify persisted state can be re-read from the store
    loaded = await scheduler_ops.get(job.id)
    assert loaded is not None
    assert loaded.delivery is not None
    assert loaded.delivery.mode == DeliveryMode.NONE


def test_normalize_delivery_for_target_none_defaults_to_delivery_config() -> None:
    """_normalize_delivery_for_target safely returns DeliveryConfig for None."""
    res_isolated = _normalize_delivery_for_target(
        session_target=SessionTarget.ISOLATED,
        delivery=None,
        explicit_delivery=True,
    )
    assert isinstance(res_isolated, DeliveryConfig)
    assert res_isolated.mode == DeliveryMode.NONE

    res_main = _normalize_delivery_for_target(
        session_target=SessionTarget.MAIN,
        delivery=None,
        explicit_delivery=True,
    )
    assert isinstance(res_main, DeliveryConfig)
    assert res_main.mode == DeliveryMode.NONE


def test_persistence_serialize_delivery_none_safe() -> None:
    """_serialize_delivery safely handles delivery=None without AttributeError."""
    serialized = _serialize_delivery(None)
    data = json.loads(serialized)
    assert data["schema_version"] == 4
    assert data["mode"] == "none"


def test_persistence_effective_delivery_for_target_none_safe() -> None:
    """_effective_delivery_for_target safely handles delivery=None."""
    res = _effective_delivery_for_target(SessionTarget.MAIN, None)
    assert isinstance(res, DeliveryConfig)
    assert res.mode == DeliveryMode.NONE


@pytest.mark.asyncio
async def test_rpc_cron_update_handles_delivery_none() -> None:
    """cron.update RPC endpoint accepts delivery=None to clear delivery."""
    current = CronJob(
        id="job-rpc-reset",
        name="hook",
        cron_expr="*/5 * * * *",
        schedule_raw="*/5 * * * *",
        handler_key="agent_run",
        payload={"kind": "agent_turn", "task": "x", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
        delivery=DeliveryConfig(
            mode=DeliveryMode.CHANNEL,
            channel_name="slack",
            channel_id="C12345",
        ),
    )

    class _FakeScheduler:
        def __init__(self, job: CronJob) -> None:
            self.job = job
            self.last_patch: dict | None = None

        async def get_job(self, job_id: str) -> CronJob | None:
            return self.job if self.job.id == job_id else None

        async def update_job(self, job_id: str, **patch) -> CronJob:
            self.last_patch = patch
            for k, v in patch.items():
                setattr(self.job, k, v)
            return self.job

    sched = _FakeScheduler(current)
    ctx = RpcContext(conn_id="test-conn", cron_scheduler=sched)

    await _handle_cron_update(
        {"id": "job-rpc-reset", "delivery": None},
        ctx,
    )

    assert sched.last_patch is not None
    assert "delivery" in sched.last_patch
    assert isinstance(sched.last_patch["delivery"], DeliveryConfig)
    assert sched.last_patch["delivery"].mode == DeliveryMode.NONE


@pytest.mark.asyncio
async def test_execute_with_timeout_failure_handles_delivery_without_failure_destination() -> None:
    """Failed execution handles default delivery safely."""

    async def _failing_handler(job):
        raise RuntimeError("boom")

    job = CronJob(
        id="job-fail-test",
        name="job-fail-test",
        cron_expr="* * * * *",
        handler_key="agent_run",
        session_target=SessionTarget.ISOLATED,
        delivery=DeliveryConfig(),
    )

    execution = await execute_with_timeout(
        job=job,
        handler=_failing_handler,
    )
    assert not execution.success
    assert "boom" in (execution.error or "")
