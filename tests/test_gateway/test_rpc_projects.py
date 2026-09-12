"""Tests for projects domain RPC handlers (real storage-backed manager)."""

from __future__ import annotations

import pytest
import pytest_asyncio

from agentos.gateway.config import GatewayConfig
from agentos.gateway.rpc import RpcContext, get_dispatcher
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


@pytest.fixture
def ctx_no_manager() -> RpcContext:
    return RpcContext(conn_id="test-conn", config=GatewayConfig())


async def _create_project(dispatcher, ctx, name="Research", knowledge="Shared facts."):
    res = await dispatcher.dispatch(
        "r1", "projects.create", {"agentId": "main", "name": name, "knowledge": knowledge}, ctx
    )
    assert res.ok is True
    return res.payload["project"]


class TestProjectsCreate:
    @pytest.mark.asyncio
    async def test_create_returns_dual_case_row(self, dispatcher, ctx):
        project = await _create_project(dispatcher, ctx)
        assert project["name"] == "Research"
        assert project["knowledge"] == "Shared facts."
        assert project["project_id"] == project["projectId"]
        assert project["agent_id"] == "main"
        assert project["agentId"] == "main"
        assert project["session_count"] == 0
        assert project["sessionCount"] == 0

    @pytest.mark.asyncio
    async def test_create_requires_name(self, dispatcher, ctx):
        res = await dispatcher.dispatch("r1", "projects.create", {"agentId": "main"}, ctx)
        assert res.ok is False

    @pytest.mark.asyncio
    async def test_create_requires_manager(self, dispatcher, ctx_no_manager):
        res = await dispatcher.dispatch("r1", "projects.create", {"name": "X"}, ctx_no_manager)
        assert res.ok is False
        assert res.error.code == "UNAVAILABLE"


class TestProjectsListGetUpdateDelete:
    @pytest.mark.asyncio
    async def test_list_includes_session_counts(self, dispatcher, ctx, manager):
        project = await _create_project(dispatcher, ctx)
        await manager.create(
            "agent:main:webchat:11110001", agent_id="main", project_id=project["project_id"]
        )
        res = await dispatcher.dispatch("r1", "projects.list", {"agentId": "main"}, ctx)
        assert res.ok is True
        assert res.payload["count"] == 1
        row = res.payload["projects"][0]
        assert row["sessionCount"] == 1

    @pytest.mark.asyncio
    async def test_get_unknown_project_errors(self, dispatcher, ctx):
        res = await dispatcher.dispatch("r1", "projects.get", {"projectId": "missing"}, ctx)
        assert res.ok is False

    @pytest.mark.asyncio
    async def test_update_knowledge(self, dispatcher, ctx):
        project = await _create_project(dispatcher, ctx)
        res = await dispatcher.dispatch(
            "r1",
            "projects.update",
            {"projectId": project["project_id"], "knowledge": "v2"},
            ctx,
        )
        assert res.ok is True
        assert res.payload["project"]["knowledge"] == "v2"

    @pytest.mark.asyncio
    async def test_update_with_stale_expected_updated_at_conflicts(self, dispatcher, ctx):
        project = await _create_project(dispatcher, ctx)
        first = await dispatcher.dispatch(
            "r1",
            "projects.update",
            {"projectId": project["project_id"], "knowledge": "v2"},
            ctx,
        )
        assert first.ok is True
        res = await dispatcher.dispatch(
            "r2",
            "projects.update",
            {
                "projectId": project["project_id"],
                "knowledge": "v3",
                "expectedUpdatedAt": project["updated_at"],
            },
            ctx,
        )
        assert res.ok is False
        assert res.error.code == "project.conflict"
        # The losing write must not have landed.
        fresh = await dispatcher.dispatch(
            "r3", "projects.get", {"projectId": project["project_id"]}, ctx
        )
        assert fresh.payload["project"]["knowledge"] == "v2"

        winner = await dispatcher.dispatch(
            "r4",
            "projects.update",
            {
                "projectId": project["project_id"],
                "knowledge": "v3",
                "expectedUpdatedAt": first.payload["project"]["updated_at"],
            },
            ctx,
        )
        assert winner.ok is True
        assert winner.payload["project"]["knowledge"] == "v3"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_expected", [True, False])
    async def test_update_rejects_bool_expected_updated_at(self, dispatcher, ctx, bad_expected):
        # bool is a subclass of int in Python, so a naive `isinstance(x, int)`
        # check accepts True/False as if they were real ms timestamps. That
        # previously reached the storage compare-and-swap as 1/0, which never
        # matches a real updated_at and surfaces the misleading
        # "project.conflict" / reload-and-retry error instead of a clear
        # INVALID_REQUEST for the caller's bad param.
        project = await _create_project(dispatcher, ctx)
        res = await dispatcher.dispatch(
            "r1",
            "projects.update",
            {
                "projectId": project["project_id"],
                "knowledge": "v2",
                "expectedUpdatedAt": bad_expected,
            },
            ctx,
        )
        assert res.ok is False
        assert res.error.code == "INVALID_REQUEST"
        # The rejected write must not have landed.
        fresh = await dispatcher.dispatch(
            "r2", "projects.get", {"projectId": project["project_id"]}, ctx
        )
        assert fresh.payload["project"]["knowledge"] == "Shared facts."

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_name", ["", "   ", "\t\n"])
    async def test_update_rejects_empty_or_whitespace_name(self, dispatcher, ctx, bad_name):
        project = await _create_project(dispatcher, ctx)
        res = await dispatcher.dispatch(
            "r1",
            "projects.update",
            {"projectId": project["project_id"], "name": bad_name},
            ctx,
        )
        assert res.ok is False
        assert res.error.code == "INVALID_REQUEST"
        fresh = await dispatcher.dispatch(
            "r2", "projects.get", {"projectId": project["project_id"]}, ctx
        )
        assert fresh.payload["project"]["name"] == "Research"

    @pytest.mark.asyncio
    async def test_delete_reports_detached_sessions(self, dispatcher, ctx, manager):
        project = await _create_project(dispatcher, ctx)
        await manager.create(
            "agent:main:webchat:11110002", agent_id="main", project_id=project["project_id"]
        )
        res = await dispatcher.dispatch(
            "r1", "projects.delete", {"projectId": project["project_id"]}, ctx
        )
        assert res.ok is True
        assert res.payload["sessionsCleared"] == 1
        node = await manager.get_session("agent:main:webchat:11110002")
        assert node is not None
        assert node.project_id is None


class TestSessionsProjectIntegration:
    @pytest.mark.asyncio
    async def test_sessions_create_accepts_project_id(self, dispatcher, ctx):
        project = await _create_project(dispatcher, ctx)
        res = await dispatcher.dispatch(
            "r1",
            "sessions.create",
            {"agentId": "main", "projectId": project["project_id"]},
            ctx,
        )
        assert res.ok is True
        assert res.payload["projectId"] == project["project_id"]

    @pytest.mark.asyncio
    async def test_sessions_create_rejects_unknown_project(self, dispatcher, ctx):
        res = await dispatcher.dispatch(
            "r1",
            "sessions.create",
            {"agentId": "main", "projectId": "missing"},
            ctx,
        )
        assert res.ok is False

    @pytest.mark.asyncio
    async def test_sessions_patch_moves_and_detaches(self, dispatcher, ctx, manager):
        project = await _create_project(dispatcher, ctx)
        await manager.create("agent:main:webchat:11110003", agent_id="main")

        res = await dispatcher.dispatch(
            "r1",
            "sessions.patch",
            {"key": "agent:main:webchat:11110003", "projectId": project["project_id"]},
            ctx,
        )
        assert res.ok is True
        assert "projectId" in res.payload["updated"]
        node = await manager.get_session("agent:main:webchat:11110003")
        assert node.project_id == project["project_id"]

        res = await dispatcher.dispatch(
            "r2",
            "sessions.patch",
            {"key": "agent:main:webchat:11110003", "projectId": None},
            ctx,
        )
        assert res.ok is True
        node = await manager.get_session("agent:main:webchat:11110003")
        assert node.project_id is None

    @pytest.mark.asyncio
    async def test_sessions_patch_moves_across_agents(self, dispatcher, ctx, manager):
        # Projects are cross-agent: any agent's session may join any project.
        other = await manager.create_project("other", "OtherProj")
        await manager.create("agent:main:webchat:11110004", agent_id="main")
        res = await dispatcher.dispatch(
            "r1",
            "sessions.patch",
            {"key": "agent:main:webchat:11110004", "projectId": other["project_id"]},
            ctx,
        )
        assert res.ok is True
        node = await manager.get_session("agent:main:webchat:11110004")
        assert node.project_id == other["project_id"]

    @pytest.mark.asyncio
    async def test_sessions_list_filters_and_exposes_project_id(self, dispatcher, ctx, manager):
        project = await _create_project(dispatcher, ctx)
        await manager.create(
            "agent:main:webchat:11110005", agent_id="main", project_id=project["project_id"]
        )
        await manager.create("agent:main:webchat:11110006", agent_id="main")

        res = await dispatcher.dispatch("r1", "sessions.list", {"limit": 50}, ctx)
        assert res.ok is True
        by_key = {row["key"]: row for row in res.payload["sessions"]}
        assert by_key["agent:main:webchat:11110005"]["projectId"] == project["project_id"]
        assert by_key["agent:main:webchat:11110005"]["project_id"] == project["project_id"]
        assert by_key["agent:main:webchat:11110006"]["projectId"] is None

        res = await dispatcher.dispatch(
            "r2", "sessions.list", {"projectId": project["project_id"]}, ctx
        )
        assert res.ok is True
        assert [row["key"] for row in res.payload["sessions"]] == ["agent:main:webchat:11110005"]
