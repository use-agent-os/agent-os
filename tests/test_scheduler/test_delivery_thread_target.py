"""A scheduled job answers in the conversation it was scheduled in.

``DeliveryChain._post_to_channel`` and ``HeartbeatService._send_delivery``
resolved a thread id and then addressed the message with ``channel_id`` alone
for every channel except Slack. A Telegram forum topic is not the chat, and a
Discord thread is not its parent channel, so the resolved thread was dropped:
a job scheduled inside a topic delivered to the group's General topic, and one
scheduled inside a Discord thread delivered to the channel around it.

``channel_dispatch._route_envelope_reply_message`` states the rule without
naming a channel — when a reply targets a thread, the thread is the address and
the channel rides in ``metadata["channel"]``. These two paths now follow it,
while Slack's own branch and email's recipient handling stay exactly as they
were.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agentos.channels.manager import ChannelManager
from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig
from agentos.channels.types import IncomingMessage, OutgoingMessage
from agentos.scheduler.delivery import DeliveryChain, infer_delivery
from agentos.scheduler.heartbeat_service import HeartbeatService
from agentos.scheduler.types import (
    CronJob,
    DeliveryConfig,
    DeliveryMode,
    FailureDestination,
    ScheduleKind,
    SessionTarget,
)

CHAT = "-1001234567890"
TOPIC = "42"
MAILBOX = "alerts@example.com"
THREAD_KEY = "CAGr5Gg=xyz@mail.gmail.com"


class _FakeAdapter:
    def __init__(self) -> None:
        self.messages: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> None:
        self.messages.append(message)


def _manager(adapter: _FakeAdapter, channel_type: str) -> ChannelManager:
    return ChannelManager(
        _channels={channel_type: adapter},  # type: ignore[dict-item]
        _turn_runner=None,
        _session_manager=None,
        _channel_types={channel_type: channel_type},
    )


async def _deliver(
    channel_type: str,
    *,
    channel_id: str,
    thread_id: str = "",
    configured_recipient: bool = False,
) -> OutgoingMessage:
    adapter = _FakeAdapter()
    chain = DeliveryChain(channel_manager_ref=lambda: _manager(adapter, channel_type))
    status = await chain._post_to_channel(  # noqa: SLF001
        job_id="job-1",
        text="cron output",
        channel_name=channel_type,
        channel_id=channel_id,
        thread_id=thread_id,
        configured_recipient=configured_recipient,
    )
    assert status == "delivered", status
    (message,) = adapter.messages
    return message


async def _heartbeat(
    channel_type: str,
    *,
    channel_id: str,
    thread_id: str = "",
    mode: DeliveryMode = DeliveryMode.CHANNEL,
) -> OutgoingMessage:
    adapter = _FakeAdapter()
    service = HeartbeatService(
        turn_runner=None,
        session_storage=None,
        channel_manager_ref=lambda: _manager(adapter, channel_type),
    )
    delivery = DeliveryConfig(
        mode=mode,
        channel_name=channel_type,
        channel_id=channel_id,
        thread_id=thread_id,
    )
    assert await service._send_delivery(delivery, "heartbeat") is None  # noqa: SLF001
    (message,) = adapter.messages
    return message


def _telegram() -> TelegramChannel:
    return TelegramChannel(TelegramChannelConfig(token="token"))


# --- cron delivery ---------------------------------------------------------


async def test_cron_delivery_keeps_the_forum_topic_it_was_scheduled_in() -> None:
    message = await _deliver("telegram", channel_id=CHAT, thread_id=TOPIC)

    assert message.reply_to == TOPIC
    assert message.metadata["channel"] == CHAT


async def test_cron_output_reaches_the_topic_and_not_the_group_at_large() -> None:
    """The payload Telegram receives, not just the envelope."""
    payload = _telegram()._build_send_payload(  # noqa: SLF001
        await _deliver("telegram", channel_id=CHAT, thread_id=TOPIC)
    )

    assert payload["chat_id"] == CHAT
    assert payload["message_thread_id"] == int(TOPIC)


async def test_cron_delivery_matches_the_in_turn_reply_for_the_same_topic() -> None:
    channel = _telegram()
    inbound = IncomingMessage(
        channel_id=CHAT, sender_id="u1", content="hi", metadata={"thread_id": TOPIC}
    )

    cron = channel._build_send_payload(  # noqa: SLF001
        await _deliver("telegram", channel_id=CHAT, thread_id=TOPIC)
    )
    in_turn = channel._build_send_payload(  # noqa: SLF001
        channel.build_reply_message("cron output", inbound)
    )

    assert cron["chat_id"] == in_turn["chat_id"]
    assert cron["message_thread_id"] == in_turn["message_thread_id"]


async def test_cron_delivery_into_a_discord_thread_addresses_the_thread() -> None:
    """Discord resolves the target from ``reply_to``; a thread id is a channel id."""
    message = await _deliver("discord", channel_id="C-parent", thread_id="T-thread")

    assert message.reply_to == "T-thread"
    assert message.metadata == {"channel": "C-parent"}


async def test_cron_delivery_without_a_thread_is_unchanged() -> None:
    message = await _deliver("telegram", channel_id=CHAT)

    assert message.reply_to == CHAT
    assert message.metadata == {}


async def test_cron_delivery_with_a_thread_but_no_chat_invents_nothing() -> None:
    message = await _deliver("telegram", channel_id="", thread_id=TOPIC)

    assert message.reply_to == TOPIC
    assert message.metadata == {}


async def test_cron_delivery_leaves_the_slack_thread_shape_untouched() -> None:
    message = await _deliver("slack", channel_id="C123", thread_id="1700000000.1")

    assert message.reply_to == "1700000000.1"
    assert message.metadata == {"channel": "C123"}


async def test_cron_delivery_leaves_the_slack_channel_shape_untouched() -> None:
    """Slack's no-thread branch posts to the channel with its own sentinel."""
    message = await _deliver("slack", channel_id="C123")

    assert message.reply_to == "cron"
    assert message.metadata == {"channel": "C123", "thread_ts": None}


async def test_cron_delivery_still_names_the_mailbox_for_a_configured_email_target() -> None:
    message = await _deliver("email", channel_id=MAILBOX, configured_recipient=True)

    assert message.reply_to == MAILBOX
    assert message.metadata == {"to": MAILBOX}


async def test_cron_delivery_keeps_an_inferred_email_thread_as_reply_to_only() -> None:
    """Email never carries a separate thread id; ``channel_id`` is the thread key."""
    message = await _deliver("email", channel_id=THREAD_KEY)

    assert message.reply_to == THREAD_KEY
    assert message.metadata == {}


# --- failure notifications -------------------------------------------------


async def test_a_failure_notice_reaches_the_topic_too() -> None:
    adapter = _FakeAdapter()
    chain = DeliveryChain(channel_manager_ref=lambda: _manager(adapter, "telegram"))
    job = CronJob(
        id="job-1",
        name="job",
        schedule_kind=ScheduleKind.CRON,
        schedule_raw="*/5 * * * *",
        cron_expr="*/5 * * * *",
        handler_key="agent_run",
        session_target=SessionTarget.ISOLATED,
    )
    fd = FailureDestination(
        mode=DeliveryMode.CHANNEL,
        channel_name="telegram",
        channel_id=CHAT,
        thread_id=TOPIC,
    )

    assert await chain._deliver_to_failure_destination(job, "it failed", fd) == "delivered"  # noqa: SLF001

    (message,) = adapter.messages
    assert message.reply_to == TOPIC
    assert message.metadata == {"channel": CHAT}


# --- heartbeat -------------------------------------------------------------


async def test_heartbeat_keeps_the_forum_topic() -> None:
    message = await _heartbeat("telegram", channel_id=CHAT, thread_id=TOPIC)

    assert message.reply_to == TOPIC
    assert message.metadata == {"channel": CHAT}


async def test_heartbeat_reaches_the_topic_and_not_the_group_at_large() -> None:
    payload = _telegram()._build_send_payload(  # noqa: SLF001
        await _heartbeat("telegram", channel_id=CHAT, thread_id=TOPIC)
    )

    assert payload["chat_id"] == CHAT
    assert payload["message_thread_id"] == int(TOPIC)


async def test_heartbeat_into_a_discord_thread_addresses_the_thread() -> None:
    message = await _heartbeat("discord", channel_id="C-parent", thread_id="T-thread")

    assert message.reply_to == "T-thread"
    assert message.metadata == {"channel": "C-parent"}


async def test_heartbeat_without_a_thread_is_unchanged() -> None:
    message = await _heartbeat("telegram", channel_id=CHAT)

    assert message.reply_to == CHAT
    assert message.metadata == {}


async def test_heartbeat_leaves_the_slack_shapes_untouched() -> None:
    threaded = await _heartbeat("slack", channel_id="C123", thread_id="1700000000.1")
    assert threaded.reply_to == "1700000000.1"
    assert threaded.metadata == {"channel": "C123"}

    plain = await _heartbeat("slack", channel_id="C123")
    assert plain.reply_to == "cron"
    assert plain.metadata == {"channel": "C123", "thread_ts": None}


async def test_heartbeat_keeps_an_inferred_email_thread_as_reply_to_only() -> None:
    message = await _heartbeat("email", channel_id=THREAD_KEY, mode=DeliveryMode.ORIGIN)

    assert message.reply_to == THREAD_KEY
    assert message.metadata == {}


# --- provenance ------------------------------------------------------------


async def test_the_topic_a_job_was_scheduled_in_is_what_gets_stored() -> None:
    """The other half: origin mode reads the topic off the session node."""

    class _Storage:
        async def get_session(self, session_key: str) -> Any:
            return SimpleNamespace(
                last_channel="telegram",
                last_to=CHAT,
                last_account_id="",
                last_thread_id=TOPIC,
            )

    config = await infer_delivery(
        session_storage=_Storage(), session_key="agent:main:telegram", user_overrides=None
    )

    assert config.mode == DeliveryMode.ORIGIN
    assert (config.channel_id, config.thread_id) == (CHAT, TOPIC)


# --- target resolution -----------------------------------------------------


@pytest.mark.parametrize("channel_type", ["telegram", "discord", "slack"])
def test_a_thread_resolves_for_every_channel_that_can_address_one(channel_type: str) -> None:
    manager = _manager(_FakeAdapter(), channel_type)

    resolved = manager.resolve_delivery_target(target=channel_type, to="C1", thread_id="T1")

    assert resolved.ok, resolved.reason
    assert resolved.thread_id == "T1"


def test_a_thread_id_beside_an_email_thread_key_is_still_refused() -> None:
    """Email's thread key is the channel id; a second one is a mistake."""
    manager = _manager(_FakeAdapter(), "email")

    resolved = manager.resolve_delivery_target(
        target="email", to=THREAD_KEY, thread_id="something-else"
    )

    assert not resolved.ok
    assert resolved.reason == "unsupported_thread"


def test_resolution_without_a_thread_is_unchanged_everywhere() -> None:
    for channel_type in ("telegram", "discord", "slack", "email"):
        resolved = _manager(_FakeAdapter(), channel_type).resolve_delivery_target(
            target=channel_type, to="C1"
        )
        assert resolved.ok, (channel_type, resolved.reason)
        assert resolved.thread_id == ""
