"""``cron.runs``' ``limit`` param must never reach the store unclamped.

``persistence.list_executions`` passes ``limit`` straight into a raw SQL
``LIMIT ?``. SQLite reads a negative ``LIMIT`` as "no limit at all" (the same
footgun ``mcp_server/bridge.py``'s ``_clamp_limit`` already guards against for
session listings), so an unvalidated negative or absurdly large value would
return a job's entire run history in one response instead of the bounded
preview the run-history drawer expects.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentos.gateway.rpc import RpcContext
from agentos.gateway.rpc_cron import _MAX_CRON_RUNS_LIMIT, _handle_cron_runs
from agentos.scheduler.types import JobExecution


class _RecordingScheduler:
    """Records the ``limit`` it actually receives instead of executing a query."""

    def __init__(self) -> None:
        self.received_limit: int | None = None

    async def get_runs(self, job_id: str, limit: int = 20) -> list[JobExecution]:
        self.received_limit = limit
        return []


def _ctx(scheduler: Any) -> RpcContext:
    ctx = RpcContext(conn_id="test", session_manager=None)
    ctx.cron_scheduler = scheduler  # type: ignore[attr-defined]
    return ctx


@pytest.mark.asyncio
async def test_negative_limit_is_clamped_to_one_not_passed_through() -> None:
    scheduler = _RecordingScheduler()

    await _handle_cron_runs({"id": "job-1", "limit": -1}, _ctx(scheduler))

    assert scheduler.received_limit == 1


@pytest.mark.asyncio
async def test_oversized_limit_is_capped() -> None:
    scheduler = _RecordingScheduler()

    await _handle_cron_runs({"id": "job-1", "limit": 10_000_000}, _ctx(scheduler))

    assert scheduler.received_limit == _MAX_CRON_RUNS_LIMIT


@pytest.mark.asyncio
async def test_non_numeric_limit_falls_back_to_the_default() -> None:
    scheduler = _RecordingScheduler()

    await _handle_cron_runs({"id": "job-1", "limit": "not-a-number"}, _ctx(scheduler))

    assert scheduler.received_limit == 20


@pytest.mark.asyncio
async def test_ordinary_limit_passes_through_unchanged() -> None:
    scheduler = _RecordingScheduler()

    await _handle_cron_runs({"id": "job-1", "limit": 5}, _ctx(scheduler))

    assert scheduler.received_limit == 5


@pytest.mark.asyncio
async def test_omitted_limit_uses_the_default_of_twenty() -> None:
    scheduler = _RecordingScheduler()

    await _handle_cron_runs({"id": "job-1"}, _ctx(scheduler))

    assert scheduler.received_limit == 20
