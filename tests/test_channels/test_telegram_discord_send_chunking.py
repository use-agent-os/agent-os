"""Issue #1544: send() had no message-length chunking on Telegram or Discord.

TelegramChannel.send() posted the full payload with no length check at all,
and DiscordChannel.send() assigned payload["content"] = message.content
directly. Either the platform API call fails or the message is
truncated/rejected server-side, and the whole final reply -- the answer the
user is actually waiting for -- is lost. Reachable whenever the stream
policy is final_only or streaming is off.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from agentos.channels._util import split_text_for_limit
from agentos.channels.discord import DiscordChannel, DiscordChannelConfig
from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig
from agentos.channels.types import OutgoingMessage


class _TelegramResponse:
    def __init__(self, message_id: int) -> None:
        self._message_id = message_id

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"ok": True, "result": {"message_id": self._message_id}}


def _telegram_channel() -> tuple[TelegramChannel, AsyncMock]:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    client = AsyncMock()
    calls: list[dict[str, Any]] = []

    async def _post(url: str, **kwargs: Any) -> _TelegramResponse:
        payload = kwargs.get("json", {})
        calls.append(payload)
        return _TelegramResponse(len(calls))

    client.post = _post
    channel._client = client
    channel._owns_client = False
    return channel, calls  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_telegram_send_chunks_a_reply_longer_than_the_limit() -> None:
    channel, calls = _telegram_channel()
    long_content = "x" * 5000

    await channel.send(OutgoingMessage(content=long_content, reply_to="123"))

    assert len(calls) > 1
    for call in calls:
        assert len(call["text"]) <= 4096
    joined = "".join(str(call["text"]) for call in calls)
    assert joined == long_content


@pytest.mark.asyncio
async def test_telegram_send_a_short_reply_is_still_a_single_call() -> None:
    channel, calls = _telegram_channel()

    await channel.send(OutgoingMessage(content="hello", reply_to="123"))

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_telegram_send_reply_parameters_only_attach_to_the_first_chunk() -> None:
    channel, calls = _telegram_channel()
    long_content = "x" * 5000

    await channel.send(
        OutgoingMessage(
            content=long_content,
            reply_to="123",
            metadata={"reply_to_message_id": 42},
        )
    )

    assert len(calls) > 1
    assert "reply_parameters" in calls[0]
    assert all("reply_parameters" not in call for call in calls[1:])


@pytest.mark.asyncio
async def test_telegram_send_does_not_break_a_fenced_code_block_across_chunks() -> None:
    channel, calls = _telegram_channel()
    fenced = "intro " + ("a" * 4000) + "\n```python\ncode " + ("b" * 200) + "\n```\n" + ("c" * 200)

    await channel.send(OutgoingMessage(content=fenced, reply_to="123"))

    assert len(calls) > 1
    for call in calls:
        assert str(call["text"]).count("```") % 2 == 0


def test_split_for_limit_keeps_a_fenced_code_block_whole() -> None:
    fenced = "intro " + ("a" * 4000) + "\n```python\ncode\n```\n" + ("c" * 200)
    head, tail = TelegramChannel._split_for_limit(fenced)
    assert head.count("```") % 2 == 0
    assert tail.count("```") % 2 == 0
    assert head + tail == fenced


def test_split_text_for_limit_respects_a_raw_length_measure() -> None:
    """The shared splitter's default `len` measure is what a plain-text
    channel like Discord needs -- no render step to look through."""
    content = "y" * 3000
    head, tail = split_text_for_limit(content, 2000)
    assert len(head) <= 2000
    assert head + tail == content


def _split_all(content: str, limit: int) -> list[str]:
    """Drive the splitter the way the adapters do: loop until the tail is empty."""
    chunks: list[str] = []
    remaining = content
    for _ in range(100):  # bounded, so a non-advancing split fails instead of hanging
        head, tail = split_text_for_limit(remaining, limit)
        chunks.append(head)
        if not tail:
            return chunks
        remaining = tail
    raise AssertionError("splitter never consumed the segment")


def test_split_text_for_limit_balances_a_fence_that_opens_the_segment() -> None:
    """A fence at offset 0 has no preceding line to back up to.

    The backup was skipped whenever it resolved to 0, so the first chunk
    went out with a half-open block. Cutting at 0 instead would emit an
    empty chunk and hang the adapters' loop, so the block is closed here
    and reopened on the remainder.
    """
    segment = "```python\n" + ("a" * 400) + "\n```\nrest"

    head, tail = split_text_for_limit(segment, 100)

    assert head
    assert head.count("```") % 2 == 0
    assert len(head) <= 100
    assert tail.startswith("```python")


def test_split_text_for_limit_cuts_before_a_fence_on_the_first_line() -> None:
    """Text preceding the fence is a valid, non-empty place to cut."""
    segment = "intro text ```" + ("a" * 400) + "```\nrest"

    head, tail = split_text_for_limit(segment, 100)

    assert head == "intro text "
    assert head.count("```") % 2 == 0
    assert head + tail == segment  # nothing invented when a real cut exists


def test_split_text_for_limit_always_advances_on_fenced_content() -> None:
    """Every chunk balanced, and the loop terminates."""
    segment = "```python\n" + ("a" * 900) + "\n```\ntail text"

    chunks = _split_all(segment, 120)

    assert all(chunk.count("```") % 2 == 0 for chunk in chunks)
    assert all(chunks)


class _DiscordResponse:
    status_code = 200

    def __init__(self, message_id: str) -> None:
        self._message_id = message_id

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"id": self._message_id}


def _discord_channel() -> tuple[DiscordChannel, list[dict[str, Any]]]:
    channel = DiscordChannel(DiscordChannelConfig(token="token", application_id="app"))
    client = AsyncMock()
    calls: list[dict[str, Any]] = []

    async def _post(url: str, **kwargs: Any) -> _DiscordResponse:
        calls.append(kwargs.get("json", {}))
        return _DiscordResponse(str(len(calls)))

    async def _patch(url: str, **kwargs: Any) -> _DiscordResponse:
        calls.append(kwargs.get("json", {}))
        return _DiscordResponse(str(len(calls)))

    client.post = _post
    client.patch = _patch
    channel._client = client
    return channel, calls


@pytest.mark.asyncio
async def test_discord_send_chunks_a_reply_longer_than_the_limit() -> None:
    channel, calls = _discord_channel()
    long_content = "x" * 5000

    await channel.send(OutgoingMessage(content=long_content, reply_to="channel-1"))

    assert len(calls) > 1
    for call in calls:
        assert len(call["content"]) <= 2000
    joined = "".join(str(call["content"]) for call in calls)
    assert joined == long_content


@pytest.mark.asyncio
async def test_discord_send_a_short_reply_is_still_a_single_call() -> None:
    channel, calls = _discord_channel()

    await channel.send(OutgoingMessage(content="hello", reply_to="channel-1"))

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_discord_send_embeds_only_attach_to_the_last_chunk() -> None:
    channel, calls = _discord_channel()
    long_content = "x" * 5000

    await channel.send(
        OutgoingMessage(
            content=long_content,
            reply_to="channel-1",
            metadata={"embeds": [{"title": "t"}], "reply_to_message_id": "99"},
        )
    )

    assert len(calls) > 1
    assert "message_reference" in calls[0]
    assert all("message_reference" not in call for call in calls[1:])
    assert all("embeds" not in call for call in calls[:-1])
    assert calls[-1]["embeds"] == [{"title": "t"}]


@pytest.mark.asyncio
async def test_discord_interaction_response_overflow_goes_to_channel_followups() -> None:
    """The interaction original-response slot holds exactly one message;
    overflow chunks must still reach the user as regular channel messages
    rather than being silently dropped."""
    channel, calls = _discord_channel()
    long_content = "x" * 5000

    await channel.send(
        OutgoingMessage(
            content=long_content,
            reply_to="channel-1",
            metadata={
                "interaction_token": "tok",
                "interaction_application_id": "app",
                "interaction_id": "int-1",
            },
        )
    )

    assert len(calls) > 1
    joined = "".join(str(call["content"]) for call in calls)
    assert joined == long_content
