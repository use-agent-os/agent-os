"""Children a restart cut off are reported to their waiting parent, once.

``SessionStorage.mark_abandoned_agent_tasks`` flips queued/running tasks to
``abandoned`` at startup with one SQL update and no terminal event, so a
parent waiting on its children was never woken (``subagent-lifecycle`` spec,
OpenSpec change ``refactor-agent-harness-core``).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agentos.gateway.subagent_announce import (
    RESTART_ANNOUNCED_KEY,
    _tracker,
    announce_restart_abandoned_children,
    announce_subagent_completion,
    set_background_completion_manager,
)
from agentos.session.models import AgentTaskStatus
from tests.test_gateway.test_subagent_announce_group_wake import (
    PARENT,
    PARENT_TASK,
    _SessionManager,
    _SessionRow,
    _TaskRuntime,
)

CHILD_A = "agent:worker:subagent:a"
CHILD_B = "agent:worker:subagent:b"


def _abandoned_task(session_key: str, task_id: str, created_at: int, **extra: Any) -> Any:
    return SimpleNamespace(
        task_id=task_id,
        session_key=session_key,
        agent_id="worker",
        run_kind=extra.pop("run_kind", "subagent"),
        status=AgentTaskStatus.ABANDONED,
        terminal_reason="process_restart",
        created_at=created_at,
        updated_at=created_at,
        finished_at=created_at,
        details=extra.pop(
            "details",
            {"metadata": {"parent_session_key": PARENT, "parent_task_id": PARENT_TASK}},
        ),
    )


class _RestartStorage:
    def __init__(self, tasks: list[Any]) -> None:
        self.tasks = tasks
        self.queries: list[tuple[list[str], int]] = []

    async def list_interrupted_agent_tasks(
        self, terminal_reasons: list[str], *, since_ms: int, limit: int = 500
    ) -> list[Any]:
        self.queries.append((terminal_reasons, since_ms))
        return [t for t in self.tasks if t.terminal_reason in terminal_reasons]

    async def update_agent_task(self, task_id: str, **fields: Any) -> None:
        for task in self.tasks:
            if task.task_id == task_id:
                for key, value in fields.items():
                    setattr(task, key, value)


@pytest.fixture(autouse=True)
def _clean_tracker():
    _tracker.evict(PARENT)
    set_background_completion_manager(None)
    yield
    _tracker.evict(PARENT)
    set_background_completion_manager(None)


def _setup() -> tuple[_SessionManager, _TaskRuntime, _RestartStorage, Any]:
    tasks = [_abandoned_task(CHILD_A, "task-a", 1), _abandoned_task(CHILD_B, "task-b", 2)]
    manager = _SessionManager(
        [_SessionRow(CHILD_A, status="running"), _SessionRow(CHILD_B, status="running")],
        tasks_by_session={task.session_key: [task] for task in tasks},
        transcripts={CHILD_A: "half of a", CHILD_B: ""},
    )
    runtime = _TaskRuntime()

    async def listener(event: Any) -> None:
        await announce_subagent_completion(event, session_manager=manager, task_runtime=runtime)

    return manager, runtime, _RestartStorage(tasks), listener


async def test_parent_is_woken_once_with_every_abandoned_child() -> None:
    manager, runtime, storage, listener = _setup()

    announced = await announce_restart_abandoned_children(
        storage=storage, listener=listener, max_age_ms=1000, now_ms=10_000
    )

    assert announced == 2
    assert len(runtime.sent) == 1
    wake_session, wake_message, _ = runtime.sent[0]
    assert wake_session == PARENT
    assert "task_id=task-a" in wake_message and "task_id=task-b" in wake_message
    assert "abandoned" in wake_message
    # Every child was recorded on the parent and is terminal now.
    assert len([m for m in manager.messages if m[0] == PARENT]) == 2
    assert {key for key, _status in manager.finished} == {CHILD_A, CHILD_B}
    assert storage.queries == [(["process_restart"], 9_000)]


async def test_a_second_restart_does_not_wake_the_parent_again() -> None:
    _manager, runtime, storage, listener = _setup()

    await announce_restart_abandoned_children(storage=storage, listener=listener, max_age_ms=1000)
    _tracker.evict(PARENT)  # a fresh process: nothing remembered in memory
    again = await announce_restart_abandoned_children(
        storage=storage, listener=listener, max_age_ms=1000
    )

    assert again == 0
    assert len(runtime.sent) == 1
    assert all(task.details[RESTART_ANNOUNCED_KEY] for task in storage.tasks)


async def test_top_level_and_orphan_tasks_are_left_alone() -> None:
    calls: list[Any] = []

    async def listener(event: Any) -> None:
        calls.append(event)

    storage = _RestartStorage(
        [
            _abandoned_task("agent:main:webchat:top", "t1", 1, run_kind="default"),
            _abandoned_task(CHILD_A, "t2", 2, details={"metadata": {}}),
        ]
    )

    announced = await announce_restart_abandoned_children(
        storage=storage, listener=listener, max_age_ms=1000
    )

    assert announced == 0
    assert calls == []


async def test_one_failing_child_does_not_stop_the_sweep() -> None:
    seen: list[str] = []

    async def listener(event: Any) -> None:
        seen.append(event.child_session_key)
        if event.child_session_key == CHILD_A:
            raise RuntimeError("parent session gone")

    storage = _RestartStorage(
        [_abandoned_task(CHILD_A, "task-a", 1), _abandoned_task(CHILD_B, "task-b", 2)]
    )

    announced = await announce_restart_abandoned_children(
        storage=storage, listener=listener, max_age_ms=1000
    )

    assert seen == [CHILD_A, CHILD_B]
    assert announced == 1
    assert RESTART_ANNOUNCED_KEY not in storage.tasks[0].details
