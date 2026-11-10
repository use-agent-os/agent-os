"""``sessions.list`` reports how many sessions match, not only the page size."""

from __future__ import annotations

import pytest
import pytest_asyncio

from agentos.gateway.config import GatewayConfig
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.gateway.rpc_sessions import _sessions_total
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage


@pytest.fixture
def dispatcher():
    return get_dispatcher()


@pytest_asyncio.fixture
async def manager():
    storage = SessionStorage(":memory:")
    await storage.connect()
    mgr = SessionManager(storage, inject_time_prefix=False)
    yield mgr
    await storage.close()


@pytest.fixture
def ctx(manager) -> RpcContext:
    context = RpcContext(conn_id="test-conn", config=GatewayConfig())
    context.session_manager = manager
    return context


async def _seed(manager, count: int, *, project_id: str | None = None, prefix: str = "1111"):
    for i in range(count):
        await manager.create(
            f"agent:main:webchat:{prefix}{i:04d}", agent_id="main", project_id=project_id
        )


@pytest.mark.asyncio
async def test_truncated_page_reports_the_true_total(dispatcher, ctx, manager):
    await _seed(manager, 5)
    res = await dispatcher.dispatch("r1", "sessions.list", {"limit": 3}, ctx)
    assert res.ok is True
    assert len(res.payload["sessions"]) == 3
    # ``count`` keeps its old meaning (rows returned); ``total`` is the whole set.
    assert res.payload["count"] == 3
    assert res.payload["total"] == 5


@pytest.mark.asyncio
async def test_short_page_total_is_the_page(dispatcher, ctx, manager):
    await _seed(manager, 2)
    res = await dispatcher.dispatch("r1", "sessions.list", {"limit": 50}, ctx)
    assert res.payload["count"] == 2
    assert res.payload["total"] == 2


@pytest.mark.asyncio
async def test_project_filter_counts_only_that_project(dispatcher, ctx, manager):
    project = await manager.create_project("main", "Research")
    await _seed(manager, 4, project_id=project["project_id"], prefix="2222")
    await _seed(manager, 3, prefix="3333")
    res = await dispatcher.dispatch(
        "r1", "sessions.list", {"limit": 2, "projectId": project["project_id"]}, ctx
    )
    assert len(res.payload["sessions"]) == 2
    assert res.payload["total"] == 4


@pytest.mark.asyncio
async def test_storage_without_a_counter_reports_the_page():
    class NoCount:
        pass

    assert await _sessions_total(NoCount(), 3, 3, None) == 3


@pytest.mark.asyncio
async def test_failing_counter_reports_the_page():
    class Broken:
        async def count_sessions(self) -> int:
            raise RuntimeError("db locked")

    assert await _sessions_total(Broken(), 3, 3, None) == 3


@pytest.mark.asyncio
async def test_no_session_manager_reports_zero_total(dispatcher):
    context = RpcContext(conn_id="test-conn", config=GatewayConfig())
    res = await dispatcher.dispatch("r1", "sessions.list", {}, context)
    assert res.payload["sessions"] == []
    assert res.payload["count"] == 0
    assert res.payload["total"] == 0


@pytest.mark.asyncio
async def test_rows_carry_the_session_creation_time(dispatcher, ctx, manager):
    # The console's Session details shows "Created" from this field.
    await _seed(manager, 1)
    node = await manager.get_session("agent:main:webchat:11110000")
    assert node is not None
    res = await dispatcher.dispatch("r1", "sessions.list", {"limit": 5}, ctx)
    (row,) = res.payload["sessions"]
    assert row["created_at"] == row["createdAt"] == node.created_at
    assert isinstance(row["created_at"], int) and row["created_at"] > 0
