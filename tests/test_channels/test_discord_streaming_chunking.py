"""Issue #2105: Discord's streamed reply had no 2000-character chunking.

``DiscordChannel.send()`` chunks (#1544/#1659) and ``TelegramChannel.send_streaming``
rolls overflow into new messages, but Discord's ``send_streaming`` POSTed and
PATCHed the accumulated text verbatim. ``StreamThrottle`` resends the *whole*
accumulated text on every flush, so the moment a streamed reply crosses 2000
characters Discord answers 400, ``raise_for_status()`` raises, and the
in-flight reply dies mid-stream.

Both streaming paths are affected: a regular channel message, and the deferred
interaction response edited at ``/messages/@original``. The interaction path
needs more than a length check -- a second PATCH to ``@original`` would
*replace* the text already shown, so overflow has to become a follow-up
message.
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

APP_ID = "app-1"
TOKEN = "tok-1"


class _Response:
    # `retry_request` inspects status_code before the caller sees the response.
    status_code = 200

    def __init__(self, message_id: str) -> None:
        self._id = message_id

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"id": self._id}


class _RecordingClient:
    """Records every POST/PATCH, and models the messages they produce.

    A POST creates a message; a PATCH replaces the content of an existing one.
    Several messages share the ``/channels/{id}/messages`` path, so the mailbox
    is keyed by message id -- reconstructing by path would fold a message
    together with its own later edits.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []  # (verb, path, content)
        self.messages: list[list[str]] = []  # [message_id, current content]
        self._by_id: dict[str, list[str]] = {}
        self._next = 0

    async def post(self, path: str, **kwargs: Any) -> _Response:
        content = self._content(kwargs)
        self.calls.append(("POST", path, content))
        self._next += 1
        message_id = f"msg-{self._next}"
        record = [message_id, content]
        self.messages.append(record)
        self._by_id[message_id] = record
        return _Response(message_id)

    async def patch(self, path: str, **kwargs: Any) -> _Response:
        content = self._content(kwargs)
        self.calls.append(("PATCH", path, content))
        key = path.rsplit("/", 1)[-1]  # a message id, or "@original"
        record = self._by_id.get(key)
        if record is None:
            # First PATCH to @original opens the interaction's own message.
            record = [key, content]
            self.messages.append(record)
            self._by_id[key] = record
        else:
            record[1] = content
        return _Response(record[0])

    @staticmethod
    def _content(kwargs: dict[str, Any]) -> str:
        return str((kwargs.get("json") or {}).get("content", ""))

    @property
    def contents(self) -> list[str]:
        return [content for _verb, _path, content in self.calls]


def _channel() -> tuple[DiscordChannel, _RecordingClient]:
    channel = DiscordChannel(DiscordChannelConfig(token="t", application_id=APP_ID))
    client = _RecordingClient()
    channel._client = client  # type: ignore[assignment]
    channel._owns_client = False
    return channel, client


async def _stream(*chunks: str) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


def _assert_all_within_cap(contents: list[str]) -> None:
    oversized = [c for c in contents if len(c) > _DISCORD_MESSAGE_TEXT_LIMIT]
    assert not oversized, (
        f"{len(oversized)} request(s) over Discord's {_DISCORD_MESSAGE_TEXT_LIMIT}-char cap; "
        f"largest was {max((len(c) for c in contents), default=0)}"
    )


def _final_text(client: _RecordingClient) -> str:
    """What the reader ends up seeing: every message's final content, in the
    order the messages were opened."""
    return "".join(content for _id, content in client.messages)


# ── regular channel-message streaming ───────────────────────────────────────


@pytest.mark.asyncio
async def test_a_stream_past_the_cap_never_sends_an_oversized_request() -> None:
    """The reported failure: the request that crosses 2000 chars is rejected
    with a 400 and kills the stream."""
    channel, client = _channel()
    piece = "x" * 900

    await channel.send_streaming(
        _stream(piece, piece, piece, piece), channel_id="chan-1", update_interval_ms=0
    )

    assert client.calls
    _assert_all_within_cap(client.contents)


@pytest.mark.asyncio
async def test_the_whole_streamed_reply_is_delivered() -> None:
    """Chunking must not lose the overflow it was added to carry."""
    channel, client = _channel()
    piece = "x" * 900
    expected = piece * 4

    await channel.send_streaming(
        _stream(piece, piece, piece, piece), channel_id="chan-1", update_interval_ms=0
    )

    assert _final_text(client) == expected


@pytest.mark.asyncio
async def test_overflow_opens_a_second_message_rather_than_editing_the_first() -> None:
    channel, client = _channel()
    piece = "x" * 900

    await channel.send_streaming(
        _stream(piece, piece, piece), channel_id="chan-1", update_interval_ms=0
    )

    posts = [c for c in client.calls if c[0] == "POST"]
    assert len(posts) >= 2, "the overflow should be a new message, not an edit"


@pytest.mark.asyncio
async def test_a_short_stream_still_uses_one_message() -> None:
    """Chunking must not turn every ordinary streamed reply into several."""
    channel, client = _channel()

    await channel.send_streaming(
        _stream("hello ", "world"), channel_id="chan-1", update_interval_ms=0
    )

    posts = [c for c in client.calls if c[0] == "POST"]
    assert len(posts) == 1
    assert _final_text(client) == "hello world"


@pytest.mark.asyncio
async def test_an_empty_stream_sends_nothing() -> None:
    channel, client = _channel()

    result = await channel.send_streaming(_stream(), channel_id="chan-1", update_interval_ms=0)

    assert client.calls == []
    assert result is None


@pytest.mark.asyncio
async def test_a_single_first_chunk_larger_than_the_cap_is_split() -> None:
    """The first flush can already be oversized -- a fast producer, or a
    chunk that arrives whole. There is no earlier edit to roll over from."""
    channel, client = _channel()
    huge = "x" * 5000

    await channel.send_streaming(_stream(huge), channel_id="chan-1", update_interval_ms=0)

    _assert_all_within_cap(client.contents)
    assert _final_text(client) == huge


# ── deferred interaction response (@original) streaming ─────────────────────


@pytest.mark.asyncio
async def test_interaction_stream_past_the_cap_stays_within_it() -> None:
    channel, client = _channel()
    piece = "x" * 900

    await channel.send_streaming(
        _stream(piece, piece, piece),
        interaction_token=TOKEN,
        interaction_application_id=APP_ID,
        update_interval_ms=0,
    )

    _assert_all_within_cap(client.contents)


@pytest.mark.asyncio
async def test_interaction_overflow_becomes_a_followup_not_a_second_original_patch() -> None:
    """The bug a length check alone would leave behind.

    ``@original`` is edited in place, so PATCHing it again with the overflow
    would *replace* the first 2000 characters instead of continuing after
    them. Overflow has to be a follow-up message on the same interaction.
    """
    channel, client = _channel()
    piece = "x" * 900

    await channel.send_streaming(
        _stream(piece, piece, piece),
        interaction_token=TOKEN,
        interaction_application_id=APP_ID,
        update_interval_ms=0,
    )

    followup_posts = [
        c for c in client.calls if c[0] == "POST" and c[1] == f"/webhooks/{APP_ID}/{TOKEN}"
    ]
    assert followup_posts, "overflow must open a follow-up message"


@pytest.mark.asyncio
async def test_interaction_stream_delivers_the_whole_reply() -> None:
    channel, client = _channel()
    piece = "x" * 900
    expected = piece * 3

    await channel.send_streaming(
        _stream(piece, piece, piece),
        interaction_token=TOKEN,
        interaction_application_id=APP_ID,
        update_interval_ms=0,
    )

    assert _final_text(client) == expected


@pytest.mark.asyncio
async def test_interaction_edits_after_rollover_target_the_followup() -> None:
    """Once the stream has rolled over, further edits belong to the follow-up.
    An edit still aimed at ``@original`` would overwrite the published head.
    """
    channel, client = _channel()
    piece = "x" * 900

    await channel.send_streaming(
        _stream(piece, piece, piece, piece, piece),
        interaction_token=TOKEN,
        interaction_application_id=APP_ID,
        update_interval_ms=0,
    )

    original_path = f"/webhooks/{APP_ID}/{TOKEN}/messages/@original"
    original_writes = [c for c in client.calls if c[1] == original_path]
    # Whatever @original ends up holding must be a prefix of the reply, and it
    # must never exceed the cap.
    assert original_writes
    assert len(original_writes[-1][2]) <= _DISCORD_MESSAGE_TEXT_LIMIT
    assert (piece * 5).startswith(original_writes[-1][2])


@pytest.mark.asyncio
async def test_a_short_interaction_stream_only_touches_original() -> None:
    channel, client = _channel()

    await channel.send_streaming(
        _stream("hi ", "there"),
        interaction_token=TOKEN,
        interaction_application_id=APP_ID,
        update_interval_ms=0,
    )

    assert all(c[1].endswith("/@original") for c in client.calls)
    assert _final_text(client) == "hi there"


@pytest.mark.asyncio
async def test_a_missing_application_id_is_still_rejected() -> None:
    """Unchanged behaviour, pinned: the interaction path needs an app id, and
    the new follow-up route derives from the same value."""
    channel = DiscordChannel(DiscordChannelConfig(token="t"))
    channel._client = _RecordingClient()  # type: ignore[assignment]
    channel._owns_client = False

    with pytest.raises(ValueError, match="application id"):
        await channel.send_streaming(_stream("hi"), interaction_token=TOKEN)
