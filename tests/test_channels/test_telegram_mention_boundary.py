"""Issue #2464: the plain-text mention check matched a username as a substring.

``is_group_mentioned`` checks Telegram's structured entities first and then
falls back to ``mention in text.lower()``. A Telegram username is word
characters after the ``@``, so ``@helper`` is a substring of a different bot's
``@helperbot2`` and of ``someone@helperdesk.com``; with the default group
policy (``mention_required_in_group=True``) the bot replied to messages that
never addressed it.

The fallback is not only for messages without entities. Telegram attaches a
``mention`` entity to every ``@username`` it renders, so the realistic shape
in a group is entities *present*, naming a different bot, matching nothing --
and then the substring check ran anyway. That is the case that reaches
production, and it is pinned below alongside the entity-less one.
"""

from __future__ import annotations

import pytest

from agentos.channels.telegram import (
    TelegramChannel,
    TelegramChannelConfig,
    _text_mentions_username,
)
from agentos.channels.types import IncomingMessage


def _channel(username: str = "helper") -> TelegramChannel:
    channel = TelegramChannel(TelegramChannelConfig(token="test-token"))
    channel.bot_username = username
    channel.bot_user_id = "555"
    return channel


def _group(content: str, **metadata: object) -> IncomingMessage:
    return IncomingMessage(
        sender_id="42",
        channel_id="-100",
        content=content,
        metadata={"is_group": True, **metadata},
    )


def _mention_entity(text: str, handle: str) -> dict[str, object]:
    """A Telegram ``mention`` entity for *handle* where it sits in *text*."""
    offset = text.index(handle)
    return {"type": "mention", "offset": offset, "length": len(handle)}


# ── the issue's reproduction ────────────────────────────────────────────────


def test_a_longer_username_that_starts_with_ours_is_not_us() -> None:
    assert _channel().is_group_mentioned(_group("hey @helperbot2 can you handle this?")) is False


def test_an_email_address_containing_our_username_is_not_a_mention() -> None:
    assert _channel().is_group_mentioned(_group("mail someone@helperdesk.com")) is False


# ── the boundary, on both sides ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "hey @helperbot2",
        "@helper_bot please",
        "@helpers unite",
        "@helper2",
        "@helperx",
    ],
)
def test_a_word_character_after_the_username_is_another_username(text: str) -> None:
    assert _channel().is_group_mentioned(_group(text)) is False


@pytest.mark.parametrize(
    "text",
    [
        "mail bob@helper.com",
        "x@helper",
        "user_@helper",
        "1@helper",
        "email me at bob@helper right away",
    ],
)
def test_a_word_character_before_the_at_sign_is_an_address_not_a_mention(text: str) -> None:
    """The side #2594's fix does not check: ``bob@helper.com`` is an email
    whose domain happens to be our username."""
    assert _channel().is_group_mentioned(_group(text)) is False


@pytest.mark.parametrize(
    "text",
    [
        "@helper",
        "hey @helper can you",
        "(@helper)",
        "@helper, please",
        "thanks @helper!",
        "@helper?",
        "cc: @helper.",
        "@helper\nsecond line",
        "first line\n@helper",
        "[@helper]",
        '"@helper"',
        "→ @helper ←",
        "@helper's turn",
    ],
)
def test_punctuation_and_whitespace_around_the_username_are_boundaries(text: str) -> None:
    assert _channel().is_group_mentioned(_group(text)) is True


@pytest.mark.parametrize("text", ["thanks @Helper!", "@HELPER", "hey @hElPeR"])
def test_the_match_is_case_insensitive(text: str) -> None:
    assert _channel().is_group_mentioned(_group(text)) is True


def test_a_bot_username_with_uppercase_letters_still_matches() -> None:
    assert _channel("HelperBot").is_group_mentioned(_group("ping @helperbot")) is True
    assert _channel("HelperBot").is_group_mentioned(_group("ping @HelperBot")) is True


def test_a_username_with_regex_metacharacters_is_matched_literally() -> None:
    """Usernames cannot contain them, but the helper must not assume that."""
    assert _text_mentions_username("hi @a.b", "a.b") is True
    assert _text_mentions_username("hi @axb", "a.b") is False


# ── the realistic production shape: entities present, none of them us ───────


def test_a_mention_entity_for_another_bot_does_not_fall_through_to_a_substring_match() -> None:
    text = "hey @helperbot2 can you handle this?"

    result = _channel().is_group_mentioned(
        _group(text, entities=[_mention_entity(text, "@helperbot2")])
    )

    assert result is False


def test_a_mention_entity_for_us_still_counts() -> None:
    text = "hey @helper can you"

    assert (
        _channel().is_group_mentioned(_group(text, entities=[_mention_entity(text, "@helper")]))
        is True
    )


def test_two_entities_one_of_them_us() -> None:
    text = "@helperbot2 and @helper both"
    entities = [_mention_entity(text, "@helperbot2"), _mention_entity(text, "@helper")]

    assert _channel().is_group_mentioned(_group(text, entities=entities)) is True


def test_a_url_entity_alongside_an_address_does_not_rescue_the_substring() -> None:
    """Entities that are not mentions at all leave the fallback to decide."""
    text = "see https://example.com and mail someone@helperdesk.com"
    entities = [{"type": "url", "offset": 4, "length": 19}]

    assert _channel().is_group_mentioned(_group(text, entities=entities)) is False


# ── what must not change ────────────────────────────────────────────────────


def test_a_private_chat_never_needs_a_mention() -> None:
    msg = IncomingMessage(sender_id="42", channel_id="42", content="hi", metadata={})

    assert _channel().is_group_mentioned(msg) is True


def test_no_bot_username_means_no_mention_possible() -> None:
    assert _channel("").is_group_mentioned(_group("hey @helper")) is False


def test_a_reply_to_the_bot_counts_regardless_of_text() -> None:
    assert _channel().is_group_mentioned(_group("sure", reply_to_message_from_id="555")) is True


def test_a_text_mention_entity_with_our_id_counts() -> None:
    entity = {"type": "text_mention", "offset": 0, "length": 6, "user": {"id": 555}}

    assert _channel().is_group_mentioned(_group("Helper hi", entities=[entity])) is True


def test_a_bot_command_addressed_to_another_bot_still_declines() -> None:
    text = "/start@helperbot2"
    entity = {"type": "bot_command", "offset": 0, "length": len(text)}

    assert _channel().is_group_mentioned(_group(text, entities=[entity])) is False


def test_a_bare_bot_command_still_counts() -> None:
    entity = {"type": "bot_command", "offset": 0, "length": 6}

    assert _channel().is_group_mentioned(_group("/start now", entities=[entity])) is True


def test_a_mention_after_a_non_bmp_character_is_found_by_the_fallback() -> None:
    """The plain-text path has no offsets to drift; an emoji before the
    handle must not matter."""
    assert _channel().is_group_mentioned(_group("🚀 @helper go")) is True


# ── the helper on its own ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "username", "expected"),
    [
        ("@helper", "helper", True),
        ("@helper", "@helper", True),
        ("@helperbot2", "helper", False),
        ("a@helper", "helper", False),
        ("@helper", "helperbot2", False),
        ("", "helper", False),
        ("no handle here", "helper", False),
        ("@@helper", "helper", True),
    ],
)
def test_text_mentions_username(text: str, username: str, expected: bool) -> None:
    assert _text_mentions_username(text, username) is expected


def test_a_leading_at_in_the_configured_username_is_tolerated() -> None:
    """``getMe`` returns the bare name, but the rest of this method already
    strips a leading ``@``; the fallback now does the same rather than
    building ``@@helper`` and matching nothing."""
    assert _channel("@helper").is_group_mentioned(_group("hey @helper")) is True
