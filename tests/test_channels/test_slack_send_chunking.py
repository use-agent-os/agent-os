"""SlackChannel.send() had no message-length chunking, unlike Telegram/Discord.

Issue #1544 fixed the same gap for Telegram's and Discord's ``send()`` --
neither capped anything before posting, so a final reply longer than the
platform's cap either failed the API call or was truncated/rejected
server-side. SlackChannel.send() posted ``message.content`` straight into
``chat.postMessage``'s ``text`` field with no length check: Slack truncates
(and may split unpredictably) text past 40000 characters.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from agentos.channels.slack import _SLACK_MESSAGE_TEXT_LIMIT, SlackChannel
from agentos.channels.types import OutgoingMessage

_REQUEST = httpx.Request("POST", "https://slack.test/api")


def _channel() -> tuple[SlackChannel, list[dict[str, Any]]]:
    channel = SlackChannel(token="xoxb-test", slack_channel_id="C123")
    channel.bot_user_id = "UBOT"
    calls: list[dict[str, Any]] = []

    async def _post(url: str, **kwargs: Any) -> httpx.Response:
        payload = kwargs.get("json", {})
        calls.append(payload)
        return httpx.Response(200, json={"ok": True, "ts": f"1.{len(calls)}"}, request=_REQUEST)

    client = AsyncMock()
    client.post = _post
    channel._client = client
    return channel, calls


@pytest.mark.asyncio
async def test_send_chunks_a_reply_longer_than_the_limit() -> None:
    channel, calls = _channel()
    long_content = "x" * (_SLACK_MESSAGE_TEXT_LIMIT * 2 + 500)

    await channel.send(OutgoingMessage(content=long_content, reply_to="C123"))

    assert len(calls) > 1
    for call in calls:
        assert len(call["text"]) <= _SLACK_MESSAGE_TEXT_LIMIT
    assert "".join(call["text"] for call in calls) == long_content


@pytest.mark.asyncio
async def test_send_of_a_short_reply_is_a_single_message() -> None:
    channel, calls = _channel()

    await channel.send(OutgoingMessage(content="hello world", reply_to="C123"))

    assert len(calls) == 1
    assert calls[0]["text"] == "hello world"


@pytest.mark.asyncio
async def test_send_chunking_keeps_thread_ts_on_every_chunk() -> None:
    """Every chunk of one logical reply belongs in the same thread -- unlike
    a reply reference, this isn't "point at message N", so it must not be
    first-chunk-only."""
    channel, calls = _channel()
    long_content = "y" * (_SLACK_MESSAGE_TEXT_LIMIT * 2 + 10)

    await channel.send(
        OutgoingMessage(content=long_content, reply_to="1700000000.000100")
    )

    assert len(calls) > 1
    for call in calls:
        assert call["thread_ts"] == "1700000000.000100"


@pytest.mark.asyncio
async def test_send_chunking_puts_extra_metadata_only_on_the_last_chunk() -> None:
    """blocks/attachments describe the complete answer; repeating them on
    every chunk would duplicate them, so only the last chunk carries them."""
    channel, calls = _channel()
    long_content = "z" * (_SLACK_MESSAGE_TEXT_LIMIT * 2 + 10)

    await channel.send(
        OutgoingMessage(
            content=long_content,
            reply_to="C123",
            metadata={"blocks": [{"type": "divider"}]},
        )
    )

    assert len(calls) > 1
    for call in calls[:-1]:
        assert "blocks" not in call
    assert calls[-1]["blocks"] == [{"type": "divider"}]
