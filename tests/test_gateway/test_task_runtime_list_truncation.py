"""Regression tests for #1805: TaskRuntime.list() must surface the newest tasks.

``agent_tasks`` rows are only deleted when a session is deleted, so a
long-lived session outgrows ``list_agent_tasks``' 100-row window. With the
window taken from the *oldest* end, ``TaskRuntime.list()`` stops reporting a
session's current task, and the two callers the issue names act on a stale
row: ``sessions_yield`` waits on ``rows[-1]`` and session reset/delete scans
the same rows for something still running.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agentos.gateway.task_runtime import TaskRuntime
from agentos.session.models import AgentTaskRecord, AgentTaskStatus
from agentos.session.storage import SessionStorage

KEY = "agent:main:webchat:long-lived"
TOTAL = 105
RUNNING_TASK = f"task-{TOTAL - 1:03d}"


async def _storage_with_tasks(tmp_path, *, name: str = "tasks.db") -> SessionStorage:
    """105 tasks for one session: all succeeded except the newest, still running."""

    storage = SessionStorage(str(tmp_path / name))
    await storage.connect()
    for index in range(TOTAL):
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id=f"task-{index:03d}",
                session_key=KEY,
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=(
                    AgentTaskStatus.RUNNING if index == TOTAL - 1 else AgentTaskStatus.SUCCEEDED
                ),
                created_at=1000 + index,
                updated_at=1000 + index,
            )
        )
    return storage


def _runtime(storage: SessionStorage) -> TaskRuntime:
    async def _handler(*_args: Any, **_kwargs: Any) -> None:  # pragma: no cover - never run
        return None

    return TaskRuntime(storage=storage, turn_handler=_handler)


# --------------------------------------------------------------------------
# storage layer
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_newest_first_window_keeps_the_running_task(tmp_path) -> None:
    storage = await _storage_with_tasks(tmp_path)
    try:
        rows = await storage.list_agent_tasks(session_key=KEY, newest_first=True)

        assert len(rows) == 100
        assert rows[-1].task_id == RUNNING_TASK
        assert rows[-1].status == AgentTaskStatus.RUNNING
        # Still oldest-first within the window, so `rows[-1]` is the latest.
        assert [row.task_id for row in rows] == sorted(row.task_id for row in rows)
        assert rows[0].task_id == f"task-{TOTAL - 100:03d}"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_default_window_is_unchanged_for_other_callers(tmp_path) -> None:
    """The reviewer's constraint: other callers keep the oldest-first default."""

    storage = await _storage_with_tasks(tmp_path)
    try:
        rows = await storage.list_agent_tasks(session_key=KEY)

        assert len(rows) == 100
        assert rows[0].task_id == "task-000"
        assert rows[-1].task_id == "task-099"
        assert all(row.status == AgentTaskStatus.SUCCEEDED for row in rows)
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_newest_first_breaks_ties_by_insertion_order(tmp_path) -> None:
    """Tasks can share a `created_at`; `task_id` is a random uuid.

    The newest-first window must tie-break on insertion order, or `rows[-1]`
    becomes a coin flip among rows written in the same millisecond.
    """

    storage = SessionStorage(str(tmp_path / "ties.db"))
    await storage.connect()
    try:
        # Deliberately descending ids so any id-based ordering reverses them.
        for task_id in ("task-zzz", "task-mmm", "task-aaa"):
            await storage.create_agent_task(
                AgentTaskRecord(
                    task_id=task_id,
                    session_key=KEY,
                    source_kind="webui",
                    queue_mode="followup",
                    run_kind="web_turn",
                    status=AgentTaskStatus.SUCCEEDED,
                    created_at=5000,
                    updated_at=5000,
                )
            )

        rows = await storage.list_agent_tasks(session_key=KEY, newest_first=True, limit=2)

        assert [row.task_id for row in rows] == ["task-mmm", "task-aaa"]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_newest_first_still_honours_the_status_filter(tmp_path) -> None:
    storage = await _storage_with_tasks(tmp_path)
    try:
        rows = await storage.list_agent_tasks(
            session_key=KEY,
            status=AgentTaskStatus.RUNNING,
            newest_first=True,
        )

        assert [row.task_id for row in rows] == [RUNNING_TASK]
    finally:
        await storage.close()


# --------------------------------------------------------------------------
# TaskRuntime
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_runtime_list_surfaces_the_running_task(tmp_path) -> None:
    storage = await _storage_with_tasks(tmp_path)
    try:
        rows = await _runtime(storage).list(session_key=KEY)

        assert any(row.task_id == RUNNING_TASK for row in rows)
        assert rows[-1].task_id == RUNNING_TASK
    finally:
        await storage.close()


# --------------------------------------------------------------------------
# caller 1: sessions_yield
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_yield_waits_on_the_running_task(tmp_path, monkeypatch) -> None:
    """Issue #1805: `latest = rows[-1]` must be the in-flight task."""

    from agentos.tools.builtin import sessions as sessions_tools

    storage = await _storage_with_tasks(tmp_path)
    runtime = _runtime(storage)

    class _Manager:
        async def get_current_session(self) -> Any:
            return None

        async def get_session(self, key: str) -> Any:
            assert key == KEY
            return type("_Session", (), {"session_key": key, "status": "running"})()

    monkeypatch.setattr(sessions_tools, "_session_manager", _Manager())
    monkeypatch.setattr(sessions_tools, "_task_runtime", runtime)

    try:
        payload = json.loads(await sessions_tools.sessions_yield(session_key=KEY))
    finally:
        await storage.close()

    assert payload["waited"] is True
    assert payload["task_id"] == RUNNING_TASK
    assert payload["status"] == str(AgentTaskStatus.RUNNING)


# --------------------------------------------------------------------------
# caller 2: session reset / delete
# --------------------------------------------------------------------------


async def _drain_and_record(tmp_path, drain, *, name: str) -> list[str]:
    """Run a drain helper and report which task ids it tried to settle."""

    storage = await _storage_with_tasks(tmp_path, name=name)
    runtime = _runtime(storage)
    waited: list[str] = []
    original_wait = runtime.wait

    async def _recording_wait(task_id: str, timeout: float | None = None) -> Any:
        waited.append(task_id)
        return await original_wait(task_id, timeout)

    runtime.wait = _recording_wait  # type: ignore[method-assign]
    try:
        await drain(runtime)
    finally:
        await storage.close()
    return waited


@pytest.mark.asyncio
async def test_session_reset_settles_the_running_task(tmp_path) -> None:
    """Issue #1805: reset must see a running task past the 100th turn."""

    from agentos.gateway import rpc_sessions

    waited = await _drain_and_record(
        tmp_path,
        lambda runtime: rpc_sessions._drain_task_runtime_for_reset(runtime, KEY),
        name="reset.db",
    )

    assert RUNNING_TASK in waited


@pytest.mark.asyncio
async def test_session_delete_settles_the_running_task(tmp_path) -> None:
    """The delete path shares the same drain helper and the same blind spot."""

    from agentos.gateway import rpc_sessions

    waited = await _drain_and_record(
        tmp_path,
        lambda runtime: rpc_sessions._drain_task_runtime_for_session(
            runtime,
            KEY,
            source="sessions_delete",
            reason="session_deleted",
            op="delete",
        ),
        name="delete.db",
    )

    assert RUNNING_TASK in waited
