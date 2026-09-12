from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage
from agentos.tools.builtin.session_search import create_session_search_tool
from agentos.tools.registry import ToolRegistry


@pytest.fixture
async def session_env(tmp_path: Path):
    db_path = tmp_path / "test_session_search.db"
    storage = SessionStorage(str(db_path))
    await storage.connect()
    manager = SessionManager(storage=storage)
    try:
        yield manager, storage
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_session_search_with_session_key_and_id(session_env) -> None:
    manager, storage = session_env
    node1 = await manager.create("main:chat_1", agent_id="main")
    node2 = await manager.create("main:chat_2", agent_id="main")

    await manager.append_message(node1.session_key, role="user", content="quantum widget deployed")
    await manager.append_message(node2.session_key, role="user", content="quantum widget pending")

    registry = ToolRegistry()
    create_session_search_tool(storage, registry=registry)
    tool = registry.get("session_search")
    assert tool is not None

    # Search with session_key as session_id filter
    res_key = json.loads(await tool.handler(query="deployed", session_id="main:chat_1"))
    assert res_key.get("result_count") == 1
    assert res_key["results"][0]["session_key"] == "main:chat_1"

    # Search with UUID session_id filter
    res_uuid = json.loads(await tool.handler(query="deployed", session_id=node1.session_id))
    assert res_uuid.get("result_count") == 1
    assert res_uuid["results"][0]["session_key"] == "main:chat_1"

    # Search for "deployed" in chat_2 should yield no matches
    res_other = json.loads(await tool.handler(query="deployed", session_id="main:chat_2"))
    assert res_other.get("results") == []
