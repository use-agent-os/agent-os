"""A subagent result must land in the conversation the parent turn came from.

Both background-completion delivery and the subagent announcement addressed a
thread by putting the thread id in ``reply_to`` and dropping the channel id.
Slack was special-cased out of that collapse; Telegram was not, and a Telegram
forum topic id is not a chat id. ``_build_send_payload`` then read the topic
number as ``chat_id``, so a background answer for a turn that arrived in a forum
topic was addressed to a chat that has nothing to do with the group.

``channel_dispatch._route_envelope_reply_message`` already states the rule for
the in-turn reply: when a reply targets a thread, the channel id rides in
``metadata["channel"]``. These two paths now follow it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig
from agentos.channels.types import IncomingMessage, OutgoingMessage
from agentos.gateway.background_completion import _build_channel_message
from agentos.gateway.subagent_announce import _announce_to_parent_channel

CHAT = "-1001234567890"
TOPIC = "42"


class _Adapter:
    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> None:
        self.sent.append(message)


class _ChannelManager:
    def __init__(self, adapter: _Adapter) -> None:
        self._adapter = adapter

    def get(self, channel_name: str) -> Any:
        return self._adapter


def _telegram() -> TelegramChannel:
    return TelegramChannel(TelegramChannelConfig(token="token"))


async def _announce(channel_name: str, channel_id: str | None, thread_id: str | None) -> _Adapter:
    adapter = _Adapter()
    await _announce_to_parent_channel(
        {"child_session_key": "agent:main:child", "status": "succeeded", "result": {"text": "ok"}},
        parent=SimpleNamespace(
            last_channel=channel_name, last_to=channel_id, last_thread_id=thread_id
        ),
        channel_manager=_ChannelManager(adapter),
    )
    return adapter


# --- background completion -------------------------------------------------


def test_background_delivery_keeps_the_chat_when_targeting_a_forum_topic() -> None:
    message = _build_channel_message(
        channel_name="telegram", channel_id=CHAT, thread_id=TOPIC, content="final"
    )

    assert message.reply_to == TOPIC
    assert message.metadata["channel"] == CHAT


def test_background_delivery_reaches_the_group_the_turn_came_from() -> None:
    """The consequence: the payload Telegram receives, not just the envelope."""
    message = _build_channel_message(
        channel_name="telegram", channel_id=CHAT, thread_id=TOPIC, content="final"
    )

    payload = _telegram()._build_send_payload(message)  # noqa: SLF001

    assert payload["chat_id"] == CHAT
    assert payload["message_thread_id"] == int(TOPIC)


def test_background_delivery_matches_the_in_turn_reply_for_the_same_topic() -> None:
    """Parity with the path that answers the user inline; it was already right."""
    inbound = IncomingMessage(
        channel_id=CHAT, sender_id="u1", content="hi", metadata={"thread_id": TOPIC}
    )
    channel = _telegram()

    background = channel._build_send_payload(  # noqa: SLF001
        _build_channel_message(
            channel_name="telegram", channel_id=CHAT, thread_id=TOPIC, content="final"
        )
    )
    in_turn = channel._build_send_payload(  # noqa: SLF001
        channel.build_reply_message("final", inbound)
    )

    assert background["chat_id"] == in_turn["chat_id"]
    assert background["message_thread_id"] == in_turn["message_thread_id"]


def test_background_delivery_without_a_thread_addresses_the_channel() -> None:
    """No thread means nothing to preserve -- the channel stays in reply_to."""
    message = _build_channel_message(
        channel_name="telegram", channel_id=CHAT, thread_id=None, content="final"
    )

    assert message.reply_to == CHAT
    assert "channel" not in message.metadata


def test_background_delivery_with_only_a_thread_invents_no_channel() -> None:
    """A target with no channel id is delivered as before, not guessed at."""
    message = _build_channel_message(
        channel_name="telegram", channel_id=None, thread_id=TOPIC, content="final"
    )

    assert message.reply_to == TOPIC
    assert message.metadata == {}


@pytest.mark.parametrize("channel_name", ["discord", "msteams", "email"])
def test_background_delivery_keeps_reply_to_for_the_other_channels(channel_name: str) -> None:
    """These adapters resolve the target from reply_to and ignore the extra key.

    Regression guard in the opposite direction: the fix must not move what they
    are addressed by, only add a field beside it.
    """
    message = _build_channel_message(
        channel_name=channel_name, channel_id="C1", thread_id="T1", content="final"
    )

    assert message.reply_to == "T1"
    assert message.metadata == {"channel": "C1"}


def test_background_delivery_leaves_the_slack_thread_shape_untouched() -> None:
    message = _build_channel_message(
        channel_name="slack", channel_id="C123", thread_id="1700000000.1", content="final"
    )

    assert message.reply_to == "1700000000.1"
    assert message.metadata == {"channel": "C123"}


def test_background_delivery_leaves_the_slack_channel_shape_untouched() -> None:
    """Slack's no-thread branch posts to the channel with thread_ts cleared."""
    message = _build_channel_message(
        channel_name="slack", channel_id="C123", thread_id=None, content="final"
    )

    assert message.reply_to is None
    assert message.metadata == {"channel": "C123", "thread_ts": None}


# --- subagent announcement -------------------------------------------------


@pytest.mark.asyncio
async def test_announcement_keeps_the_chat_when_targeting_a_forum_topic() -> None:
    adapter = await _announce("telegram", CHAT, TOPIC)

    assert adapter.sent[0].reply_to == TOPIC
    assert adapter.sent[0].metadata["channel"] == CHAT


@pytest.mark.asyncio
async def test_announcement_reaches_the_group_the_turn_came_from() -> None:
    adapter = await _announce("telegram", CHAT, TOPIC)

    payload = _telegram()._build_send_payload(adapter.sent[0])  # noqa: SLF001

    assert payload["chat_id"] == CHAT
    assert payload["message_thread_id"] == int(TOPIC)


@pytest.mark.asyncio
async def test_announcement_without_a_thread_addresses_the_channel() -> None:
    adapter = await _announce("telegram", CHAT, None)

    assert adapter.sent[0].reply_to == CHAT
    assert adapter.sent[0].metadata == {}


@pytest.mark.asyncio
async def test_announcement_leaves_the_slack_shape_untouched() -> None:
    adapter = await _announce("slack", "C123", "1700000000.1")

    assert adapter.sent[0].reply_to == "1700000000.1"
    assert adapter.sent[0].metadata == {"channel": "C123"}


@pytest.mark.asyncio
async def test_announcement_with_no_channel_name_sends_nothing() -> None:
    """Guard: a parent that never spoke on a channel is not announced to one."""
    adapter = await _announce("", CHAT, TOPIC)

    assert adapter.sent == []
