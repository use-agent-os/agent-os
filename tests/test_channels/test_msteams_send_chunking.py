"""MSTeamsChannel.send() had no message-length chunking, unlike Telegram/Discord.

Issue #1544 fixed the same gap for Telegram's and Discord's ``send()`` --
neither capped anything before posting, so a final reply longer than the
platform's cap either failed the API call or was truncated/rejected
server-side. MSTeamsChannel.send() called ``turn_context.send_activity()``
with the raw ``message.content`` and had the identical gap: Teams' Activity
payload is capped at 40 KB (UTF-16), and an oversized one fails with a 413
MessageSizeTooBig instead of delivering anything at all.
"""

from __future__ import annotations

import sys
import types
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentos.channels.msteams import (
    _MSTEAMS_TEXT_LIMIT,
    MSTeamsChannel,
    MSTeamsChannelConfig,
    _utf16_units,
)
from agentos.channels.types import OutgoingMessage


@pytest.fixture(autouse=True)
def _stub_botbuilder_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Activity:
        def __init__(self, **fields: Any) -> None:
            self.__dict__.update(fields)

    schema_module = types.ModuleType("botbuilder.schema")
    schema_module.Activity = _Activity  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "botbuilder.schema", schema_module)


def _channel() -> tuple[MSTeamsChannel, list[str]]:
    channel = MSTeamsChannel(config=MSTeamsChannelConfig(name="msteams"))
    channel._references = {"conversation-A": "REF_FOR_A"}
    channel._adapter = MagicMock()
    sent: list[str] = []

    async def _continue_conversation(
        reference: object, callback: Callable[[object], Awaitable[None]], **_: object
    ) -> None:
        turn_context = MagicMock()

        async def _send_activity(text: str) -> MagicMock:
            sent.append(text)
            return MagicMock(id=f"activity-{len(sent)}")

        turn_context.send_activity = AsyncMock(side_effect=_send_activity)
        await callback(turn_context)

    channel._adapter.continue_conversation = AsyncMock(side_effect=_continue_conversation)
    return channel, sent


@pytest.mark.asyncio
async def test_send_chunks_a_reply_longer_than_the_limit() -> None:
    channel, sent = _channel()
    long_content = "x" * (_MSTEAMS_TEXT_LIMIT * 2 + 500)

    await channel.send(OutgoingMessage(content=long_content, reply_to="conversation-A"))

    assert len(sent) > 1
    for chunk in sent:
        assert len(chunk) <= _MSTEAMS_TEXT_LIMIT
    assert "".join(sent) == long_content


@pytest.mark.asyncio
async def test_send_of_a_short_reply_is_a_single_activity() -> None:
    channel, sent = _channel()

    await channel.send(OutgoingMessage(content="hello world", reply_to="conversation-A"))

    assert sent == ["hello world"]


@pytest.mark.asyncio
async def test_send_measures_emoji_in_utf16_units_not_characters() -> None:
    """An emoji is one character but two UTF-16 units -- Teams' actual cap.

    18,000 emoji is 18,000 characters (at the _MSTEAMS_TEXT_LIMIT boundary
    under a character count) but 36,000 UTF-16 units -- double the limit.
    A character-counted split would leave this as a single, oversized
    activity that Teams would reject with 413.
    """
    channel, sent = _channel()
    emoji_content = "\U0001f600" * 18_000

    await channel.send(OutgoingMessage(content=emoji_content, reply_to="conversation-A"))

    assert len(sent) > 1
    for chunk in sent:
        assert _utf16_units(chunk) <= _MSTEAMS_TEXT_LIMIT
    assert "".join(sent) == emoji_content


@pytest.mark.asyncio
async def test_send_never_splits_inside_a_surrogate_pair() -> None:
    """Counting in UTF-16 units means a cut can land between the high and
    low surrogate of one astral character; the splitter must not do that,
    or a chunk boundary would produce invalid UTF-16 on the wire."""
    channel, sent = _channel()
    # Pad to land a naive unit-boundary cut exactly inside an emoji's
    # surrogate pair, then verify every emitted chunk is still valid text.
    content = ("a" * (_MSTEAMS_TEXT_LIMIT - 1)) + ("\U0001f600" * 10)

    await channel.send(OutgoingMessage(content=content, reply_to="conversation-A"))

    assert len(sent) > 1
    for chunk in sent:
        chunk.encode("utf-16-le")  # raises if a surrogate is unpaired
        assert _utf16_units(chunk) <= _MSTEAMS_TEXT_LIMIT
    assert "".join(sent) == content


@pytest.mark.asyncio
async def test_send_tracks_the_last_chunk_for_edit_delete() -> None:
    """Matches Discord's "track the last chunk" convention: a later
    edit()/delete() by the returned id must act on the final message."""
    channel, sent = _channel()
    long_content = "y" * (_MSTEAMS_TEXT_LIMIT * 2 + 10)

    await channel.send(OutgoingMessage(content=long_content, reply_to="conversation-A"))

    last_id = f"activity-{len(sent)}"
    assert channel._message_conversation_keys[last_id] == "conversation-A"


# ---------------------------------------------------------------------------
# send_streaming(): same gap, in the streaming path (mid-stream edits and
# the final flush both posted the full accumulated text with no cap).
# ---------------------------------------------------------------------------


class _StreamRecorder:
    """Records every send_activity/update_activity call, keyed by activity id."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.sent_ids: list[str] = []
        self.updated: list[tuple[str, str]] = []  # (activity_id, text)
        self._next_id = 0

    def _fresh_id(self) -> str:
        self._next_id += 1
        return f"activity-{self._next_id}"

    async def continue_conversation(
        self, reference: object, callback: Callable[[object], Awaitable[None]], **_: object
    ) -> None:
        turn_context = MagicMock()

        async def _send_activity(text: str) -> MagicMock:
            self.sent.append(text)
            new_id = self._fresh_id()
            self.sent_ids.append(new_id)
            return MagicMock(id=new_id)

        async def _update_activity(activity: Any) -> None:
            self.updated.append((activity.id, activity.text))

        turn_context.send_activity = AsyncMock(side_effect=_send_activity)
        turn_context.update_activity = AsyncMock(side_effect=_update_activity)
        await callback(turn_context)


def _streaming_channel(edit_interval_s: float = 2.0) -> tuple[MSTeamsChannel, _StreamRecorder]:
    config = MSTeamsChannelConfig(name="msteams", edit_interval_s=edit_interval_s)
    channel = MSTeamsChannel(config=config)
    channel._references = {"conversation-A": "REF_FOR_A"}
    recorder = _StreamRecorder()
    channel._adapter = MagicMock()
    channel._adapter.continue_conversation = AsyncMock(side_effect=recorder.continue_conversation)
    return channel, recorder


async def _stream(*chunks: str) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


@pytest.mark.asyncio
async def test_streaming_final_flush_rolls_overflow_into_a_new_activity() -> None:
    """Mid-stream edits are throttled off (default 2s interval, instant test
    loop), so the whole reply piles up for one final flush -- which must
    split across activities instead of updating one with an oversized text."""
    channel, recorder = _streaming_channel()

    first = "start "
    rest = "y" * (_MSTEAMS_TEXT_LIMIT * 2)
    await channel.send_streaming(_stream(first, rest), reply_to="conversation-A")

    assert recorder.sent[0] == first
    # The final flush updates the first activity's content up to the cap,
    # then any overflow rolls into fresh activities -- none of them, nor
    # that update, ever carries oversized text.
    assert len(recorder.sent) > 1
    assert len(recorder.updated) == 1
    for chunk in recorder.sent:
        assert len(chunk) <= _MSTEAMS_TEXT_LIMIT
    for _activity_id, text in recorder.updated:
        assert len(text) <= _MSTEAMS_TEXT_LIMIT
    delivered = recorder.updated[0][1] + "".join(recorder.sent[1:])
    assert delivered == first + rest


@pytest.mark.asyncio
async def test_streaming_mid_stream_edit_rolls_overflow_into_a_new_activity() -> None:
    """With throttling disabled, each chunk after the first tries to edit the
    open activity -- once accumulated text for that activity exceeds the cap,
    it must freeze and roll into a new one, not send an oversized edit."""
    channel, recorder = _streaming_channel(edit_interval_s=0.0)

    chunk_size = 4000
    big_chunk = "z" * chunk_size
    # First chunk opens the activity; five more (20000 chars) push it past
    # the 18000-char cap mid-stream.
    await channel.send_streaming(_stream(big_chunk, *([big_chunk] * 5)), reply_to="conversation-A")

    for text in [*recorder.sent, *(t for _id, t in recorder.updated)]:
        assert len(text) <= _MSTEAMS_TEXT_LIMIT
    # A rollover happened: more than the one initial activity was opened.
    assert len(recorder.sent) > 1

    # The *last* known content per activity, concatenated in the order the
    # activities were opened, must reconstruct the full stream exactly --
    # each edit replaces an activity's whole text, so only the latest one
    # per id matters.
    latest: dict[str, str] = dict(zip(recorder.sent_ids, recorder.sent, strict=True))
    for activity_id, text in recorder.updated:
        latest[activity_id] = text
    delivered = "".join(latest[activity_id] for activity_id in recorder.sent_ids)
    assert delivered == big_chunk * 6
