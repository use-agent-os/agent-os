"""Regression tests for transient Telegram Bot API connection failures."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agentos.channels.telegram import TelegramApiError, TelegramChannel, TelegramChannelConfig


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transient_error",
    [
        httpx.ConnectTimeout("tls handshake timed out"),
        httpx.PoolTimeout("no pooled connection became available"),
    ],
)
async def test_telegram_api_retries_pre_send_timeouts_like_connect_error(
    transient_error: httpx.TimeoutException,
) -> None:
    """ConnectTimeout/PoolTimeout also fail before a request is sent, so they
    should get the same retry-with-backoff treatment as ConnectError.

    ConnectTimeout is not a subclass of ConnectError (they're siblings under
    TransportError/TimeoutException respectively), so catching only
    ConnectError silently skips retrying these and raises immediately.
    """
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    client = AsyncMock()
    client.post = AsyncMock(side_effect=[transient_error, _Response()])
    channel._client = client
    channel._owns_client = False

    with patch("agentos.channels.telegram.asyncio.sleep", new=AsyncMock()) as sleep:
        result = await channel._api("sendMessage", {"chat_id": "1", "text": "hello"})

    assert result == {"id": 1}
    assert client.post.await_count == 2
    sleep.assert_awaited_once_with(0.25)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "post_send_error",
    [
        httpx.WriteTimeout("timed out sending request body"),
        httpx.ReadTimeout("timed out waiting for response"),
    ],
)
async def test_telegram_api_does_not_retry_post_send_timeouts(
    post_send_error: httpx.TimeoutException,
) -> None:
    """WriteTimeout/ReadTimeout can occur after Telegram already received the
    request, so retrying risks duplicate sends (e.g. sendMessage firing
    twice). These must raise immediately rather than retry.
    """
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    client = AsyncMock()
    client.post = AsyncMock(side_effect=[post_send_error])
    channel._client = client
    channel._owns_client = False

    with pytest.raises(TelegramApiError) as exc_info:
        await channel._api("sendMessage", {"chat_id": "1", "text": "hello"})

    assert str(exc_info.value) == "Telegram sendMessage request failed"
    assert client.post.await_count == 1


@pytest.mark.asyncio
async def test_telegram_long_poll_timeout_has_network_headroom() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token", poll_timeout_s=30))
    client = AsyncMock()
    client.post = AsyncMock(return_value=_Response())
    channel._client = client
    channel._owns_client = False

    await channel._api("getUpdates", channel._get_updates_payload())

    assert client.post.await_args.kwargs["timeout"] == 35.0


@pytest.mark.asyncio
async def test_telegram_api_redacts_token_after_connect_retries_are_exhausted() -> None:
    token = "secret-bot-token"
    channel = TelegramChannel(TelegramChannelConfig(token=token))
    request = httpx.Request("POST", f"https://api.telegram.org/bot{token}/sendMessage")
    client = AsyncMock()
    client.post = AsyncMock(
        side_effect=[
            httpx.ConnectError("connect failed", request=request),
            httpx.ConnectError("connect failed", request=request),
            httpx.ConnectError("connect failed", request=request),
        ]
    )
    channel._client = client
    channel._owns_client = False

    with (
        patch("agentos.channels.telegram.asyncio.sleep", new=AsyncMock()),
        pytest.raises(TelegramApiError) as exc_info,
    ):
        await channel._api("sendMessage", {"chat_id": "1", "text": "hello"})

    assert token not in str(exc_info.value)
    assert str(exc_info.value) == "Telegram sendMessage connection failed"
    assert client.post.await_count == 3


@pytest.mark.asyncio
async def test_telegram_api_redacts_token_from_http_status_errors() -> None:
    token = "secret-bot-token"
    channel = TelegramChannel(TelegramChannelConfig(token=token))
    request = httpx.Request("POST", f"https://api.telegram.org/bot{token}/getMe")
    client = AsyncMock()
    client.post = AsyncMock(return_value=httpx.Response(401, request=request))
    channel._client = client
    channel._owns_client = False

    with pytest.raises(TelegramApiError) as exc_info:
        await channel._api("getMe")

    assert token not in str(exc_info.value)
    assert str(exc_info.value) == "Telegram getMe failed with HTTP 401"
