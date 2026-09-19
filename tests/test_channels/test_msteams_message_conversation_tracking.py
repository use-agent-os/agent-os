"""Regression tests for MSTeamsChannel edit()/delete() conversation targeting.

``edit()``/``delete()`` used to grab ``next(iter(self._references.values()))``
-- whichever conversation was cached *first* -- regardless of which
conversation ``message_id`` actually belongs to. Any bot serving more than
one Teams conversation would silently act against the wrong one.

These tests exercise the real ``MSTeamsChannel`` methods end to end (with
the Bot Framework SDK mocked out, since it isn't an installable dependency
of this project -- see ``registry.py``'s ``_HIDDEN`` comment) and assert on
which ``ConversationReference`` actually reached ``continue_conversation``.
"""

from __future__ import annotations

import sys
import types
from collections.abc import AsyncIterator, Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentos.channels.msteams import MSTeamsChannel, MSTeamsChannelConfig
from agentos.channels.types import OutgoingMessage


@pytest.fixture(autouse=True)
def _stub_botbuilder_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """``edit()``/``send_streaming()`` do a lazy ``from botbuilder.schema import
    Activity``. Stub just enough of the module for that import to succeed --
    the tests never touch real Bot Framework wire behavior.
    """

    class _FakeActivity:
        def __init__(self, *, type: str, id: str, text: str) -> None:  # noqa: A002
            self.type = type
            self.id = id
            self.text = text

    schema_module = types.ModuleType("botbuilder.schema")
    schema_module.Activity = _FakeActivity  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "botbuilder.schema", schema_module)


def _channel_with_two_conversations() -> MSTeamsChannel:
    """A channel with conversation "A" cached before conversation "B"."""
    channel = MSTeamsChannel(config=MSTeamsChannelConfig(name="msteams"))
    channel._references = {
        "conversation-A": "REF_FOR_A",
        "conversation-B": "REF_FOR_B",
    }
    channel._adapter = MagicMock()
    channel._adapter.continue_conversation = AsyncMock()
    return channel


def _ref_passed_to_continue_conversation(channel: MSTeamsChannel) -> object:
    call = channel._adapter.continue_conversation.call_args
    return call.args[0] if call.args else call.kwargs.get("reference")


async def test_delete_uses_the_conversation_the_message_was_sent_into() -> None:
    """A message sent into B, then deleted, must delete from B -- not A."""
    channel = _channel_with_two_conversations()
    channel._message_conversation_keys["activity-in-b"] = "conversation-B"

    await channel.delete(message_id="activity-in-b")

    assert _ref_passed_to_continue_conversation(channel) == "REF_FOR_B"


async def test_edit_uses_the_conversation_the_message_was_sent_into() -> None:
    """Same as above for edit() -- previously identical bug, identical fix."""
    channel = _channel_with_two_conversations()
    channel._message_conversation_keys["activity-in-a"] = "conversation-A"

    await channel.edit(message_id="activity-in-a", content="updated")

    assert _ref_passed_to_continue_conversation(channel) == "REF_FOR_A"


async def test_delete_of_untracked_message_falls_back_to_most_recent_not_oldest() -> None:
    """Unknown ``message_id`` (e.g. sent before this adapter instance started
    tracking) must fall back to the same "most recent" rule ``send()``
    already uses -- never to whichever conversation happens to be oldest.
    """
    channel = _channel_with_two_conversations()

    await channel.delete(message_id="never-tracked")

    assert _ref_passed_to_continue_conversation(channel) == "REF_FOR_B"


async def test_delete_forgets_the_message_once_deleted() -> None:
    channel = _channel_with_two_conversations()
    channel._message_conversation_keys["activity-in-b"] = "conversation-B"

    await channel.delete(message_id="activity-in-b")

    assert "activity-in-b" not in channel._message_conversation_keys


async def test_send_records_which_conversation_the_message_landed_in() -> None:
    """send() must remember the returned activity id so a later delete()
    targets the right conversation -- previously send() didn't capture the
    id at all, so no plain-send()-originated message was ever trackable.
    """
    channel = _channel_with_two_conversations()

    async def _continue_conversation(
        reference: object, callback: Callable[[object], Awaitable[None]], **_: object
    ) -> None:
        turn_context = MagicMock()
        turn_context.send_activity = AsyncMock(return_value=MagicMock(id="new-activity-id"))
        await callback(turn_context)

    channel._adapter.continue_conversation.side_effect = _continue_conversation

    msg = OutgoingMessage(content="hi", metadata={}, reply_to="conversation-A")
    await channel.send(msg)

    assert channel._message_conversation_keys["new-activity-id"] == "conversation-A"

    # And a delete() of that id now correctly targets conversation A.
    channel._adapter.continue_conversation.side_effect = None
    channel._adapter.continue_conversation.reset_mock()
    await channel.delete(message_id="new-activity-id")
    assert _ref_passed_to_continue_conversation(channel) == "REF_FOR_A"


async def test_send_streaming_records_which_conversation_the_message_landed_in() -> None:
    """The first chunk of a stream must also register its conversation, so a
    later edit()/delete() by the streamed message id lands in the right chat.
    """
    channel = _channel_with_two_conversations()

    async def _continue_conversation(
        reference: object, callback: Callable[[object], Awaitable[None]], **_: object
    ) -> None:
        turn_context = MagicMock()
        turn_context.send_activity = AsyncMock(return_value=MagicMock(id="stream-activity-id"))
        turn_context.update_activity = AsyncMock()
        await callback(turn_context)

    channel._adapter.continue_conversation.side_effect = _continue_conversation

    async def _chunks() -> AsyncIterator[str]:
        yield "hello"

    message_id = await channel.send_streaming(_chunks(), reply_to="conversation-A")

    assert message_id == "stream-activity-id"
    assert channel._message_conversation_keys["stream-activity-id"] == "conversation-A"


def test_message_conversation_keys_is_bounded_and_evicts_lru() -> None:
    """Issue #3052: _message_conversation_keys must be bounded so high message volumes
    do not leak memory indefinitely."""
    channel = MSTeamsChannel(config=MSTeamsChannelConfig(name="msteams"))
    from agentos.util.bounded_registry import BoundedRegistry

    channel._message_conversation_keys = BoundedRegistry(
        name="test_message_keys",
        max_entries=3,
        register=False,
    )

    channel._remember_sent_message("msg-1", "conv-1")
    channel._remember_sent_message("msg-2", "conv-2")
    channel._remember_sent_message("msg-3", "conv-3")
    assert len(channel._message_conversation_keys) == 3

    channel._remember_sent_message("msg-4", "conv-4")
    assert len(channel._message_conversation_keys) == 3
    assert "msg-1" not in channel._message_conversation_keys
    assert channel._message_conversation_keys.get("msg-2") == "conv-2"
    assert channel._message_conversation_keys.get("msg-4") == "conv-4"
