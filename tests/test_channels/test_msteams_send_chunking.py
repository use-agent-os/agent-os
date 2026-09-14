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
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentos.channels.msteams import _MSTEAMS_TEXT_LIMIT, MSTeamsChannel, MSTeamsChannelConfig
from agentos.channels.types import OutgoingMessage


@pytest.fixture(autouse=True)
def _stub_botbuilder_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    schema_module = types.ModuleType("botbuilder.schema")
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
async def test_send_tracks_the_last_chunk_for_edit_delete() -> None:
    """Matches Discord's "track the last chunk" convention: a later
    edit()/delete() by the returned id must act on the final message."""
    channel, sent = _channel()
    long_content = "y" * (_MSTEAMS_TEXT_LIMIT * 2 + 10)

    await channel.send(OutgoingMessage(content=long_content, reply_to="conversation-A"))

    last_id = f"activity-{len(sent)}"
    assert channel._message_conversation_keys[last_id] == "conversation-A"
