"""EVERY+interval anchor preservation.

``CronJob.anchor_at`` records the original interval anchor so fire times stay
stable across restarts and slow ticks instead of drifting with each run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentos.scheduler.jobs import _next_run
from agentos.scheduler.ops import SchedulerOps
from agentos.scheduler.payloads import make_agent_turn_payload
from agentos.scheduler.persistence import JobStore
from agentos.scheduler.types import CronJob, ScheduleKind, SessionTarget


def _every_job(interval_seconds: int, anchor_at: datetime | None) -> CronJob:
    return CronJob(
        id="job-1",
        cron_expr=str(interval_seconds),
        handler_key="agent_run",
        payload={"kind": "agent_turn", "task": "x", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
        schedule_kind=ScheduleKind.EVERY,
        anchor_at=anchor_at,
    )


# --- pure _next_run anchor math -----------------------------------------


def test_anchored_next_run_aligns_to_interval_grid() -> None:
    """anchor=T0, interval=60s. After t=T0+125s, next_run = T0+180s."""
    t0 = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
    after = t0 + timedelta(seconds=125)
    job = _every_job(60, t0)
    assert _next_run(job, after) == t0 + timedelta(seconds=180)


def test_anchored_next_run_exactly_on_grid_yields_next_slot() -> None:
    """If after lands exactly on a grid point, return the next slot (strictly >)."""
    t0 = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
    after = t0 + timedelta(seconds=60)
    job = _every_job(60, t0)
    assert _next_run(job, after) == t0 + timedelta(seconds=120)


def test_anchored_next_run_before_anchor_returns_anchor() -> None:
    """When the scan starts before the anchor, the anchor itself is the first slot."""
    t0 = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
    after = t0 - timedelta(seconds=10)
    job = _every_job(60, t0)
    assert _next_run(job, after) == t0


def test_unanchored_every_keeps_legacy_now_plus_interval() -> None:
    """Without anchor_at, behaviour matches the historical now+interval drift."""
    after = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
    job = _every_job(60, anchor_at=None)
    assert _next_run(job, after) == after + timedelta(seconds=60)


# --- SchedulerOps integration -------------------------------------------


async def test_ops_add_every_records_anchor(tmp_path: Path) -> None:
    db = tmp_path / "cron.db"
    store = JobStore(str(db))
    await store.open()
    try:
        ops = SchedulerOps(store)
        job = await ops.add(
            name="ping",
            schedule_kind=ScheduleKind.CRON,
            schedule_value="*/5 * * * *",
            handler_key="agent_run",
            payload=make_agent_turn_payload("ping"),
            session_target=SessionTarget.ISOLATED,
        )
        # 5-minute cron path: anchor is unused for CRON, only EVERY+seconds anchors.
        assert job.schedule_kind == ScheduleKind.CRON
    finally:
        await store.close()


async def test_ops_add_every_seconds_records_anchor(tmp_path: Path) -> None:
    db = tmp_path / "cron.db"
    store = JobStore(str(db))
    await store.open()
    try:
        ops = SchedulerOps(store)
        # 7m as raw seconds: 60 % 7 != 0 so this exercises the EVERY+seconds path.
        job = await ops.add(
            name="ping",
            schedule_kind=ScheduleKind.EVERY,
            schedule_value=str(7 * 60),
            handler_key="agent_run",
            payload=make_agent_turn_payload("ping"),
            session_target=SessionTarget.ISOLATED,
        )
        assert job.schedule_kind == ScheduleKind.EVERY
        assert job.cron_expr == str(7 * 60)
        assert job.anchor_at is not None

        reloaded = await store.get(job.id)
        assert reloaded is not None
        assert reloaded.anchor_at is not None
        assert reloaded.anchor_at.tzinfo is not None  # UTC after round-trip
    finally:
        await store.close()


async def test_ops_update_schedule_resets_anchor(tmp_path: Path) -> None:
    db = tmp_path / "cron.db"
    store = JobStore(str(db))
    await store.open()
    try:
        ops = SchedulerOps(store)
        job = await ops.add(
            name="ping",
            schedule_kind=ScheduleKind.EVERY,
            schedule_value=str(7 * 60),
            handler_key="agent_run",
            payload=make_agent_turn_payload("ping"),
            session_target=SessionTarget.ISOLATED,
        )
        original_anchor = job.anchor_at
        assert original_anchor is not None

        # Patch to a different interval — anchor must reset, not be carried over.
        await store.save(job)  # ensure persisted
        updated = await ops.update(
            job.id,
            schedule_kind=ScheduleKind.EVERY,
            schedule_value=str(11 * 60),
        )
        assert updated is not None
        assert updated.anchor_at is not None
        assert updated.anchor_at >= original_anchor

        # Patch back to a cron expression — anchor must clear.
        updated2 = await ops.update(
            job.id,
            schedule_kind=ScheduleKind.CRON,
            schedule_value="0 9 * * *",
        )
        assert updated2 is not None
        assert updated2.schedule_kind == ScheduleKind.CRON
        assert updated2.anchor_at is None
    finally:
        await store.close()
