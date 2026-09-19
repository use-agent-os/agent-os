"""``is_group_mentioned``'s last-resort plain-text fallback must not match a
username as a substring of a different, longer one (#2066-adjacent).

Discord and Slack gate group mentions on ``bot_user_id in extract_mentions(text)``
-- a structured, ID-based check. Telegram's structured entity checks (mention,
text_mention, bot_command) are equally precise, but when Telegram sends no
entity metadata at all, the fallback used to be plain ``mention in text.lower()``:
``@helper`` is a substring of a different bot's ``@helperbot2`` and of an
unrelated ``someone@helperdesk.com``, so either would silently make the bot
respond to a group message that never actually addressed it.
"""

from __future__ import annotations

from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig
from agentos.channels.types import IncomingMessage


def _channel(username: str = "helper") -> TelegramChannel:
    channel = TelegramChannel(TelegramChannelConfig(token="test-token"))
    channel.bot_username = username
    channel.bot_user_id = "555"
    return channel


def _msg(content: str) -> IncomingMessage:
    # No entities at all -- forces the plain-text fallback path.
    return IncomingMessage(
        sender_id="42",
        channel_id="-100",
        content=content,
        metadata={"is_group": True},
    )


def test_fallback_does_not_match_a_different_longer_username() -> None:
    channel = _channel()
    assert channel.is_group_mentioned(_msg("hey @helperbot2 can you handle this?")) is False


def test_fallback_does_not_match_inside_an_email_or_url() -> None:
    channel = _channel()
    assert channel.is_group_mentioned(_msg("contact someone@helperdesk.com please")) is False
    assert channel.is_group_mentioned(_msg("see https://example.com/@helperbotics")) is False


def test_fallback_still_matches_an_exact_plain_mention() -> None:
    channel = _channel()
    assert channel.is_group_mentioned(_msg("hey @helper can you help?")) is True


def test_fallback_matches_regardless_of_surrounding_punctuation() -> None:
    channel = _channel()
    assert channel.is_group_mentioned(_msg("(@helper) please look")) is True
    assert channel.is_group_mentioned(_msg("@helper, thanks!")) is True
    assert channel.is_group_mentioned(_msg("cc @helper.")) is True


def test_fallback_is_case_insensitive_at_the_boundary() -> None:
    channel = _channel()
    assert channel.is_group_mentioned(_msg("Hey @HELPER can you help?")) is True
    assert channel.is_group_mentioned(_msg("hey @HELPERBOT2 handle this?")) is False
