"""Test that session_search / search_transcript filters by session_key (#1801)."""

from __future__ import annotations

import json

import pytest

from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage
from agentos.tools.builtin.session_search import create_session_search_tool
from agentos.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_search_transcript_filters_by_session_key() -> None:
    """search_transcript must return results when filtered by session_key.

    Regression test for #1801.
    """
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage)
        s1 = await manager.create("agent:main:chat_1")
        s2 = await manager.create("agent:main:chat_2")

        # Insert transcript entries via manager (correct API)
        await manager.append_message(s1.session_key, role="user", content="deployed to production")
        await manager.append_message(s2.session_key, role="user", content="deployed to staging")

        # Search without session filter — should find both
        all_hits = await storage.search_transcript("deployed")
        assert len(all_hits) == 2

        # Search filtered by session_key — should find only session 1
        key_hits = await storage.search_transcript("deployed", session_key="agent:main:chat_1")
        assert len(key_hits) == 1
        assert key_hits[0]["session_key"] == "agent:main:chat_1"

        # Search filtered by session_id (backward compat) — should also work
        id_hits = await storage.search_transcript("deployed", session_id=s1.session_id)
        assert len(id_hits) == 1
        assert id_hits[0]["session_key"] == "agent:main:chat_1"

        # session_key takes precedence over session_id when both provided
        both_hits = await storage.search_transcript(
            "deployed",
            session_id=s2.session_id,
            session_key="agent:main:chat_1",
        )
        assert len(both_hits) == 1
        assert both_hits[0]["session_key"] == "agent:main:chat_1"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_session_search_tool_uses_session_key_filter() -> None:
    """session_search tool must filter by session_key (not session_id).

    Regression test for #1801.
    """
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage)
        s1 = await manager.create("agent:main:chat_1")
        s2 = await manager.create("agent:main:chat_2")

        await manager.append_message(
            s1.session_key, role="user", content="quantum widget blueprint"
        )
        await manager.append_message(
            s2.session_key, role="user", content="quantum widget prototype"
        )

        registry = ToolRegistry()
        create_session_search_tool(storage, registry=registry)
        registered = registry.get("session_search")
        assert registered is not None

        # Search with session filter using session_key
        result_json = await registered.handler(query="quantum widget", session="agent:main:chat_1")
        result = json.loads(result_json)
        assert result["result_count"] == 1
        assert result["results"][0]["session_key"] == "agent:main:chat_1"

        # Search with session_id alias
        result_alias_json = await registered.handler(
            query="quantum widget", session_id="agent:main:chat_2"
        )
        result_alias = json.loads(result_alias_json)
        assert result_alias["result_count"] == 1
        assert result_alias["results"][0]["session_key"] == "agent:main:chat_2"
    finally:
        await storage.close()
