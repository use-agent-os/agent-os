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
from collections import OrderedDict
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
    channel._references = OrderedDict(
        [
            ("conversation-A", "REF_FOR_A"),
            ("conversation-B", "REF_FOR_B"),
        ]
    )
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


class _FakeTurnContext:
    """Stand-in for ``botbuilder.core.TurnContext`` -- only the one static
    method ``_on_turn`` calls."""

    @staticmethod
    def get_conversation_reference(activity: object) -> str:
        return f"REF_FOR_{activity.conversation.id.rsplit('-', 1)[-1]}"  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _stub_botbuilder_core(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_on_turn`` does a lazy ``from botbuilder.core import TurnContext``."""
    core_module = types.ModuleType("botbuilder.core")
    core_module.TurnContext = _FakeTurnContext  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "botbuilder.core", core_module)


def _fake_message_activity(conversation_id: str, sender_id: str = "user1") -> types.SimpleNamespace:
    from types import SimpleNamespace

    return SimpleNamespace(
        type="message",
        id=f"activity-{conversation_id}",
        text="hi",
        entities=[],
        channel_data={},
        service_url="",
        conversation=SimpleNamespace(id=conversation_id, conversation_type="", tenant_id=""),
        from_property=SimpleNamespace(id=sender_id),
        recipient=SimpleNamespace(id="bot-id"),
    )


async def test_on_turn_reactivating_a_conversation_makes_it_the_most_recent() -> None:
    """Regression for #1789: ``_on_turn`` re-touching an *existing* cache key
    (a conversation that spoke again) must move it to the end of
    ``_references``, since the "most recent" fallback in
    ``_resolve_reference_key`` reads ``next(reversed(self._references))``.
    Before the fix, dict reassignment of an existing key left insertion
    order (and thus the fallback) unchanged, so B -- not the just-spoken A --
    kept winning.
    """
    channel = MSTeamsChannel(config=MSTeamsChannelConfig(name="msteams"))
    channel._adapter = MagicMock()
    channel._adapter.continue_conversation = AsyncMock()

    turn_context_a = MagicMock(activity=_fake_message_activity("conversation-A"))
    turn_context_b = MagicMock(activity=_fake_message_activity("conversation-B"))
    await channel._on_turn(turn_context_a)
    await channel._on_turn(turn_context_b)
    # A speaks again, after B -- A is now the most recently active conversation.
    await channel._on_turn(MagicMock(activity=_fake_message_activity("conversation-A")))

    msg = OutgoingMessage(content="proactive notice", metadata={}, reply_to=None)
    await channel.send(msg)

    assert _ref_passed_to_continue_conversation(channel) == "REF_FOR_A"


async def test_delete_of_untracked_message_follows_reactivated_conversation() -> None:
    """Same regression as above, through delete()'s untracked-message fallback
    (``_resolve_reference_for_message``), which reads
    ``next(reversed(self._references.values()))``.
    """
    channel = MSTeamsChannel(config=MSTeamsChannelConfig(name="msteams"))
    channel._adapter = MagicMock()
    channel._adapter.continue_conversation = AsyncMock()

    await channel._on_turn(MagicMock(activity=_fake_message_activity("conversation-A")))
    await channel._on_turn(MagicMock(activity=_fake_message_activity("conversation-B")))
    await channel._on_turn(MagicMock(activity=_fake_message_activity("conversation-A")))

    await channel.delete(message_id="never-tracked")

    assert _ref_passed_to_continue_conversation(channel) == "REF_FOR_A"


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
