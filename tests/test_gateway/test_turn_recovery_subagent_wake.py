"""A crash under a parent that had children running gives the parent one turn.

After the crash, restart recovery resumes the parent's interrupted turn and the
restart announce reports the abandoned children to the same parent. Each used
to enqueue a turn of its own (the wake is ``internal_system``, which the
recovery does not treat as the user writing first), so the parent ran twice:
a "continue" turn and an "your children were abandoned" turn, in either order.
While a recovery of the parent is pending or running, the children are
recorded on the parent and the wake is skipped: the resumed turn reads them.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agentos.gateway.subagent_announce import (
    _tracker,
    announce_restart_abandoned_children,
    announce_subagent_completion,
)
from agentos.session.models import AgentTaskRecord, AgentTaskStatus
from tests.test_gateway.test_turn_recovery import KEY, _Env, _interrupted_session

CHILD = "agent:main:subagent:abandoned-child"


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[_Env]:
    environment = _Env(tmp_path)
    await environment.open()
    yield environment
    await environment.close()


async def _abandoned_child(env: _Env, parent_task_id: str) -> None:
    assert env.manager is not None and env.storage is not None
    await env.manager.create(
        CHILD,
        spawned_by=KEY,
        origin={"kind": "subagent", "parent_task_id": parent_task_id},
    )
    await env.manager.append_message(CHILD, "user", "Look into it.")
    now = int(time.time() * 1000)
    await env.storage.create_agent_task(
        AgentTaskRecord(
            session_key=CHILD,
            source_kind="system",
            run_kind="subagent",
            status=AgentTaskStatus.ABANDONED,
            terminal_reason="process_restart",
            created_at=now - 500,
            started_at=now - 500,
            details={
                "source_name": "test",
                "metadata": {"parent_session_key": KEY, "parent_task_id": parent_task_id},
            },
        )
    )


async def _parent_turns(env: _Env) -> list[dict[str, Any]]:
    assert env.runner is not None
    for _ in range(100):
        await asyncio.sleep(0.02)
    return [turn for turn in env.runner.turns if turn["session_key"] == KEY]


async def test_a_recovered_parent_is_not_also_woken_for_its_abandoned_children(
    env: _Env,
) -> None:
    assert env.manager is not None and env.storage is not None
    parent = await _interrupted_session(env, tail="round_complete")
    await _abandoned_child(env, parent.task_id)
    _tracker.evict(KEY)
    recovery = env.recovery()

    async def listener(event: Any) -> None:
        await announce_subagent_completion(
            event, session_manager=env.manager, task_runtime=env.runtime
        )

    # Boot order: the recovery scan first, then the restart announce.
    await recovery.scan()
    announced = await announce_restart_abandoned_children(
        storage=env.storage,
        listener=listener,
        max_age_ms=60_000,
        turn_recovery=recovery,
    )
    await recovery.wait_idle(timeout=5)
    turns = await _parent_turns(env)

    assert announced == 1
    assert len(turns) == 1, [turn["message"][:60] for turn in turns]
    assert turns[0]["provenance"].get("source_tool") != "subagent_completion"
    # The child is still on the parent's record for the resumed turn to read.
    transcript = await env.manager.read_transcript(KEY, limit=50)
    assert any(CHILD in str(row.get("content")) for row in transcript)
    _tracker.evict(KEY)


async def test_without_a_recovery_the_parent_is_still_woken(env: _Env) -> None:
    assert env.manager is not None and env.storage is not None
    parent = await _interrupted_session(env, tail="finished")
    await _abandoned_child(env, parent.task_id)
    _tracker.evict(KEY)
    recovery = env.recovery()

    async def listener(event: Any) -> None:
        await announce_subagent_completion(
            event, session_manager=env.manager, task_runtime=env.runtime
        )

    await recovery.scan()  # the parent's turn had finished: nothing to resume
    assert not recovery.is_recovering(KEY)
    await announce_restart_abandoned_children(
        storage=env.storage, listener=listener, max_age_ms=60_000, turn_recovery=recovery
    )
    turns = await _parent_turns(env)

    assert len(turns) == 1
    assert turns[0]["provenance"].get("source_tool") == "subagent_completion"
    _tracker.evict(KEY)
