"""Tests for #2232: usage.status/usage.cost silently capped at 100 sessions.

``_handle_usage_status``/``_handle_usage_cost`` called
``session_manager.list_sessions()`` with no arguments, which defaults to
``limit=100`` — deployments past 100 sessions got deflated totals, and a
specific older ``sessionKey`` lookup came back blank. ``usage.cost``'s
fallback path also resolved ``agent_id``/``channel`` only from the
in-memory tracker's scope cache, which is empty for any session that
predates a gateway restart.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agentos.engine.usage import UsageTracker
from agentos.gateway.rpc.registry import RpcContext
from agentos.gateway.rpc_usage import _handle_usage_cost, _handle_usage_status, _list_all_sessions
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage


def _ctx(*, session_manager=None, usage_tracker=None) -> RpcContext:
    return RpcContext(
        conn_id="test",
        session_manager=session_manager,
        usage_tracker=usage_tracker,
        config=SimpleNamespace(llm=SimpleNamespace(model="claude-opus-4-7")),
    )


async def _seeded_manager(
    count: int, *, agent_id: str = "main"
) -> tuple[SessionStorage, SessionManager]:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    for i in range(count):
        key = f"agent:{agent_id}:s{i:03d}"
        await manager.create(key, agent_id=agent_id)
        await manager.update(
            key,
            input_tokens=10,
            output_tokens=5,
            total_cost_usd=0.01,
            billed_cost_usd=0.01,
            cost_source="provider_billed",
        )
    return storage, manager


def test_usage_status_pages_through_all_sessions_beyond_the_default_page() -> None:
    """The issue's own repro: 120 sessions must all count toward the totals."""

    async def scenario():
        storage, manager = await _seeded_manager(120)
        try:
            ctx = _ctx(session_manager=manager, usage_tracker=UsageTracker())
            return await _handle_usage_status(None, ctx)
        finally:
            await storage.close()

    payload = asyncio.run(scenario())

    assert payload["totalSessions"] == 120
    assert len(payload["sessions"]) == 120
    assert payload["totalInputTokens"] == 1200
    assert payload["totalOutputTokens"] == 600
    assert payload["totalCostUsd"] == 1.2


def test_usage_status_finds_a_specific_session_older_than_the_first_page() -> None:
    """A sessionKey created before the last 100 sessions must still be found."""

    async def scenario():
        storage, manager = await _seeded_manager(150)
        try:
            ctx = _ctx(session_manager=manager, usage_tracker=UsageTracker())
            return await _handle_usage_status({"sessionKey": "agent:main:s005"}, ctx)
        finally:
            await storage.close()

    payload = asyncio.run(scenario())

    matches = [s for s in payload["sessions"] if s["session"] == "agent:main:s005"]
    assert len(matches) == 1


def test_usage_cost_pages_through_all_sessions_for_totals() -> None:
    """usage.cost's session-fallback path must also page past the default 100."""

    async def scenario():
        storage, manager = await _seeded_manager(130)
        try:
            ctx = _ctx(session_manager=manager, usage_tracker=UsageTracker())
            return await _handle_usage_cost(None, ctx)
        finally:
            await storage.close()

    payload = asyncio.run(scenario())

    assert len(payload["breakdown"]) == 130
    assert payload["totalCostUsd"] == pytest.approx(1.3, abs=1e-9)


def test_usage_cost_direct_session_key_lookup_finds_older_session() -> None:
    """Querying usage.cost by sessionKey must find a session past offset 100
    without requiring every earlier page to be paged through first."""

    async def scenario():
        storage, manager = await _seeded_manager(150)
        try:
            ctx = _ctx(session_manager=manager, usage_tracker=UsageTracker())
            return await _handle_usage_cost({"sessionKey": "agent:main:s140"}, ctx)
        finally:
            await storage.close()

    payload = asyncio.run(scenario())

    assert len(payload["breakdown"]) == 1
    assert payload["breakdown"][0]["sessionKey"] == "agent:main:s140"


def test_usage_cost_falls_back_to_stored_agent_id_and_channel_when_tracker_has_no_scope() -> None:
    """A session the tracker never saw (gateway restart, aged-out session)
    must still resolve agent_id/channel from the session record itself,
    not collapse to ("unknown", "unknown")."""

    async def scenario():
        storage = SessionStorage(":memory:")
        await storage.connect()
        manager = SessionManager(storage)
        try:
            await manager.create("agent:analyst:s001", agent_id="analyst")
            await manager.update(
                "agent:analyst:s001",
                input_tokens=100,
                output_tokens=10,
                total_cost_usd=0.02,
                billed_cost_usd=0.02,
                cost_source="provider_billed",
                channel="slack",
            )
            # Empty tracker: get_session_scope() has nothing for this key.
            ctx = _ctx(session_manager=manager, usage_tracker=UsageTracker())
            return await _handle_usage_cost({"agentId": "analyst"}, ctx)
        finally:
            await storage.close()

    payload = asyncio.run(scenario())

    assert len(payload["breakdown"]) == 1
    row = payload["breakdown"][0]
    assert row["agentId"] == "analyst"
    assert row["channelType"] == "slack"


def test_list_all_sessions_without_paging_support_falls_back_to_one_unpaginated_call() -> None:
    """Boundary: a session_manager whose list_sessions() takes no limit/offset
    (an older or minimal implementation) must still return its sessions, not
    silently come back empty. This is the exact regression a naive pagination
    helper introduces by calling list_sessions(limit=..., offset=...)
    unconditionally and swallowing the resulting TypeError."""

    class _NoPagingSessionManager:
        async def list_sessions(self):
            return [{"session_key": "only-session", "agent_id": "main"}]

    async def scenario():
        return await _list_all_sessions(_NoPagingSessionManager())

    sessions = asyncio.run(scenario())

    assert sessions == [{"session_key": "only-session", "agent_id": "main"}]
