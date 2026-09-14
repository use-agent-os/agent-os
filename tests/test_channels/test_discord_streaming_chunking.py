"""DiscordChannel.send_streaming had no message-length chunking.

_post/_edit PATCHed the raw StreamThrottle-accumulated text straight to
Discord's API with no check against Discord's 2000-character message cap
(_DISCORD_MESSAGE_TEXT_LIMIT). Once a streamed reply's accumulated text
crossed that cap, the next POST/PATCH got a 400 from Discord and
raise_for_status() killed the in-flight stream -- unlike DiscordChannel.send()
(chunked since #1544/#1659) and TelegramChannel.send_streaming (already
chunks via _post_segments).

Confirmed safe to roll over into a new message mid-stream because the
chunks send_streaming receives are append-only deltas (TextDeltaEvent, fed
through gateway/channel_dispatch.py) -- already-sent text is never revised
-- and because neither caller of send_streaming
(_RuntimeChannelStreamRelay._run, the direct-streaming consumer in
channel_dispatch.py) reads the id this method returns.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agentos.channels.discord import DiscordChannel, DiscordChannelConfig


class _DiscordResponse:
    status_code = 200

    def __init__(self, message_id: str) -> None:
        self._message_id = message_id

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"id": self._message_id}


def _streaming_channel() -> tuple[DiscordChannel, list[tuple[str, str, dict[str, Any]]]]:
    """A DiscordChannel whose client records (method, url, json) per call.

    POST creates a new message and mints a fresh id, matching Discord: PATCH
    edits an existing one and echoes back its id unchanged -- @original's
    real underlying id ("original-msg-id") is only discoverable this way,
    the same as DiscordChannel.send()'s interaction path already relies on.
    """
    channel = DiscordChannel(DiscordChannelConfig(token="token", application_id="app"))
    client = AsyncMock()
    calls: list[tuple[str, str, dict[str, Any]]] = []
    created = 0

    async def _post(url: str, **kwargs: Any) -> _DiscordResponse:
        nonlocal created
        calls.append(("POST", url, kwargs.get("json", {})))
        created += 1
        return _DiscordResponse(f"msg-{created}")

    async def _patch(url: str, **kwargs: Any) -> _DiscordResponse:
        calls.append(("PATCH", url, kwargs.get("json", {})))
        if url.endswith("/@original"):
            return _DiscordResponse("original-msg-id")
        return _DiscordResponse(url.rsplit("/", 1)[-1])

    client.post = _post
    client.patch = _patch
    channel._client = client
    return channel, calls


async def _one_chunk(text: str) -> AsyncIterator[str]:
    yield text


async def _many_chunks(*pieces: str) -> AsyncIterator[str]:
    for piece in pieces:
        yield piece


@pytest.mark.asyncio
async def test_send_streaming_rolls_a_long_first_flush_into_multiple_messages() -> None:
    """Real artifact: inspect the actual sequence of POST calls, not just
    what split_text_for_limit would return in isolation."""
    channel, calls = _streaming_channel()
    long_content = "x" * 5000

    result = await channel.send_streaming(_one_chunk(long_content), channel_id="chan-1")

    assert len(calls) > 1
    for method, _url, payload in calls:
        assert method == "POST"
        assert len(payload["content"]) <= 2000
    joined = "".join(str(payload["content"]) for _method, _url, payload in calls)
    assert joined == long_content
    assert result == "msg-3"  # last of the 3 messages a 5000-char reply needs


@pytest.mark.asyncio
async def test_send_streaming_short_reply_is_still_a_single_call() -> None:
    channel, calls = _streaming_channel()

    await channel.send_streaming(_one_chunk("hello"), channel_id="chan-1")

    assert len(calls) == 1
    assert calls[0][2]["content"] == "hello"


@pytest.mark.parametrize(("length", "expected_calls"), [(2000, 1), (2001, 2)])
@pytest.mark.asyncio
async def test_send_streaming_boundary_at_exactly_the_cap(length: int, expected_calls: int) -> None:
    """2000 chars fits in one message; one more must roll into a second."""
    channel, calls = _streaming_channel()
    content = "y" * length

    await channel.send_streaming(_one_chunk(content), channel_id="chan-1")

    assert len(calls) == expected_calls
    joined = "".join(str(payload["content"]) for _method, _url, payload in calls)
    assert joined == content


@pytest.mark.asyncio
async def test_send_streaming_edit_rolls_over_when_incremental_growth_crosses_the_cap() -> None:
    """Real artifact, incremental path: growth crosses the cap across
    several small edits (StreamThrottle._edit), not one oversized first
    flush. The first open message must stop receiving edits once it is full,
    and later edits must target the new (rolled-over) message id, never the
    frozen one.

    update_interval_ms=0 makes every chunk its own flush (StreamThrottle's
    interval check always passes), so each of the 3 pieces below drives one
    post/edit call: open message 1 with 1500 "a"s; growing to 1500 "a"s +
    500 "b"s (2000) is still one PATCH on message 1; the remaining 200 "b"s
    don't fit, so message 1 is frozen at 2000 and a new message 2 opens with
    them; the final 100 "c"s PATCH message 2 to 300 chars.
    """
    channel, calls = _streaming_channel()
    pieces = ["a" * 1500, "b" * 700, "c" * 100]

    await channel.send_streaming(_many_chunks(*pieces), channel_id="chan-1", update_interval_ms=0)

    assert calls == [
        ("POST", "/channels/chan-1/messages", {"content": "a" * 1500}),
        ("PATCH", "/channels/chan-1/messages/msg-1", {"content": "a" * 1500 + "b" * 500}),
        ("POST", "/channels/chan-1/messages", {"content": "b" * 200}),
        ("PATCH", "/channels/chan-1/messages/msg-2", {"content": "b" * 200 + "c" * 100}),
    ]


@pytest.mark.asyncio
async def test_send_streaming_interaction_overflow_goes_to_regular_channel_messages() -> None:
    """Discord's original-response slot holds exactly one message. Overflow
    must reach the user as regular channel messages -- the same overflow
    handling send() already uses -- not a webhook followup endpoint."""
    channel, calls = _streaming_channel()
    long_content = "x" * 5000

    await channel.send_streaming(
        _one_chunk(long_content),
        channel_id="chan-1",
        interaction_token="tok",
        interaction_application_id="app",
        interaction_id="int-1",
    )

    assert len(calls) > 1
    first_method, first_url, first_payload = calls[0]
    assert first_method == "PATCH"
    assert first_url == "/webhooks/app/tok/messages/@original"
    for method, url, _payload in calls[1:]:
        assert method == "POST"
        assert url == "/channels/chan-1/messages"
    joined = "".join(str(payload["content"]) for _method, _url, payload in calls)
    assert joined == long_content


@pytest.mark.asyncio
async def test_send_streaming_interaction_short_reply_only_edits_original() -> None:
    """Regression pin for #2076's sibling contract: editing @original is the
    only way to learn its real message id, so send_streaming must capture it
    from the edit response instead of returning None (matching what
    test_discord_streaming_reply_completes_original_interaction_response in
    test_discord_interactions.py already pins for the single-chunk case)."""
    channel, calls = _streaming_channel()

    result = await channel.send_streaming(
        _one_chunk("hello"),
        channel_id="chan-1",
        interaction_token="tok",
        interaction_application_id="app",
    )

    assert len(calls) == 1
    assert calls[0] == ("PATCH", "/webhooks/app/tok/messages/@original", {"content": "hello"})
    assert result == "original-msg-id"


@pytest.mark.asyncio
async def test_send_streaming_empty_stream_returns_none() -> None:
    channel, calls = _streaming_channel()

    result = await channel.send_streaming(_many_chunks(), channel_id="chan-1")

    assert calls == []
    assert result is None
