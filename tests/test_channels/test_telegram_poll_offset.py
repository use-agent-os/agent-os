"""Regression tests for Telegram polling offset advancement and callback retry (Issue #1027)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig


def _callback_update(update_id: int = 10, cb_id: str = "cb_1") -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": cb_id,
            "data": "approve:req_1",
            "from": {"id": 123, "username": "alice"},
            "message": {
                "message_id": 456,
                "chat": {"id": 123, "type": "private"},
                "text": "Please approve",
            },
        },
    }


def _message_update(update_id: int = 20, message_id: int = 555) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {"id": 123, "username": "alice"},
            "chat": {"id": 123, "type": "private"},
            "text": "hello",
        },
    }


@pytest.mark.asyncio
async def test_poll_retains_offset_and_retries_failed_callback() -> None:
    """Callback handling failure retains offset; retry succeeds and advances offset."""
    channel = TelegramChannel(TelegramChannelConfig(token="token", poll_idle_sleep_s=0.1))
    attempts = 0
    poll_payloads: list[dict[str, Any]] = []

    async def _mock_handle_callback(cb: dict[str, Any]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient internal error")

    channel._handle_telegram_callback = _mock_handle_callback  # type: ignore[method-assign]

    async def _mock_api(method: str, payload: dict[str, Any] | None = None) -> Any:
        if method == "getUpdates":
            poll_payloads.append(dict(payload or {}))
            if len(poll_payloads) == 1:
                return [_callback_update(update_id=10, cb_id="cb_1")]
            if len(poll_payloads) == 2:
                # Retrying update 10
                return [_callback_update(update_id=10, cb_id="cb_1")]
            # After success, subsequent poll
            raise asyncio.CancelledError()
        return {}

    channel._api = _mock_api  # type: ignore[method-assign]

    with (
        patch("agentos.channels.telegram.asyncio.sleep", new=AsyncMock()),
        pytest.raises(asyncio.CancelledError),
    ):
        await channel._poll_loop()

    assert attempts == 2
    # First poll had no offset. Second poll retried with still no offset past 10.
    # Third poll sent offset=11 because update 10 succeeded on retry!
    assert "offset" not in poll_payloads[0]
    assert "offset" not in poll_payloads[1]
    assert poll_payloads[2]["offset"] == 11
    assert channel._update_offset == 11


@pytest.mark.asyncio
async def test_poll_stops_processing_later_updates_when_callback_fails() -> None:
    """A later update in the same batch must not be processed before the failed update succeeds."""
    channel = TelegramChannel(TelegramChannelConfig(token="token"))

    async def _failing_handle_callback(cb: dict[str, Any]) -> None:
        raise RuntimeError("dependency unavailable")

    channel._handle_telegram_callback = _failing_handle_callback  # type: ignore[method-assign]

    poll_payloads: list[dict[str, Any]] = []

    async def _mock_api(method: str, payload: dict[str, Any] | None = None) -> Any:
        if method == "getUpdates":
            poll_payloads.append(dict(payload or {}))
            if len(poll_payloads) == 1:
                return [
                    _callback_update(update_id=10, cb_id="cb_1"),
                    _message_update(update_id=11, message_id=555),
                ]
            raise asyncio.CancelledError()
        return {}

    channel._api = _mock_api  # type: ignore[method-assign]

    with (
        patch("agentos.channels.telegram.asyncio.sleep", new=AsyncMock()),
        pytest.raises(asyncio.CancelledError),
    ):
        await channel._poll_loop()

    # The later message update (11) must NOT be enqueued because update 10 failed
    assert channel._queue.empty()
    # The offset was not advanced to 11 or 12
    assert channel._update_offset is None
    # Next getUpdates call did not advance past update 10
    assert "offset" not in poll_payloads[1]


@pytest.mark.asyncio
async def test_poll_deduplicates_redelivered_callback_query() -> None:
    """After successful processing, re-delivery of the same callback query is not double-handled."""
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    calls = 0

    async def _mock_handle_callback(cb: dict[str, Any]) -> None:
        nonlocal calls
        calls += 1

    channel._handle_telegram_callback = _mock_handle_callback  # type: ignore[method-assign]

    poll_count = 0

    async def _mock_api(method: str, payload: dict[str, Any] | None = None) -> Any:
        nonlocal poll_count
        if method == "getUpdates":
            poll_count += 1
            if poll_count == 1:
                return [_callback_update(update_id=10, cb_id="cb_1")]
            if poll_count == 2:
                # Re-delivery of the same callback query
                return [_callback_update(update_id=10, cb_id="cb_1")]
            raise asyncio.CancelledError()
        return {}

    channel._api = _mock_api  # type: ignore[method-assign]

    with (
        patch("agentos.channels.telegram.asyncio.sleep", new=AsyncMock()),
        pytest.raises(asyncio.CancelledError),
    ):
        await channel._poll_loop()

    # Callback was handled exactly once, despite being returned in both polls
    assert calls == 1
    assert channel._update_offset == 11


@pytest.mark.asyncio
async def test_poll_bounds_retries_and_advances_on_permanent_failure() -> None:
    """A persistently failing callback is retried up to the limit, then advances past it."""
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    attempts = 0
    poll_payloads: list[dict[str, Any]] = []

    async def _always_fails(cb: dict[str, Any]) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("permanent corruption")

    channel._handle_telegram_callback = _always_fails  # type: ignore[method-assign]

    async def _mock_api(method: str, payload: dict[str, Any] | None = None) -> Any:
        if method == "getUpdates":
            poll_payloads.append(dict(payload or {}))
            if len(poll_payloads) <= 4:
                return [_callback_update(update_id=10, cb_id="cb_1")]
            raise asyncio.CancelledError()
        return {}

    channel._api = _mock_api  # type: ignore[method-assign]

    with (
        patch("agentos.channels.telegram.asyncio.sleep", new=AsyncMock()),
        pytest.raises(asyncio.CancelledError),
    ):
        await channel._poll_loop()

    # Initial attempt + 3 retries = 4 total attempts
    assert attempts == 4
    # On the 4th failure (exhausted), offset advanced to 11
    assert channel._update_offset == 11
    assert poll_payloads[-1]["offset"] == 11


@pytest.mark.asyncio
async def test_poll_advances_offset_for_normal_messages_and_unsupported_updates() -> None:
    """Normal messages and deliberately unsupported updates retain existing offset advancement."""
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    poll_payloads: list[dict[str, Any]] = []

    async def _mock_api(method: str, payload: dict[str, Any] | None = None) -> Any:
        if method == "getUpdates":
            poll_payloads.append(dict(payload or {}))
            if len(poll_payloads) == 1:
                return [
                    _message_update(update_id=20, message_id=555),
                    {"update_id": 21, "inline_query": {"id": "iq1"}},  # unsupported update
                ]
            raise asyncio.CancelledError()
        return {}

    channel._api = _mock_api  # type: ignore[method-assign]

    with (
        patch("agentos.channels.telegram.asyncio.sleep", new=AsyncMock()),
        pytest.raises(asyncio.CancelledError),
    ):
        await channel._poll_loop()

    # Message was enqueued
    assert channel._queue.qsize() == 1
    # Offset was advanced past both the message (20) and the unsupported update (21)
    assert channel._update_offset == 22
    assert poll_payloads[1]["offset"] == 22


@pytest.mark.asyncio
async def test_webhook_deduplicates_callback_query() -> None:
    """Webhook mode deduplicates repeated callback queries without re-handling."""
    channel = TelegramChannel(
        TelegramChannelConfig(
            token="token",
            transport="webhook",
            webhook_secret_token="secret-123",
        )
    )
    calls = 0

    async def _mock_handle_callback(cb: dict[str, Any]) -> None:
        nonlocal calls
        calls += 1

    channel._handle_telegram_callback = _mock_handle_callback  # type: ignore[method-assign]

    request = MagicMock(spec=Request)
    request.headers = {"X-Telegram-Bot-Api-Secret-Token": "secret-123"}
    request.json = AsyncMock(return_value=_callback_update(update_id=10, cb_id="cb_web_1"))

    # First delivery
    resp1 = await channel._handle_webhook(request)
    assert resp1.status_code == 200
    assert calls == 1

    # Second delivery (duplicate)
    resp2 = await channel._handle_webhook(request)
    assert resp2.status_code == 200
    assert calls == 1  # Not invoked again
