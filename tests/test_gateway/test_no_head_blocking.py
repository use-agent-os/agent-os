"""No head-session blocking when idle slots are available.

Different session_keys for the same agent run concurrently when global
slots are free — session A holding a slot does NOT block B/C/D from
taking the remaining idle slots. With ``max_concurrency=4`` and one
agent owning four sessions ABCD enqueued simultaneously, all four must
start within 4 seconds rather than serialising behind the first session.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentos.gateway.routing import RouteEnvelope, SourceKind
from agentos.gateway.task_runtime import TaskRuntime
from agentos.session.models import AgentTaskRecord

# ---------------------------------------------------------------------------
# Helpers (same pattern as test_fair_queuing.py)
# ---------------------------------------------------------------------------

def _make_envelope(agent_id: str, session_key: str) -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="test",
        agent_id=agent_id,
        session_key=session_key,
        input_provenance={"kind": "test"},
    )


def _make_storage() -> Any:
    storage = MagicMock()
    task_db: dict[str, AgentTaskRecord] = {}

    async def create(record: AgentTaskRecord) -> None:
        task_db[record.task_id] = record

    async def update(task_id: str, **kwargs: Any) -> None:
        rec = task_db.get(task_id)
        if rec is None:
            return
        for k, v in kwargs.items():
            if hasattr(rec, k):
                object.__setattr__(rec, k, v)

    async def get(task_id: str) -> AgentTaskRecord | None:
        return task_db.get(task_id)

    async def list_tasks(**kwargs: Any) -> list[AgentTaskRecord]:
        return list(task_db.values())

    storage.create_agent_task = create
    storage.update_agent_task = update
    storage.get_agent_task = get
    storage.list_agent_tasks = list_tasks
    return storage


# ---------------------------------------------------------------------------
# no_head_blocking_with_idle_slots
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_head_blocking_with_idle_slots() -> None:
    """max_concurrency=4 + 1 agent + 4 sessions ABCD → all 4 start concurrently.

    Each task sleeps for 1 s.  If head-blocking were present the tasks would
    serialise (total ~4 s); with the fix they all start immediately (total ~1 s).
    We assert all 4 tasks start within 2 s of enqueue to give CI headroom.
    """
    start_deadline = 2.0  # all 4 must have *started* within this many seconds

    agent_id = "agent-no-block"
    started_at: dict[str, float] = {}
    enqueue_time: float = 0.0

    gate = asyncio.Event()  # keeps tasks alive until we release them

    async def turn_handler(run: Any) -> None:
        started_at[run.session_key] = time.monotonic()
        await gate.wait()

    runtime = TaskRuntime(
        storage=_make_storage(),
        turn_handler=turn_handler,
        max_concurrency=4,
        max_pending_per_session=None,
    )

    sessions = [f"{agent_id}::sess-{label}" for label in ("A", "B", "C", "D")]
    envs = [_make_envelope(agent_id, sk) for sk in sessions]

    enqueue_time = time.monotonic()
    handles = []
    for env in envs:
        h = await runtime.enqueue(env, "hello")
        handles.append(h)

    # Give the event loop enough ticks for all 4 tasks to reach their handler.
    deadline = asyncio.get_event_loop().time() + start_deadline
    while len(started_at) < 4 and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)

    # Release all tasks so they can finish cleanly.
    gate.set()

    # Wait for completion (with generous timeout).
    for h in handles:
        try:
            await asyncio.wait_for(runtime.wait(h.task_id), timeout=10.0)
        except (TimeoutError, KeyError):
            pass

    assert len(started_at) == 4, (
        f"Only {len(started_at)}/4 sessions started within {start_deadline}s: "
        f"{list(started_at.keys())}"
    )

    # All 4 must have started within START_DEADLINE seconds of the first enqueue.
    last_start = max(started_at.values()) - enqueue_time
    assert last_start <= start_deadline, (
        f"Last session started {last_start:.2f}s after enqueue — head-blocking "
        f"suspected (expected all 4 within {start_deadline}s): {started_at}"
    )
