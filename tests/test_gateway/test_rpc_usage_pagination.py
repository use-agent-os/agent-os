"""Tests for session pagination and scope fallback in usage.status and usage.cost RPCs."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine.usage import UsageTracker
from agentos.gateway.rpc.registry import RpcContext
from agentos.gateway.rpc_usage import _handle_usage_cost, _handle_usage_status
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage


class _PaginatingSessionManager:
    """Session manager that paginates list_sessions at 100/page."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.calls: list[dict[str, Any]] = []

    async def list_sessions(
        self,
        agent_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        spawned_by: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append({"agent_id": agent_id, "limit": limit, "offset": offset})
        rows = self._rows
        if agent_id is not None:
            rows = [r for r in rows if r.get("agent_id") == agent_id]
        if status is not None:
            rows = [r for r in rows if r.get("status") == status]
        return rows[offset : offset + limit]


def _ctx(*, session_manager: Any = None, usage_tracker: Any = None) -> RpcContext:
    return RpcContext(
        conn_id="test",
        session_manager=session_manager,
        usage_tracker=usage_tracker,
        config=SimpleNamespace(llm=SimpleNamespace(model="claude-opus-4-7")),
    )


def _make_session(
    key: str,
    *,
    agent_id: str = "main",
    channel: str = "webchat",
    input_tokens: int = 10,
    output_tokens: int = 5,
    cost_usd: float = 0.01,
    status: str = "done",
) -> dict[str, Any]:
    return {
        "session_key": key,
        "agent_id": agent_id,
        "channel": channel,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_cost_usd": cost_usd,
        "cost_usd": cost_usd,
        "status": status,
        "model": "claude-opus-4-7",
        "created_at": 1000,
        "updated_at": 2000,
    }


@pytest.mark.asyncio
async def test_usage_status_pages_through_all_sessions_for_totals() -> None:
    """When storage holds >100 sessions, usage.status must include all sessions in totals."""
    rows = [
        _make_session(f"agent:main:s{i:03d}", input_tokens=100, output_tokens=20, cost_usd=0.01)
        for i in range(150)
    ]
    mgr = _PaginatingSessionManager(rows)
    ctx = _ctx(session_manager=mgr, usage_tracker=UsageTracker())

    payload = await _handle_usage_status(None, ctx)

    assert payload["totalSessions"] == 150
    assert len(payload["sessions"]) == 150
    assert payload["totalInputTokens"] == 15_000
    assert payload["totalOutputTokens"] == 3_000
    assert payload["totalCostUsd"] == 1.5


@pytest.mark.asyncio
async def test_usage_status_finds_older_requested_session_beyond_page_limit() -> None:
    """When querying usage.status for a specific sessionKey beyond offset 100,
    it must be returned.
    """
    rows = [_make_session(f"agent:main:s{i:03d}") for i in range(150)]
    target_key = "agent:main:s140"
    mgr = _PaginatingSessionManager(rows)
    ctx = _ctx(session_manager=mgr, usage_tracker=UsageTracker())

    payload = await _handle_usage_status({"sessionKey": target_key}, ctx)

    found = [s for s in payload["sessions"] if s.get("sessionKey") == target_key]
    assert len(found) == 1
    assert found[0]["sessionKey"] == target_key


@pytest.mark.asyncio
async def test_usage_cost_fallback_queries_session_key_beyond_first_page() -> None:
    """When cost ledger has no records, querying usage.cost by sessionKey must
    find older sessions.
    """
    rows = [
        _make_session(f"agent:main:s{i:03d}", cost_usd=0.05, input_tokens=200, output_tokens=50)
        for i in range(150)
    ]
    target_key = "agent:main:s130"
    mgr = _PaginatingSessionManager(rows)
    ctx = _ctx(session_manager=mgr, usage_tracker=UsageTracker())

    payload = await _handle_usage_cost({"sessionKey": target_key}, ctx)

    assert len(payload["breakdown"]) == 1
    assert payload["breakdown"][0]["sessionKey"] == target_key
    assert payload["totalCostUsd"] == 0.05


@pytest.mark.asyncio
async def test_usage_cost_fallback_queries_agent_id_and_falls_back_to_session_agent_id() -> None:
    """When tracker has no scope in memory, agent_id must resolve from the session record."""
    rows = [
        _make_session(f"agent:main:noise{i:03d}", agent_id="main", cost_usd=0.01)
        for i in range(120)
    ]
    analyst_rows = [
        _make_session("agent:analyst:query-1", agent_id="analyst", cost_usd=0.02),
        _make_session("agent:analyst:query-2", agent_id="analyst", cost_usd=0.03),
    ]
    mgr = _PaginatingSessionManager([*rows, *analyst_rows])
    # Empty usage tracker: get_session_scope will return ("unknown", "unknown")
    ctx = _ctx(session_manager=mgr, usage_tracker=UsageTracker())

    payload = await _handle_usage_cost({"agentId": "analyst"}, ctx)

    assert len(payload["breakdown"]) == 2
    assert {r["sessionKey"] for r in payload["breakdown"]} == {
        "agent:analyst:query-1",
        "agent:analyst:query-2",
    }
    assert payload["totalCostUsd"] == 0.05


@pytest.mark.asyncio
async def test_usage_status_real_storage_pages_through_all_sessions() -> None:
    """Real SQLite storage with >100 sessions must report accurate totalSessions and costs."""
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        for i in range(120):
            key = f"agent:main:s{i:03d}"
            await manager.create(key)
            await manager.update(
                key,
                input_tokens=10,
                output_tokens=5,
                total_cost_usd=0.01,
                billed_cost_usd=0.01,
                cost_source="provider_billed",
            )
        ctx = _ctx(session_manager=manager, usage_tracker=UsageTracker())
        payload = await _handle_usage_status(None, ctx)
        assert payload["totalSessions"] == 120
        assert len(payload["sessions"]) == 120
        assert payload["totalInputTokens"] == 1200
        assert payload["totalOutputTokens"] == 600
        assert payload["totalCostUsd"] == 1.2
    finally:
        await storage.close()
