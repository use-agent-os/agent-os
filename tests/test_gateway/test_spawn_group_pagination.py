"""Spawn-group wake aggregator pages by spawned_by so >100 children are seen."""

from __future__ import annotations

import pytest

from agentos.gateway.subagent_announce import _list_spawn_group_sessions


class _Row:
    def __init__(self, session_key: str, spawned_by: str, origin: dict) -> None:
        self.session_key = session_key
        self.spawned_by = spawned_by
        self.origin = origin
        self.status = "running"


class _PaginatingManager:
    """list_sessions(spawned_by=...) returns all children paginated at 100/page."""

    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    async def list_sessions(
        self,
        agent_id=None,
        status=None,
        limit=100,
        offset=0,
        spawned_by=None,
    ):
        rows = self._rows
        if spawned_by is not None:
            rows = [r for r in rows if r.spawned_by == spawned_by]
        return rows[offset : offset + limit]


@pytest.mark.asyncio
async def test_group_aggregator_walks_past_first_page() -> None:
    """A parent with 250 children all in one parent_task_id group must be
    counted in full, not truncated at 200.
    """
    parent = "agent:main:main"
    task_id = "task-fanout"

    rows = [
        _Row(
            session_key=f"agent:main:subagent:{i:04d}",
            spawned_by=parent,
            origin={"parent_task_id": task_id},
        )
        for i in range(250)
    ]
    # Add a few rows in the same parent but a *different* task_id; they
    # must be filtered out at the app layer.
    rows.extend(
        _Row(
            session_key=f"agent:main:subagent:other-{i}",
            spawned_by=parent,
            origin={"parent_task_id": "other-task"},
        )
        for i in range(5)
    )

    mgr = _PaginatingManager(rows)
    group = await _list_spawn_group_sessions(
        parent_session_key=parent,
        parent_task_id=task_id,
        session_manager=mgr,
    )

    assert len(group) == 250
    assert all(r.spawned_by == parent for r in group)
    assert all(r.origin["parent_task_id"] == task_id for r in group)


@pytest.mark.asyncio
async def test_group_aggregator_returns_empty_when_no_match() -> None:
    mgr = _PaginatingManager([])
    group = await _list_spawn_group_sessions(
        parent_session_key="agent:main:main",
        parent_task_id="task-X",
        session_manager=mgr,
    )
    assert group == []
