"""``sessions.delete`` drops the session's derived memory index document.

Deleting a conversation is not a request to unlearn it: curated entries the
session produced, and their origins, stay.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.memory.evidence import MemoryEvidenceStore, text_hash
from agentos.memory.manager import MemoryManager
from agentos.memory.retrieval import MemoryRetriever
from agentos.memory.session_source import SessionSourceIndexer, session_source_path
from agentos.memory.store import LongTermMemoryStore
from agentos.memory.sync_manager import MemorySyncManager
from agentos.memory.turn_capture import TurnCaptureService
from agentos.session.manager import SessionManager
from agentos.session.models import SessionNode, TranscriptEntry
from agentos.session.storage import SessionStorage
from agentos.tools.builtin.memory_tools import create_memory_tools
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import ToolContext, current_tool_context


@pytest.fixture()
async def world(tmp_path: Path):
    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True)
    db = tmp_path / "memory.db"
    store = LongTermMemoryStore(db)
    await store.initialize()
    evidence = MemoryEvidenceStore(db)
    storage = SessionStorage(str(tmp_path / "sessions.db"))
    await storage.connect()
    sessions = SessionManager(storage, inject_time_prefix=False, checkpoint_workspace_dir=tmp_path)
    manager = MemoryManager(
        agent_id="main",
        db_path=db,
        store=store,
        sync_manager=MemorySyncManager(
            store=store, workspace_dir=workspace, memory_dir=workspace / "memory"
        ),
        retriever=MemoryRetriever(store),
        turn_capture=TurnCaptureService(workspace_dir=workspace, turns_dir=tmp_path / "turns"),
        workspace_dir=workspace,
        memory_dir=workspace / "memory",
        evidence=evidence,
    )
    registry = ToolRegistry()
    create_memory_tools(
        stores=store,
        retrievers=MemoryRetriever(store),
        memory_dir=str(workspace),
        registry=registry,
        evidence_stores={"main": evidence},
    )
    ctx = RpcContext(conn_id="test")
    ctx.session_manager = sessions
    ctx.memory_managers = {"main": manager}

    node = SessionNode(
        session_key="agent:main:webchat:direct:s1",
        session_id="s1-uuid",
        agent_id="main",
        updated_at=1_700_000_000_000,
    )
    await storage.upsert_session(node)
    await storage.append_transcript_entry(
        TranscriptEntry(
            session_id=node.session_id,
            session_key=node.session_key,
            role="user",
            content="our release codename is marigold",
            created_at=1_700_000_000_000,
        )
    )
    token = current_tool_context.set(
        ToolContext(
            agent_id="main", session_key=node.session_key, artifact_session_id=node.session_id
        )
    )
    try:
        added = await registry.get("memory").handler(
            action="add", content="Release codename is marigold"
        )
    finally:
        current_tool_context.reset(token)
    assert json.loads(added)["success"] is True
    await SessionSourceIndexer(storage=storage, store=store, agent_id="main").sync(force=True)

    yield type(
        "World",
        (),
        {"ctx": ctx, "store": store, "evidence": evidence, "node": node, "workspace": workspace},
    )
    await evidence.close()
    await storage.close()
    await store.close()


async def _delete(ctx: RpcContext, key: str) -> dict:
    res = await get_dispatcher().dispatch("r", "sessions.delete", {"key": key}, ctx)
    assert res.ok, res.error
    return res.payload


async def test_delete_then_search_finds_nothing_from_the_session(world) -> None:
    before, _mode = await world.store.search("marigold", min_score=0.0)
    assert session_source_path(world.node) in {hit.path for hit in before}

    payload = await _delete(world.ctx, world.node.session_key)

    assert payload == {"deleted": [world.node.session_key], "errors": []}
    after, _mode = await world.store.search("marigold", min_score=0.0)
    assert session_source_path(world.node) not in {hit.path for hit in after}
    assert session_source_path(world.node) not in await world.store.list_paths()


async def test_learned_facts_and_their_origins_stay(world) -> None:
    await _delete(world.ctx, world.node.session_key)

    memory_md = (world.workspace / "MEMORY.md").read_text(encoding="utf-8")
    assert "Release codename is marigold" in memory_md
    origins = await world.evidence.origins_for_item(
        "curated", "memory", text_hash("Release codename is marigold")
    )
    assert [(o.session_key, o.session_id) for o in origins] == [
        (world.node.session_key, world.node.session_id)
    ]


async def test_delete_without_a_memory_manager_still_deletes(world) -> None:
    world.ctx.memory_managers = {}

    payload = await _delete(world.ctx, world.node.session_key)

    assert payload == {"deleted": [world.node.session_key], "errors": []}
