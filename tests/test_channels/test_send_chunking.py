"""Regression tests for final-only send() message-length chunking (#1544).

Telegram caps one message at 4096 rendered-HTML chars and Discord at 2000
plain chars. ``send_streaming`` already split oversized text via
``_split_for_limit``/``_post_segments``, but the non-streaming ``send()`` on
both adapters posted the whole final reply in one API call — the platform
rejected or truncated it and the reply was lost whenever streaming was
disabled. These tests pin chunked delivery through the public ``send()``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from agentos.channels.discord import DiscordChannel, DiscordChannelConfig
from agentos.channels.telegram import (
    _MESSAGE_TEXT_LIMIT,
    TelegramChannel,
    TelegramChannelConfig,
)
from agentos.channels.types import OutgoingMessage

# ---------------------------------------------------------------- Telegram


class _TgResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {"ok": True, "result": {"id": 1, "message_id": 1}}


def _tg_channel() -> tuple[TelegramChannel, AsyncMock]:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    client = AsyncMock()
    client.post = AsyncMock(return_value=_TgResponse())
    channel._client = client
    channel._owns_client = False
    return channel, client


@pytest.mark.asyncio
async def test_telegram_send_chunks_oversized_final_reply() -> None:
    channel, client = _tg_channel()
    long_content = "x" * 5000  # exceeds the 4096 rendered-HTML cap

    await channel.send(OutgoingMessage(content=long_content, reply_to="chat1"))

    posts = client.post.await_args_list
    assert len(posts) == 2  # one oversized post == lost reply; must chunk
    texts = [call.kwargs["json"]["text"] for call in posts]
    assert all(len(text) <= _MESSAGE_TEXT_LIMIT for text in texts)
    assert texts[0] + texts[1] == long_content  # nothing dropped or reordered


@pytest.mark.asyncio
async def test_telegram_send_short_reply_stays_one_message() -> None:
    channel, client = _tg_channel()

    await channel.send(OutgoingMessage(content="hello", reply_to="chat1"))

    posts = client.post.await_args_list
    assert len(posts) == 1
    assert posts[0].kwargs["json"]["chat_id"] == "chat1"


@pytest.mark.asyncio
async def test_telegram_send_chunks_respect_thread_and_replies_per_segment() -> None:
    channel, client = _tg_channel()
    first = "a" * 4000 + "\n" + "b" * 2000

    await channel.send(
        OutgoingMessage(
            content=first,
            reply_to="chat1",
            metadata={"thread_id": "77", "reply_to_message_id": "55"},
        )
    )

    posts = client.post.await_args_list
    assert len(posts) >= 2
    for call in posts:
        payload: dict[str, Any] = call.kwargs["json"]
        assert payload["message_thread_id"] == 77
        assert payload["reply_parameters"]["message_id"] == 55
        assert len(payload["text"]) <= _MESSAGE_TEXT_LIMIT


# ----------------------------------------------------------------- Discord


def _discord_channel() -> DiscordChannel:
    return DiscordChannel(
        config=DiscordChannelConfig(token="bot-test", default_channel_id="C123")
    )


def _discord_response(message_id: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"id": message_id},
        request=httpx.Request("POST", "https://discord.com/api/v10/channels/C123/messages"),
    )


def _recording_discord_post(bodies: list[dict[str, Any]]) -> Any:
    """Return a ``client.post`` stub that records the JSON payload per attempt.

    The payload dict is mutated between chunked posts, so it must be snapshotted
    at call time (AsyncMock's await_args_list would alias the final mutation).
    """

    async def _post(url: str, **kwargs: Any) -> httpx.Response:
        bodies.append(dict(kwargs["json"]))
        return _discord_response(f"m{len(bodies)}")

    return _post


@pytest.mark.asyncio
async def test_discord_send_chunks_oversized_final_reply() -> None:
    channel = _discord_channel()
    client = AsyncMock()
    bodies: list[dict[str, Any]] = []
    client.post = _recording_discord_post(bodies)
    channel._client = client
    long_content = "x" * 4500  # exceeds Discord's 2000-char cap

    result = await channel.send(OutgoingMessage(content=long_content))

    assert result.status.value == "sent"
    assert len(bodies) == 3
    contents = [body["content"] for body in bodies]
    assert all(len(content) <= 2000 for content in contents)
    assert "".join(contents) == long_content


@pytest.mark.asyncio
async def test_discord_send_prefers_line_boundaries() -> None:
    channel = _discord_channel()
    client = AsyncMock()
    bodies: list[dict[str, Any]] = []
    client.post = _recording_discord_post(bodies)
    channel._client = client
    content = "A" * 1500 + "\n" + "B" * 1500

    await channel.send(OutgoingMessage(content=content))

    contents = [body["content"] for body in bodies]
    assert len(contents) == 2
    assert contents[0].endswith("\n")  # cut on the newline, not mid-word
    assert contents[0] + contents[1] == content


@pytest.mark.asyncio
async def test_discord_send_short_reply_stays_one_message() -> None:
    channel = _discord_channel()
    client = AsyncMock()
    bodies: list[dict[str, Any]] = []
    client.post = _recording_discord_post(bodies)
    channel._client = client

    await channel.send(OutgoingMessage(content="hello"))

    assert len(bodies) == 1
    assert bodies[0]["content"] == "hello"
