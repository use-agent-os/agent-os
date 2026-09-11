"""``JobStore.list_executions``' ``limit`` param must never reach SQLite unclamped.

``list_executions`` passes ``limit`` straight into a raw SQL ``LIMIT ?``.
SQLite reads a negative ``LIMIT`` as "no limit at all" and ``LIMIT 0`` as zero
rows, so an unvalidated value either leaks a job's entire run history or
swallows it. Both current callers (``rpc_cron`` and the control tool) clamp at
their own edge; the shared store method must defend itself too, so the next
caller cannot forget. The ceiling mirrors ``rpc_cron``'s run-history cap.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentos.scheduler.persistence import JobStore
from agentos.scheduler.types import JobExecution

_CEILING = 1000  # mirrors rpc_cron's cron.runs limit cap


async def _seed_runs(store: JobStore, job_id: str, count: int) -> None:
    for _ in range(count):
        await store.save_execution(
            JobExecution(job_id=job_id, success=True, started_at=datetime.now(UTC))
        )


@pytest.mark.asyncio
async def test_negative_limit_clamps_to_one(tmp_path: Path) -> None:
    """SQLite reads a negative LIMIT as unbounded; the store must clamp to 1."""
    store = JobStore(str(tmp_path / "test.db"))
    await store.open()
    try:
        await _seed_runs(store, "job-1", 5)
        rows = await store.list_executions("job-1", limit=-1)
        assert len(rows) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_zero_limit_clamps_to_one(tmp_path: Path) -> None:
    """SQLite reads LIMIT 0 as zero rows; the store must clamp to 1."""
    store = JobStore(str(tmp_path / "test.db"))
    await store.open()
    try:
        await _seed_runs(store, "job-1", 5)
        rows = await store.list_executions("job-1", limit=0)
        assert len(rows) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_ceiling_caps_huge_requests() -> None:
    """A huge limit must cap at the ceiling, not return everything."""
    store = JobStore(":memory:")
    await store.open()
    try:
        await _seed_runs(store, "job-1", _CEILING + 1)
        rows = await store.list_executions("job-1", limit=_CEILING + 500)
        assert len(rows) == _CEILING
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_bounded_limits_keep_exact_semantics(tmp_path: Path) -> None:
    """In-range limits keep their exact semantics."""
    store = JobStore(str(tmp_path / "test.db"))
    await store.open()
    try:
        await _seed_runs(store, "job-1", 5)
        rows = await store.list_executions("job-1", limit=3)
        assert len(rows) == 3
        rows = await store.list_executions("job-1", limit=10)
        assert len(rows) == 5
    finally:
        await store.close()
