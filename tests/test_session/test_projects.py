"""Tests for project CRUD, session membership, and project-scoped search."""

from __future__ import annotations

import pytest
import pytest_asyncio

from agentos.session.manager import ProjectUpdateConflictError, SessionManager
from agentos.session.storage import SessionStorage


@pytest_asyncio.fixture
async def storage():
    store = SessionStorage(":memory:")
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def manager(storage):
    return SessionManager(storage, inject_time_prefix=False)


@pytest.mark.asyncio
async def test_create_project_and_list_with_session_counts(manager):
    project = await manager.create_project("main", "Research", knowledge="Use Vietnamese.")
    assert project["name"] == "Research"
    assert project["knowledge"] == "Use Vietnamese."
    assert project["agent_id"] == "main"

    await manager.create(
        "agent:main:webchat:aaaa0001", agent_id="main", project_id=project["project_id"]
    )
    rows = await manager.list_projects("main")
    assert [(row["name"], row["session_count"]) for row in rows] == [("Research", 1)]


@pytest.mark.asyncio
async def test_create_project_rejects_duplicate_name_case_insensitive(manager):
    await manager.create_project("main", "Research")
    with pytest.raises(ValueError, match="already exists"):
        await manager.create_project("main", "research")
    # Names are unique globally, not per agent.
    with pytest.raises(ValueError, match="already exists"):
        await manager.create_project("other", "RESEARCH")


@pytest.mark.asyncio
async def test_create_project_rejects_empty_name_and_oversized_knowledge(manager):
    with pytest.raises(ValueError, match="empty"):
        await manager.create_project("main", "   ")
    with pytest.raises(ValueError, match="knowledge exceeds"):
        await manager.create_project(
            "main", "Big", knowledge="x" * (SessionManager.PROJECT_KNOWLEDGE_MAX_CHARS + 1)
        )


@pytest.mark.asyncio
async def test_session_create_with_unknown_project_raises(manager):
    with pytest.raises(KeyError, match="Project not found"):
        await manager.create("agent:main:webchat:aaaa0002", agent_id="main", project_id="missing")


@pytest.mark.asyncio
async def test_move_session_between_projects_and_detach(manager):
    project = await manager.create_project("main", "Research")
    await manager.create("agent:main:webchat:aaaa0003", agent_id="main")

    node = await manager.move_session_to_project(
        "agent:main:webchat:aaaa0003", project["project_id"]
    )
    assert node.project_id == project["project_id"]

    node = await manager.move_session_to_project("agent:main:webchat:aaaa0003", None)
    assert node.project_id is None


@pytest.mark.asyncio
async def test_move_session_across_agents_is_allowed(manager):
    # Projects are cross-agent: a session of any agent may join any project.
    project = await manager.create_project("other", "OtherProj")
    await manager.create("agent:main:webchat:aaaa0004", agent_id="main")
    node = await manager.move_session_to_project(
        "agent:main:webchat:aaaa0004", project["project_id"]
    )
    assert node.project_id == project["project_id"]
    rows = await manager.list_projects()
    assert [(row["name"], row["session_count"]) for row in rows] == [("OtherProj", 1)]


@pytest.mark.asyncio
async def test_update_project_name_and_knowledge(manager):
    project = await manager.create_project("main", "Research", knowledge="v1")
    updated = await manager.update_project(project["project_id"], name="Research 2", knowledge="v2")
    assert updated["name"] == "Research 2"
    assert updated["knowledge"] == "v2"
    assert updated["updated_at"] >= project["updated_at"]


@pytest.mark.asyncio
async def test_update_project_rejects_name_collision(manager):
    await manager.create_project("main", "One")
    other = await manager.create_project("main", "Two")
    with pytest.raises(ValueError, match="already exists"):
        await manager.update_project(other["project_id"], name="one")


@pytest.mark.asyncio
async def test_delete_project_detaches_sessions_but_keeps_them(manager):
    project = await manager.create_project("main", "Research")
    await manager.create(
        "agent:main:webchat:aaaa0005", agent_id="main", project_id=project["project_id"]
    )

    detached = await manager.delete_project(project["project_id"])
    assert detached == 1
    node = await manager.get_session("agent:main:webchat:aaaa0005")
    assert node is not None
    assert node.project_id is None
    assert await manager.get_project(project["project_id"]) is None


@pytest.mark.asyncio
async def test_delete_missing_project_raises(manager):
    with pytest.raises(KeyError, match="Project not found"):
        await manager.delete_project("missing")


@pytest.mark.asyncio
async def test_list_sessions_filters_by_project(manager):
    project = await manager.create_project("main", "Research")
    await manager.create(
        "agent:main:webchat:aaaa0006", agent_id="main", project_id=project["project_id"]
    )
    await manager.create("agent:main:webchat:aaaa0007", agent_id="main")

    rows = await manager.list_sessions(project_id=project["project_id"])
    assert [row["session_key"] for row in rows] == ["agent:main:webchat:aaaa0006"]


@pytest.mark.asyncio
async def test_get_project_knowledge_for_session(manager):
    project = await manager.create_project("main", "Research", knowledge="Shared facts.")
    await manager.create(
        "agent:main:webchat:aaaa0008", agent_id="main", project_id=project["project_id"]
    )
    await manager.create("agent:main:webchat:aaaa0009", agent_id="main")

    assert (
        await manager.get_project_knowledge_for_session("agent:main:webchat:aaaa0008")
        == "Shared facts."
    )
    assert await manager.get_project_knowledge_for_session("agent:main:webchat:aaaa0009") is None
    # Blank knowledge injects nothing.
    await manager.update_project(project["project_id"], knowledge="   ")
    assert await manager.get_project_knowledge_for_session("agent:main:webchat:aaaa0008") is None


@pytest.mark.asyncio
async def test_search_transcript_scoped_to_project(manager, storage):
    project = await manager.create_project("main", "Research")
    inside = await manager.create(
        "agent:main:webchat:aaaa000a", agent_id="main", project_id=project["project_id"]
    )
    outside = await manager.create("agent:main:webchat:aaaa000b", agent_id="main")
    await manager.append_message(inside.session_key, role="user", content="quantum widget alpha")
    await manager.append_message(outside.session_key, role="user", content="quantum widget beta")

    all_hits = await storage.search_transcript("quantum widget")
    assert len(all_hits) == 2

    scoped = await storage.search_transcript("quantum widget", project_id=project["project_id"])
    assert [hit["session_key"] for hit in scoped] == [inside.session_key]

    scoped_by_key = await storage.search_transcript("quantum widget", session_id=inside.session_key)
    assert [hit["session_key"] for hit in scoped_by_key] == [inside.session_key]

    scoped_by_id = await storage.search_transcript("quantum widget", session_id=inside.session_id)
    assert [hit["session_key"] for hit in scoped_by_id] == [inside.session_key]


@pytest.mark.asyncio
async def test_update_project_rename_does_not_clobber_concurrent_knowledge(manager):
    """A rename holding a stale row must not revert another writer's knowledge."""
    project = await manager.create_project("main", "Research", knowledge="v1")
    pid = project["project_id"]

    # Writer B saves new knowledge after writer A read the row; A then
    # renames without a precondition. Partial UPDATE keeps B's knowledge.
    await manager.update_project(pid, knowledge="v2")
    renamed = await manager.update_project(pid, name="Renamed")
    assert renamed["name"] == "Renamed"
    assert renamed["knowledge"] == "v2"


@pytest.mark.asyncio
async def test_update_project_cas_conflict_on_stale_read(manager):
    project = await manager.create_project("main", "Research", knowledge="v1")
    pid = project["project_id"]
    stale = project["updated_at"]

    fresh = await manager.update_project(pid, knowledge="v2")
    assert fresh["updated_at"] != stale
    with pytest.raises(ProjectUpdateConflictError):
        await manager.update_project(pid, knowledge="v3", expected_updated_at=stale)
    # Nothing was written by the losing update.
    row = await manager.get_project(pid)
    assert row["knowledge"] == "v2"

    # A matching precondition succeeds.
    winner = await manager.update_project(
        pid, knowledge="v3", expected_updated_at=fresh["updated_at"]
    )
    assert winner["knowledge"] == "v3"


@pytest.mark.asyncio
async def test_storage_unique_name_index_backstops_python_check(manager, storage):
    """Writing a duplicate name straight to storage (bypassing the advisory
    Python check, as a concurrent create would) hits the unique index."""
    from agentos.compat import aiosqlite
    from agentos.session.models import ProjectNode

    await manager.create_project("main", "Research")
    with pytest.raises(aiosqlite.IntegrityError):
        await storage.upsert_project(ProjectNode(agent_id="main", name="research"))


def test_write_cap_matches_injection_cap():
    """Anything that saves must inject in full — the caps may never diverge
    again (a larger write cap silently truncated every turn)."""
    from agentos.engine.runtime import TurnRunner

    assert (
        SessionManager.PROJECT_KNOWLEDGE_MAX_CHARS == TurnRunner.PROJECT_KNOWLEDGE_INJECT_MAX_CHARS
    )
