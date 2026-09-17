"""Issue #2105: ``DiscordChannel.send_streaming`` had no 2000-char chunking.

``send()`` has split at ``_DISCORD_MESSAGE_TEXT_LIMIT`` since #1544, but the
streaming path PATCHed the whole accumulated text on every flush. Once the
reply crossed 2000 characters Discord answered 400 and ``raise_for_status``
killed the stream. Overflow now rolls into a new message, the way
``TelegramChannel.send_streaming`` does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from agentos.channels.discord import (
    _DISCORD_MESSAGE_TEXT_LIMIT,
    DiscordChannel,
    DiscordChannelConfig,
)

_ORIGINAL = "/webhooks/app-1/token-1/messages/@original"


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _Client:
    """Records every call and hands out sequential message ids for POSTs."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._next_id = 0

    async def post(self, path: str, **kwargs: Any) -> _Response:
        self._next_id += 1
        message_id = f"msg-{self._next_id}"
        self.calls.append(("POST", path, kwargs["json"]["content"]))
        return _Response({"id": message_id})

    async def patch(self, path: str, **kwargs: Any) -> _Response:
        self.calls.append(("PATCH", path, kwargs["json"]["content"]))
        return _Response({"id": "original"})


def _channel() -> tuple[DiscordChannel, _Client]:
    channel = DiscordChannel(DiscordChannelConfig(token="t", application_id="app-1"))
    client = _Client()
    channel._client = client  # type: ignore[assignment]
    return channel, client


async def _words(count: int, *, per_chunk: int = 40) -> AsyncIterator[str]:
    # 40 words × ~9 chars; every chunk is flushed because the interval is 0.
    words = [f"word{i:04d} " for i in range(count)]
    for start in range(0, count, per_chunk):
        yield "".join(words[start : start + per_chunk])


def _final_text_per_message(calls: list[tuple[str, str, str]]) -> dict[str, str]:
    """The last content each distinct message path ended up holding."""
    latest: dict[str, str] = {}
    for method, path, content in calls:
        key = path if method == "PATCH" else f"{path}#{len(latest)}"
        latest[key] = content
    return latest


@pytest.mark.asyncio
async def test_streamed_channel_reply_rolls_over_at_the_limit() -> None:
    channel, client = _channel()
    full_text = "".join([c async for c in _words(600)])
    assert len(full_text) > 2 * _DISCORD_MESSAGE_TEXT_LIMIT

    message_id = await channel.send_streaming(
        _words(600), channel_id="chan-1", update_interval_ms=0
    )

    for _method, _path, content in client.calls:
        assert len(content) <= _DISCORD_MESSAGE_TEXT_LIMIT
    posts = [c for c in client.calls if c[0] == "POST"]
    assert len(posts) == 3
    assert all(path == "/channels/chan-1/messages" for _m, path, _c in posts)
    # Edits go to the message currently being filled, never a frozen one.
    edited_paths = [path for method, path, _c in client.calls if method == "PATCH"]
    assert set(edited_paths) <= {
        "/channels/chan-1/messages/msg-1",
        "/channels/chan-1/messages/msg-2",
        "/channels/chan-1/messages/msg-3",
    }
    assert message_id == "msg-3"

    # What the user sees, message by message, reassembles the whole reply.
    seen: list[str] = []
    for method, path, content in client.calls:
        if method == "POST":
            seen.append(content)
        else:
            seen[-1] = content
    assert "".join(seen) == full_text


@pytest.mark.asyncio
async def test_streamed_interaction_reply_overflows_into_channel_messages() -> None:
    # The @original slot holds exactly one message; the overflow goes to the
    # channel as follow-ups, matching what ``send()`` does for interactions.
    channel, client = _channel()
    full_text = "".join([c async for c in _words(300)])
    assert _DISCORD_MESSAGE_TEXT_LIMIT < len(full_text) <= 2 * _DISCORD_MESSAGE_TEXT_LIMIT

    message_id = await channel.send_streaming(
        _words(300),
        channel_id="chan-1",
        interaction_id="i-1",
        interaction_token="token-1",
        interaction_application_id="app-1",
        update_interval_ms=0,
    )

    for _method, _path, content in client.calls:
        assert len(content) <= _DISCORD_MESSAGE_TEXT_LIMIT
    assert client.calls[0][:2] == ("PATCH", _ORIGINAL)
    posts = [c for c in client.calls if c[0] == "POST"]
    assert [path for _m, path, _c in posts] == ["/channels/chan-1/messages"]
    original_final = [c for m, p, c in client.calls if p == _ORIGINAL][-1]
    channel_final = [c for m, p, c in client.calls if p != _ORIGINAL][-1]
    assert original_final + channel_final == full_text
    # Once rolled over, the frozen @original is never touched again.
    first_post = next(i for i, c in enumerate(client.calls) if c[0] == "POST")
    assert all(p != _ORIGINAL for _m, p, _c in client.calls[first_post:])
    assert message_id == "msg-1"


@pytest.mark.asyncio
async def test_short_streamed_reply_is_still_one_message() -> None:
    channel, client = _channel()

    async def _chunks() -> AsyncIterator[str]:
        yield "hello "
        yield "world"

    message_id = await channel.send_streaming(_chunks(), channel_id="chan-1", update_interval_ms=0)

    assert [c[0] for c in client.calls] == ["POST", "PATCH", "PATCH"]
    assert client.calls[-1][2] == "hello world"
    assert message_id == "msg-1"
