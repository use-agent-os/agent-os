"""Issue #2876: ``MSTeamsChannel.send_streaming()`` message-length chunking.

Teams rejects an Activity past the 40 KB payload cap with 413 MessageSizeTooBig.
During streaming, oversized content is split per ``_MSTEAMS_MESSAGE_TEXT_LIMIT``
with segment rollover across activities mid-stream and on final flush.
"""

from __future__ import annotations

import sys
import types
from collections.abc import AsyncIterator, Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentos.channels.msteams import (
    _MSTEAMS_ACTIVITY_PAYLOAD_LIMIT,
    _MSTEAMS_MESSAGE_TEXT_LIMIT,
    MSTeamsChannel,
    MSTeamsChannelConfig,
    _measure_activity_text,
)


@pytest.fixture(autouse=True)
def _stub_botbuilder(monkeypatch: pytest.MonkeyPatch) -> None:
    package = types.ModuleType("botbuilder")
    schema = types.ModuleType("botbuilder.schema")
    schema.Activity = lambda **kw: types.SimpleNamespace(**kw)
    monkeypatch.setitem(sys.modules, "botbuilder", package)
    monkeypatch.setitem(sys.modules, "botbuilder.schema", schema)


class _StreamRecorder:
    def __init__(self, supports_edits: bool = True) -> None:
        self.sent: list[str] = []
        self.sent_ids: list[str] = []
        self.updated: list[tuple[str, str]] = []  # (activity_id, text)
        self.supports_edits = supports_edits
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

        async def _update_activity(activity: object) -> None:
            if not self.supports_edits:
                raise RuntimeError("update_activity is not supported by this channel")
            self.updated.append((activity.id, activity.text))

        turn_context.send_activity = AsyncMock(side_effect=_send_activity)
        turn_context.update_activity = AsyncMock(side_effect=_update_activity)
        await callback(turn_context)


def _streaming_channel(
    edit_interval_s: float = 2.0, supports_edits: bool = True
) -> tuple[MSTeamsChannel, _StreamRecorder]:
    config = MSTeamsChannelConfig(name="msteams", edit_interval_s=edit_interval_s)
    channel = MSTeamsChannel(config=config)
    channel._references = {"conversation-A": "REF_FOR_A"}
    recorder = _StreamRecorder(supports_edits=supports_edits)
    channel._adapter = MagicMock()
    channel._adapter.continue_conversation = AsyncMock(side_effect=recorder.continue_conversation)
    return channel, recorder


async def _stream(*chunks: str) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


@pytest.mark.asyncio
async def test_streaming_short_reply_is_single_activity() -> None:
    channel, recorder = _streaming_channel()

    res = await channel.send_streaming(_stream("hello ", "world"), reply_to="conversation-A")

    assert res is not None
    assert len(recorder.sent) == 1
    assert recorder.sent[0] == "hello "
    assert len(recorder.updated) == 1
    assert recorder.updated[0][1] == "hello world"


@pytest.mark.asyncio
async def test_streaming_final_flush_splits_oversized_reply() -> None:
    """With default throttling, content accumulates and splits cleanly on final flush."""
    channel, recorder = _streaming_channel(edit_interval_s=2.0)

    part = "X" * 20000
    long_content = "start: " + part + part  # ~40 KB
    assert _measure_activity_text(long_content) > _MSTEAMS_ACTIVITY_PAYLOAD_LIMIT

    res = await channel.send_streaming(_stream("start: ", part, part), reply_to="conversation-A")

    assert res is not None
    assert len(recorder.sent) > 1
    for piece in recorder.sent:
        assert _measure_activity_text(piece) <= _MSTEAMS_MESSAGE_TEXT_LIMIT
    for _aid, piece in recorder.updated:
        assert _measure_activity_text(piece) <= _MSTEAMS_MESSAGE_TEXT_LIMIT

    # Reconstruct text across the update and rollover sends
    last_text = dict(zip(recorder.sent_ids, recorder.sent, strict=True))
    for aid, txt in recorder.updated:
        last_text[aid] = txt
    reconstructed = "".join(last_text[aid] for aid in recorder.sent_ids)
    assert reconstructed == long_content


@pytest.mark.asyncio
async def test_streaming_mid_stream_rollover() -> None:
    """When throttling is 0, mid-stream edits freeze at capacity and rollover to new activity."""
    channel, recorder = _streaming_channel(edit_interval_s=0.0)

    chunk = "Y" * 7000
    chunks = [chunk] * 6  # 42,000 chars

    res = await channel.send_streaming(_stream(*chunks), reply_to="conversation-A")

    assert res is not None
    assert len(recorder.sent) > 1
    for piece in recorder.sent:
        assert _measure_activity_text(piece) <= _MSTEAMS_MESSAGE_TEXT_LIMIT
    for _aid, piece in recorder.updated:
        assert _measure_activity_text(piece) <= _MSTEAMS_MESSAGE_TEXT_LIMIT

    last_text = dict(zip(recorder.sent_ids, recorder.sent, strict=True))
    for aid, txt in recorder.updated:
        last_text[aid] = txt
    reconstructed = "".join(last_text[aid] for aid in recorder.sent_ids)
    assert reconstructed == chunk * 6


@pytest.mark.asyncio
async def test_streaming_huge_initial_chunk_splits_immediately() -> None:
    channel, recorder = _streaming_channel()

    huge_chunk = "Z" * 50000
    res = await channel.send_streaming(_stream(huge_chunk), reply_to="conversation-A")

    assert res is not None
    assert len(recorder.sent) > 1
    for piece in recorder.sent:
        assert _measure_activity_text(piece) <= _MSTEAMS_MESSAGE_TEXT_LIMIT
    assert "".join(recorder.sent) == huge_chunk


@pytest.mark.asyncio
async def test_streaming_unsupported_edits_fallback_chunks_properly() -> None:
    channel, recorder = _streaming_channel(supports_edits=False)

    part = "W" * 25000
    res = await channel.send_streaming(_stream(part, part), reply_to="conversation-A")

    assert res is not None
    assert len(recorder.sent) > 1
    for piece in recorder.sent:
        assert _measure_activity_text(piece) <= _MSTEAMS_MESSAGE_TEXT_LIMIT


@pytest.mark.asyncio
async def test_streaming_tracks_all_chunk_ids_for_edit_delete() -> None:
    channel, recorder = _streaming_channel(edit_interval_s=0.0)

    chunk = "M" * 20000
    await channel.send_streaming(_stream(chunk, chunk), reply_to="conversation-A")

    assert len(recorder.sent_ids) > 1
    for sid in recorder.sent_ids:
        assert channel._message_conversation_keys[sid] == "conversation-A"


@pytest.mark.asyncio
async def test_streaming_emoji_measured_in_utf16_units() -> None:
    channel, recorder = _streaming_channel(edit_interval_s=0.0)

    # Astral plane emoji: 2 UTF-16 units each
    emoji_chunk = "\U0001f600" * 10000  # 20,000 UTF-16 code units
    await channel.send_streaming(_stream(emoji_chunk, emoji_chunk), reply_to="conversation-A")

    for piece in recorder.sent:
        assert _measure_activity_text(piece) <= _MSTEAMS_MESSAGE_TEXT_LIMIT
    for _aid, piece in recorder.updated:
        assert _measure_activity_text(piece) <= _MSTEAMS_MESSAGE_TEXT_LIMIT
