"""Regression tests for ``search_transcript``'s per-session filter (#1801).

Agents identify sessions by ``session_key`` (it is what ``session_search``
returns in its payload), but the ``session_id`` filter used to compare only
against the internal UUID column, so a key-scoped search always came back
empty.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage
from agentos.tools.builtin.session_search import create_session_search_tool
from agentos.tools.registry import ToolRegistry


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
async def test_search_transcript_filters_by_session_key(manager, storage):
    first = await manager.create("agent:main:webchat:aaaa0001", agent_id="main")
    second = await manager.create("agent:main:webchat:aaaa0002", agent_id="main")
    await manager.append_message(first.session_key, role="user", content="deployed to production")
    await manager.append_message(second.session_key, role="user", content="deployed to staging")

    hits = await storage.search_transcript("deployed", session_id=first.session_key)

    assert [hit["session_key"] for hit in hits] == [first.session_key]


@pytest.mark.asyncio
async def test_search_transcript_still_filters_by_internal_session_id(manager, storage):
    first = await manager.create("agent:main:webchat:aaaa0003", agent_id="main")
    second = await manager.create("agent:main:webchat:aaaa0004", agent_id="main")
    await manager.append_message(first.session_key, role="user", content="deployed to production")
    await manager.append_message(second.session_key, role="user", content="deployed to staging")

    hits = await storage.search_transcript("deployed", session_id=first.session_id)

    assert [hit["session_key"] for hit in hits] == [first.session_key]


@pytest.mark.asyncio
async def test_search_transcript_canonicalizes_the_session_key(manager, storage):
    """Entries are stored under the canonical key, so a legacy alias must match too."""
    session = await manager.create("agent:main:webchat:aaaa0008", agent_id="main")
    await manager.append_message(session.session_key, role="user", content="deployed to production")

    hits = await storage.search_transcript("deployed", session_id="agent:Main:webchat:aaaa0008")

    assert [hit["session_key"] for hit in hits] == [session.session_key]


@pytest.mark.asyncio
async def test_search_transcript_unknown_session_matches_nothing(manager, storage):
    session = await manager.create("agent:main:webchat:aaaa0005", agent_id="main")
    await manager.append_message(session.session_key, role="user", content="deployed to production")

    assert await storage.search_transcript("deployed", session_id="agent:main:webchat:nope") == []


@pytest.mark.asyncio
async def test_session_search_tool_accepts_session_key(manager, storage):
    first = await manager.create("agent:main:webchat:aaaa0006", agent_id="main")
    second = await manager.create("agent:main:webchat:aaaa0007", agent_id="main")
    await manager.append_message(first.session_key, role="user", content="deployed to production")
    await manager.append_message(second.session_key, role="user", content="deployed to staging")

    registry = ToolRegistry()
    create_session_search_tool(storage, registry=registry)
    tool = registry.get("session_search")
    assert tool is not None

    payload = json.loads(await tool.handler(query="deployed", session_id=first.session_key))

    assert payload["result_count"] == 1
    assert payload["results"][0]["session_key"] == first.session_key
