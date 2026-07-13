"""Tests for graceful shutdown ordering (AC-M3)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentos.gateway.boot import GatewayServer


@pytest.mark.asyncio
async def test_runtime_drained_before_channel_stop() -> None:
    """task_runtime.shutdown() must complete before channel_manager.stop_all()."""
    call_order: list[str] = []

    async def mock_runtime_shutdown(**kwargs: object) -> None:
        call_order.append("task_runtime.shutdown")

    async def mock_stop_all() -> None:
        call_order.append("channel_manager.stop_all")

    # Build a minimal GatewayServer with mocked internals
    server = GatewayServer.__new__(GatewayServer)
    server._server = None
    server._task = None

    # Mock services with a task_runtime that records call order
    mock_services = MagicMock()
    mock_task_runtime = MagicMock()
    mock_task_runtime.shutdown = AsyncMock(side_effect=mock_runtime_shutdown)
    mock_services.task_runtime = mock_task_runtime
    # Make services.close() a no-op so the duplicate shutdown call is harmless
    mock_services.close = AsyncMock()
    server._services = mock_services

    # Mock channel_manager
    mock_channel_manager = MagicMock()
    mock_channel_manager.stop_all = AsyncMock(side_effect=mock_stop_all)
    server._channel_manager = mock_channel_manager

    # Patch registry to avoid real WS/broadcast logic
    mock_registry = MagicMock()
    mock_registry.broadcast = AsyncMock()
    mock_registry.all = MagicMock(return_value=[])

    with patch("agentos.gateway.boot.get_registry", return_value=mock_registry):
        await server.close(reason="test")

    assert call_order[0] == "task_runtime.shutdown", (
        f"Expected task_runtime.shutdown first, got: {call_order}"
    )
    assert call_order[1] == "channel_manager.stop_all", (
        f"Expected channel_manager.stop_all second, got: {call_order}"
    )
