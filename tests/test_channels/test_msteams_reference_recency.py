"""Regression tests for MSTeamsChannel's "most recently active" fallback.

``send(reply_to=None)`` and ``edit()``/``delete()`` of an untracked message
fall back to ``next(reversed(self._references))`` -- documented as "whoever
last spoke to the bot". A plain ``dict[key] = ref`` reassignment does not
move an existing key to the end, so once conversation A had spoken *before*
B, A could never become "most recent" again no matter how often it talked.
A cron/heartbeat delivery would then land in B.

``_on_turn`` must re-insert the key on every touch so iteration order tracks
last activity, matching ``email.py``'s ``_threads`` and ``_util.py``'s
``EventDedupeCache``.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentos.channels.msteams import MSTeamsChannel, MSTeamsChannelConfig
from agentos.channels.types import OutgoingMessage


@pytest.fixture(autouse=True)
def _stub_botbuilder(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_on_turn`` lazily imports ``botbuilder.core.TurnContext`` for
    ``get_conversation_reference``; ``edit()`` imports ``botbuilder.schema``.
    Stub both -- the SDK is not an installable dependency of this project.
    """

    class _FakeTurnContext:
        @staticmethod
        def get_conversation_reference(activity: object) -> str:
            # The reference is opaque to the adapter; tag it with the
            # activity id so tests can tell *which* turn produced it.
            return f"REF:{activity.conversation.id}:{activity.id}"  # type: ignore[attr-defined]

    class _FakeActivity:
        def __init__(self, *, type: str, id: str, text: str) -> None:  # noqa: A002
            self.type = type
            self.id = id
            self.text = text

    core_module = types.ModuleType("botbuilder.core")
    core_module.TurnContext = _FakeTurnContext  # type: ignore[attr-defined]
    schema_module = types.ModuleType("botbuilder.schema")
    schema_module.Activity = _FakeActivity  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "botbuilder.core", core_module)
    monkeypatch.setitem(sys.modules, "botbuilder.schema", schema_module)


def _turn(conversation_id: str, activity_id: str) -> SimpleNamespace:
    activity = SimpleNamespace(
        type="message",
        id=activity_id,
        text="hi",
        conversation=SimpleNamespace(id=conversation_id, conversation_type="personal"),
        from_property=SimpleNamespace(id="user-1"),
        recipient=SimpleNamespace(id="bot-1"),
        entities=[],
        service_url="https://smba.trafficmanager.net/",
        channel_data={},
    )
    return SimpleNamespace(activity=activity)


async def _channel_after_a_b_a() -> MSTeamsChannel:
    """A speaks, B speaks, then A speaks again -- A is the most recent."""
    channel = MSTeamsChannel(config=MSTeamsChannelConfig(name="msteams"))
    channel._adapter = MagicMock()
    channel._adapter.continue_conversation = AsyncMock()
    await channel._on_turn(_turn("conversation-A", "a1"))
    await channel._on_turn(_turn("conversation-B", "b1"))
    await channel._on_turn(_turn("conversation-A", "a2"))
    return channel


def _ref_passed_to_continue_conversation(channel: MSTeamsChannel) -> object:
    call = channel._adapter.continue_conversation.call_args
    return call.args[0] if call.args else call.kwargs.get("reference")


async def test_on_turn_moves_a_reactivated_conversation_to_the_end() -> None:
    channel = await _channel_after_a_b_a()

    assert list(channel._references) == ["conversation-B", "conversation-A"]
    # The *latest* reference for A is kept, not the first one.
    assert channel._references["conversation-A"] == "REF:conversation-A:a2"


async def test_send_without_reply_to_targets_whoever_spoke_last() -> None:
    channel = await _channel_after_a_b_a()

    assert channel._resolve_reference_key(None) == "conversation-A"

    await channel.send(OutgoingMessage(content="heartbeat", metadata={}, reply_to=None))

    assert _ref_passed_to_continue_conversation(channel) == "REF:conversation-A:a2"


async def test_untracked_delete_falls_back_to_whoever_spoke_last() -> None:
    """``edit()``/``delete()`` share the same fallback and the same ordering."""
    channel = await _channel_after_a_b_a()

    await channel.delete(message_id="never-tracked")

    assert _ref_passed_to_continue_conversation(channel) == "REF:conversation-A:a2"


async def test_untracked_edit_falls_back_to_whoever_spoke_last() -> None:
    channel = await _channel_after_a_b_a()

    await channel.edit(message_id="never-tracked", content="updated")

    assert _ref_passed_to_continue_conversation(channel) == "REF:conversation-A:a2"


async def test_on_turn_does_not_reorder_when_the_latest_speaker_speaks_again() -> None:
    """Touching the already-last key must be a no-op for ordering."""
    channel = MSTeamsChannel(config=MSTeamsChannelConfig(name="msteams"))
    await channel._on_turn(_turn("conversation-A", "a1"))
    await channel._on_turn(_turn("conversation-B", "b1"))
    await channel._on_turn(_turn("conversation-B", "b2"))

    assert list(channel._references) == ["conversation-A", "conversation-B"]
    assert channel._resolve_reference_key(None) == "conversation-B"
