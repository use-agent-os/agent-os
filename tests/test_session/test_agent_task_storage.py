from __future__ import annotations

import pytest

from agentos.session.models import AgentTaskRecord, AgentTaskStatus
from agentos.session.storage import SessionStorage


@pytest.mark.asyncio
async def test_agent_task_ledger_marks_active_tasks_abandoned_after_restart(tmp_path) -> None:
    db_path = tmp_path / "sessions.db"
    key = "agent:main:webchat:restart-ledger"

    storage = SessionStorage(str(db_path))
    await storage.connect()
    try:
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="queued-task",
                session_key=key,
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=AgentTaskStatus.QUEUED,
                created_at=100,
                updated_at=100,
            )
        )
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="running-task",
                session_key=key,
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=AgentTaskStatus.RUNNING,
                created_at=110,
                updated_at=120,
                started_at=120,
            )
        )
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="done-task",
                session_key=key,
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=AgentTaskStatus.SUCCEEDED,
                created_at=130,
                updated_at=140,
                started_at=135,
                finished_at=140,
            )
        )
    finally:
        await storage.close()

    restarted = SessionStorage(str(db_path))
    await restarted.connect()
    try:
        rows = await restarted.list_agent_tasks(session_key=key)
    finally:
        await restarted.close()

    by_id = {row.task_id: row for row in rows}
    assert by_id["queued-task"].status == AgentTaskStatus.ABANDONED
    assert by_id["queued-task"].terminal_reason == "process_restart"
    assert by_id["queued-task"].finished_at is not None
    assert by_id["running-task"].status == AgentTaskStatus.ABANDONED
    assert by_id["running-task"].terminal_reason == "process_restart"
    assert by_id["running-task"].finished_at is not None
    assert by_id["done-task"].status == AgentTaskStatus.SUCCEEDED
    assert by_id["done-task"].terminal_reason is None


@pytest.mark.asyncio
async def test_list_agent_tasks_for_sessions_groups_visible_session_tasks(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "sessions.db"))
    await storage.connect()
    try:
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="one-old",
                session_key="agent:main:webchat:one",
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=AgentTaskStatus.SUCCEEDED,
                created_at=100,
                updated_at=100,
            )
        )
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="one-new",
                session_key="agent:main:webchat:one",
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=AgentTaskStatus.RUNNING,
                created_at=200,
                updated_at=200,
            )
        )
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="two-task",
                session_key="agent:main:webchat:two",
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=AgentTaskStatus.QUEUED,
                created_at=150,
                updated_at=150,
            )
        )
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="hidden-task",
                session_key="agent:main:webchat:hidden",
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=AgentTaskStatus.QUEUED,
                created_at=50,
                updated_at=50,
            )
        )

        grouped = await storage.list_agent_tasks_for_sessions(
            ["agent:main:webchat:one", "agent:main:webchat:two"],
            limit_per_session=1,
        )
    finally:
        await storage.close()

    assert set(grouped) == {"agent:main:webchat:one", "agent:main:webchat:two"}
    assert [row.task_id for row in grouped["agent:main:webchat:one"]] == ["one-new"]
    assert [row.task_id for row in grouped["agent:main:webchat:two"]] == ["two-task"]


@pytest.mark.asyncio
async def test_list_agent_tasks_returns_most_recent_tasks_when_truncated(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "sessions_truncation.db"))
    await storage.connect()
    key = "agent:main:webchat:many-tasks"
    try:
        # Create 105 tasks: tasks 0-103 are succeeded, task 104 is running
        for i in range(105):
            await storage.create_agent_task(
                AgentTaskRecord(
                    task_id=f"task-{i:03d}",
                    session_key=key,
                    source_kind="webui",
                    queue_mode="followup",
                    run_kind="web_turn",
                    status=AgentTaskStatus.SUCCEEDED if i < 104 else AgentTaskStatus.RUNNING,
                    created_at=1000 + i,
                    updated_at=1000 + i,
                )
            )

        # Truncated query with default limit 100
        rows_100 = await storage.list_agent_tasks(session_key=key, limit=100)
        assert len(rows_100) == 100
        # Should contain the newest tasks (task-005 through task-104) in ASC order
        assert rows_100[0].task_id == "task-005"
        assert rows_100[-1].task_id == "task-104"
        assert rows_100[-1].status == AgentTaskStatus.RUNNING

        # Query without limit (limit=None)
        all_rows = await storage.list_agent_tasks(session_key=key, limit=None)
        assert len(all_rows) == 105
        assert all_rows[0].task_id == "task-000"
        assert all_rows[-1].task_id == "task-104"
    finally:
        await storage.close()
