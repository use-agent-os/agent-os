import asyncio
from unittest.mock import MagicMock

import pytest

from agentos.gateway.routing import RouteEnvelope, SourceKind
from agentos.gateway.task_runtime import TaskRuntime
from agentos.session.models import AgentTaskStatus


def _env(sk: str) -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="t",
        agent_id="a",
        session_key=sk,
    )


def _mock_storage(fail_for: set[str]) -> MagicMock:
    s = MagicMock()
    db = {}

    async def create(r):
        db[r.task_id] = r

    async def update(task_id, **kw):
        if kw.get("status") == AgentTaskStatus.RUNNING and task_id in fail_for:
            raise RuntimeError("database is locked")
        rec = db.get(task_id)
        if rec:
            for k, v in kw.items():
                if hasattr(rec, k):
                    object.__setattr__(rec, k, v)

    async def get(t):
        return db.get(t)

    async def lst(**k):
        return list(db.values())

    s.create_agent_task = create
    s.update_agent_task = update
    s.get_agent_task = get
    s.list_agent_tasks = lst
    return s


@pytest.mark.asyncio
async def test_concurrency_slot_leak_on_execute_error() -> None:
    fail_for: set[str] = set()

    async def handler(run):
        await asyncio.sleep(0.001)

    rt = TaskRuntime(
        storage=_mock_storage(fail_for),
        turn_handler=handler,
        max_concurrency=1,
    )

    h1 = await rt.enqueue(_env("agent:a:s1"), "first")
    fail_for.add(h1.task_id)
    try:
        await rt.wait(h1.task_id, timeout=1.0)
    except Exception:
        pass

    # Wait a bit for the execution task to fail and clean up
    await asyncio.sleep(0.2)
    assert rt._global_in_flight == 0

    h2 = await rt.enqueue(_env("agent:a:s2"), "second")
    # This should not timeout if the runtime is not wedged
    await asyncio.wait_for(rt.wait(h2.task_id, timeout=3.0), timeout=3.5)
