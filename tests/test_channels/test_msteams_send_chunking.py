"""Issue #2114: ``MSTeamsChannel.send()`` had no message-length chunking.

Telegram and Discord ``send()`` split at their platform caps since #1544;
Teams passed ``message.content`` to ``send_activity`` as one Activity. Past
the service's 40 KB payload cap that fails with ``413 MessageSizeTooBig`` and
nothing is delivered at all. The reply is now split with the shared
``split_text_for_limit`` and the pieces are sent in order.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentos.channels.msteams import (
    _MSTEAMS_ACTIVITY_PAYLOAD_LIMIT,
    _MSTEAMS_MESSAGE_TEXT_LIMIT,
    MSTeamsChannel,
    MSTeamsChannelConfig,
    _measure_activity_text,
)
from agentos.channels.types import OutgoingMessage


@pytest.fixture()
def botbuilder_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    if "botbuilder.schema" in sys.modules:
        return

    class _Activity:
        def __init__(self, **fields: Any) -> None:
            self.__dict__.update(fields)

    package = ModuleType("botbuilder")
    schema = ModuleType("botbuilder.schema")
    schema.Activity = _Activity  # type: ignore[attr-defined]
    package.schema = schema  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "botbuilder", package)
    monkeypatch.setitem(sys.modules, "botbuilder.schema", schema)


async def _stream(*chunks: str) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


def _channel() -> tuple[MSTeamsChannel, list[str]]:
    """A started channel whose ``send_activity`` records each text it gets."""
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
async def test_send_splits_a_reply_past_the_teams_payload_cap() -> None:
    channel, sent = _channel()
    long_content = " ".join(f"word{i:05d}" for i in range(9000))  # ~100 KB
    assert _measure_activity_text(long_content) > _MSTEAMS_ACTIVITY_PAYLOAD_LIMIT

    await channel.send(OutgoingMessage(content=long_content, reply_to="conversation-A"))

    assert len(sent) > 1
    for piece in sent:
        assert _measure_activity_text(piece) <= _MSTEAMS_MESSAGE_TEXT_LIMIT
    assert "".join(sent) == long_content


@pytest.mark.asyncio
async def test_send_measures_the_text_the_way_the_service_does() -> None:
    # 20k of a 3-byte-in-UTF-8 / 2-byte-in-UTF-16 character is under the cap
    # by ``len`` alone; measured as the UTF-16 JSON the service sees it is
    # not, and a single Activity would be rejected.
    channel, sent = _channel()
    content = "あ" * 20_000

    await channel.send(OutgoingMessage(content=content, reply_to="conversation-A"))

    assert len(sent) > 1
    for piece in sent:
        assert len(json.dumps(piece, ensure_ascii=False).encode("utf-16-le")) <= (
            _MSTEAMS_MESSAGE_TEXT_LIMIT
        )
    assert "".join(sent) == content


@pytest.mark.asyncio
async def test_send_a_short_reply_is_still_a_single_activity() -> None:
    channel, sent = _channel()

    await channel.send(OutgoingMessage(content="hello", reply_to="conversation-A"))

    assert sent == ["hello"]


@pytest.mark.asyncio
async def test_send_remembers_every_chunk_for_edit_and_delete() -> None:
    channel, sent = _channel()
    long_content = "x" * 60_000

    await channel.send(OutgoingMessage(content=long_content, reply_to="conversation-A"))

    assert len(sent) > 1
    for index in range(1, len(sent) + 1):
        assert channel._message_conversation_keys[f"activity-{index}"] == "conversation-A"


def _streaming_channel(
    *, unsupported: bool = False
) -> tuple[MSTeamsChannel, list[str], list[str]]:
    """A started channel whose adapter records send_activity and update_activity."""
    channel = MSTeamsChannel(config=MSTeamsChannelConfig(name="msteams", edit_interval_s=0.0))
    channel._references = {"conversation-A": "REF_FOR_A"}
    channel._adapter = MagicMock()
    sent: list[str] = []
    updated: list[str] = []

    async def _continue_conversation(
        reference: object, callback: Callable[[object], Awaitable[None]], **_: object
    ) -> None:
        turn_context = MagicMock()

        async def _send_activity(text: str) -> MagicMock:
            sent.append(text)
            return MagicMock(id=f"activity-{len(sent)}")

        async def _update_activity(activity: Any) -> None:
            if unsupported:
                raise RuntimeError("update_activity is not supported on this channel")
            updated.append(str(getattr(activity, "text", "")))

        turn_context.send_activity = AsyncMock(side_effect=_send_activity)
        turn_context.update_activity = AsyncMock(side_effect=_update_activity)
        await callback(turn_context)

    channel._adapter.continue_conversation = AsyncMock(side_effect=_continue_conversation)
    return channel, sent, updated


@pytest.mark.asyncio
async def test_send_streaming_splits_a_reply_past_the_teams_payload_cap(
    botbuilder_schema: None,
) -> None:
    channel, sent, updated = _streaming_channel()
    words = [f"word{i:05d}" for i in range(9000)]
    full_text = " ".join(words)  # ~100 KB
    assert _measure_activity_text(full_text) > _MSTEAMS_ACTIVITY_PAYLOAD_LIMIT

    chunk_size = 500
    chunks = [" ".join(words[i : i + chunk_size]) + " " for i in range(0, len(words), chunk_size)]

    await channel.send_streaming(_stream(*chunks), reply_to="conversation-A")

    assert len(sent) > 1
    for piece in sent:
        assert _measure_activity_text(piece) <= _MSTEAMS_MESSAGE_TEXT_LIMIT
    for update in updated:
        assert _measure_activity_text(update) <= _MSTEAMS_MESSAGE_TEXT_LIMIT

    for index in range(1, len(sent) + 1):
        assert channel._message_conversation_keys[f"activity-{index}"] == "conversation-A"


@pytest.mark.asyncio
async def test_send_streaming_unsupported_splits_full_text(
    botbuilder_schema: None,
) -> None:
    channel, sent, _ = _streaming_channel(unsupported=True)
    chunk = "x" * 20_000
    await channel.send_streaming(_stream(chunk, chunk, chunk), reply_to="conversation-A")

    assert len(sent) > 1
    for piece in sent:
        assert _measure_activity_text(piece) <= _MSTEAMS_MESSAGE_TEXT_LIMIT
