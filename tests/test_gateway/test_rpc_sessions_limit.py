"""``sessions.list``/``sessions.preview``' ``limit`` param must never reach the
store unclamped.

``SessionStorage.list_sessions`` passes ``limit`` straight into a raw SQL
``... LIMIT ? OFFSET ?``. SQLite reads a negative ``LIMIT`` as "no limit at
all" (the same footgun ``mcp_server/bridge.py``'s ``_clamp_limit`` already
guards against for the MCP-facing session listing, and that
``gateway/rpc_cron.py``'s ``cron.runs`` handler has its own clamp for), so an
unvalidated negative or absurdly large value would return every session in
one response instead of the bounded page these handlers expect.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentos.gateway.config import GatewayConfig
from agentos.gateway.rpc import RpcContext
from agentos.gateway.rpc_sessions import (
    _MAX_SESSIONS_LIST_LIMIT,
    _handle_sessions_list,
    _handle_sessions_preview,
)


class _RecordingStorage:
    """Records the ``limit`` it actually receives instead of executing a query."""

    def __init__(self) -> None:
        self.received_limit: int | None = None

    async def list_sessions(self, limit: int = 50, **_kwargs: Any) -> list[Any]:
        self.received_limit = limit
        return []


def _ctx(storage: Any) -> RpcContext:
    ctx = RpcContext(conn_id="test", config=GatewayConfig())
    ctx.session_manager = type("_Manager", (), {"storage": storage})()
    return ctx


@pytest.mark.asyncio
async def test_sessions_list_negative_limit_is_clamped_to_one() -> None:
    storage = _RecordingStorage()

    await _handle_sessions_list({"limit": -1}, _ctx(storage))

    assert storage.received_limit == 1


@pytest.mark.asyncio
async def test_sessions_list_oversized_limit_is_capped() -> None:
    storage = _RecordingStorage()

    await _handle_sessions_list({"limit": 10_000_000}, _ctx(storage))

    assert storage.received_limit == _MAX_SESSIONS_LIST_LIMIT


@pytest.mark.asyncio
async def test_sessions_list_default_limit_is_fifty() -> None:
    storage = _RecordingStorage()

    await _handle_sessions_list(None, _ctx(storage))

    assert storage.received_limit == 50


@pytest.mark.asyncio
async def test_sessions_preview_negative_limit_is_clamped_to_one() -> None:
    storage = _RecordingStorage()

    await _handle_sessions_preview({"limit": -1}, _ctx(storage))

    assert storage.received_limit == 1


@pytest.mark.asyncio
async def test_sessions_preview_oversized_limit_is_capped() -> None:
    storage = _RecordingStorage()

    await _handle_sessions_preview({"limit": 10_000_000}, _ctx(storage))

    assert storage.received_limit == _MAX_SESSIONS_LIST_LIMIT
