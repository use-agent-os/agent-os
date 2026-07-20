"""Regression tests for transient Telegram Bot API connection failures."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {"ok": True, "result": {"id": 1}}


@pytest.mark.asyncio
async def test_telegram_api_retries_connect_error_before_sending() -> None:
    """TLS/connect failures are retried because no Bot API request was sent."""
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    client = AsyncMock()
    client.post = AsyncMock(side_effect=[httpx.ConnectError("tls"), _Response()])
    channel._client = client
    channel._owns_client = False

    with patch("agentos.channels.telegram.asyncio.sleep", new=AsyncMock()) as sleep:
        result = await channel._api("sendMessage", {"chat_id": "1", "text": "hello"})

    assert result == {"id": 1}
    assert client.post.await_count == 2
    sleep.assert_awaited_once_with(0.25)
