"""Regression: the plain-text mention fallback must not match on a substring.

``is_group_mentioned`` checks structured Telegram entities first, but when a
message carries no entity metadata at all, it falls back to
``mention in text.lower()``. A Telegram username is ``[A-Za-z0-9_]+``, so a
bot named ``@helper`` is a substring of ``@helperbot2`` and of
``someone@helperdesk.com`` -- either falsely satisfies the mention gate.
"""

from __future__ import annotations

from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig
from agentos.channels.types import IncomingMessage


def _group_msg(content: str) -> IncomingMessage:
    return IncomingMessage(
        sender_id="42",
        channel_id="-100",
        content=content,
        metadata={"is_group": True},
    )


def _channel() -> TelegramChannel:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    channel.bot_username = "helper"
    channel.bot_user_id = "555"
    return channel


def test_a_longer_username_that_contains_ours_is_not_a_mention() -> None:
    channel = _channel()

    assert channel.is_group_mentioned(_group_msg("hey @helperbot2 can you handle this?")) is False


def test_an_email_address_containing_our_username_is_not_a_mention() -> None:
    channel = _channel()

    assert channel.is_group_mentioned(_group_msg("contact someone@helperdesk.com")) is False


def test_our_exact_mention_is_still_recognized() -> None:
    """Guard: the plain-text fallback must still catch a real mention."""
    channel = _channel()

    assert channel.is_group_mentioned(_group_msg("hey @helper can you handle this?")) is True


def test_our_mention_immediately_followed_by_punctuation_is_recognized() -> None:
    """Guard: a boundary check must not require whitespace after the mention."""
    channel = _channel()

    assert channel.is_group_mentioned(_group_msg("@helper, can you handle this?")) is True
