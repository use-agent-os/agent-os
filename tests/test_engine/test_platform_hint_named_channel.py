"""The plain-text rendering hint follows the channel *type*, not its name.

For a channel turn ``channel_kind`` is the configured entry name. The render
hints are keyed by type (``email``), so an email channel added the way
``docs/channels.md`` shows it, ``agentos channels add email --name inbox``,
never got "reply in plain text". Its replies then arrived with literal
``**`` and ``#`` markers. The reverse also held: a non-email channel that
happened to be *named* ``email`` got the email hint.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agentos.channels.types import IncomingMessage
from agentos.engine.pipeline import TurnContext
from agentos.engine.steps.inject_platform_hint import inject_platform_hint
from agentos.gateway.config import (
    ChannelsConfig,
    EmailChannelEntry,
    GatewayConfig,
    PromptConfig,
    TelegramChannelEntry,
)
from agentos.gateway.routing import build_channel_route_envelope, tool_context_from_envelope

_PLAIN_TEXT = "Reply in plain text"


def _email(name: str) -> EmailChannelEntry:
    return EmailChannelEntry(
        name=name,
        imap_host="imap.example.com",
        imap_username="agent@example.com",
        imap_password="app-password",
        smtp_host="smtp.example.com",
        from_address="agent@example.com",
        allowed_senders=["you@example.com"],
    )


def _telegram(name: str) -> TelegramChannelEntry:
    return TelegramChannelEntry(name=name, token="t")


def _config(*entries: Any, hint_enabled: bool = True) -> GatewayConfig:
    return GatewayConfig(
        channels=ChannelsConfig(channels=list(entries)),
        prompt=PromptConfig(platform_hint_enabled=hint_enabled),
    )


async def _hint_for_channel_turn(config: GatewayConfig, channel_name: str) -> TurnContext:
    """Run an inbound message on ``channel_name`` through routing and the hint step."""
    msg = IncomingMessage(channel_id="thread-1", sender_id="you@example.com", content="hi")
    key = f"agent:main:{channel_name}:direct:you@example.com"
    envelope = build_channel_route_envelope(msg, session_key=key, session_prefix=channel_name)
    tool_ctx = tool_context_from_envelope(envelope)
    turn = TurnContext(
        message="hi",
        session_key=key,
        config=config,
        provider=None,
        model="m",
        tool_defs=[],
        system_prompt=("base", ""),
        metadata={"channel_kind": tool_ctx.channel_kind},
    )
    return await inject_platform_hint(turn)


def _suffix(turn: TurnContext) -> str:
    assert isinstance(turn.system_prompt, tuple)
    return turn.system_prompt[1]


# --- Fail on main: the hint was looked up by channel name -----------------


async def test_email_channel_named_as_the_docs_show_gets_the_plain_text_hint() -> None:
    turn = await _hint_for_channel_turn(_config(_email("inbox")), "inbox")

    assert _PLAIN_TEXT in _suffix(turn)
    assert turn.metadata["platform_markdown_hint"] == "email"


@pytest.mark.parametrize("name", ["support", "Work-Mail", "email-2"])
async def test_any_email_channel_name_gets_the_hint(name: str) -> None:
    turn = await _hint_for_channel_turn(_config(_email(name)), name)

    assert _PLAIN_TEXT in _suffix(turn)


async def test_the_hint_follows_the_entry_among_several_channels() -> None:
    config = _config(_telegram("personal"), _email("inbox"), _email("billing"))

    assert _PLAIN_TEXT in _suffix(await _hint_for_channel_turn(config, "billing"))
    assert _PLAIN_TEXT not in _suffix(await _hint_for_channel_turn(config, "personal"))


async def test_a_telegram_channel_named_email_does_not_get_the_email_hint() -> None:
    """The name alone used to decide: Telegram renders Markdown, so no hint."""
    turn = await _hint_for_channel_turn(_config(_telegram("email")), "email")

    assert _PLAIN_TEXT not in _suffix(turn)
    assert turn.metadata["inject_platform_hint__applied"] is False


# --- Guards: pass on main and with the fix, by design ---------------------


async def test_email_channel_named_email_still_gets_the_hint() -> None:
    """Guard: the name-equals-type setup keeps working."""
    turn = await _hint_for_channel_turn(_config(_email("email")), "email")

    assert _PLAIN_TEXT in _suffix(turn)
    assert turn.metadata["platform_markdown_hint"] == "email"


async def test_renamed_markdown_channel_still_gets_no_hint() -> None:
    """Guard: a Telegram channel named ``personal`` is left alone."""
    turn = await _hint_for_channel_turn(_config(_telegram("personal")), "personal")

    assert _suffix(turn) == ""
    assert turn.metadata["inject_platform_hint__applied"] is False


async def test_kill_switch_still_wins_for_a_renamed_email_channel() -> None:
    """Guard: ``prompt.platform_hint_enabled = false`` is honoured first."""
    turn = await _hint_for_channel_turn(_config(_email("inbox"), hint_enabled=False), "inbox")

    assert _suffix(turn) == ""
    assert turn.metadata["inject_platform_hint__applied"] is False


@pytest.mark.parametrize(
    ("channel_kind", "expected"),
    [
        ("whatsapp", True),
        ("SMS", True),
        ("webchat", False),
        ("cli", False),
        ("cron", False),
        ("", False),
    ],
)
async def test_kinds_that_are_not_entry_names_resolve_as_before(
    channel_kind: str, expected: bool
) -> None:
    """Guard: web, cli and cron turns, and configs without channels, are unchanged."""
    turn = TurnContext(
        message="hi",
        session_key="agent:main:main",
        config=SimpleNamespace(prompt=SimpleNamespace(platform_hint_enabled=True)),
        provider=None,
        model="m",
        tool_defs=[],
        system_prompt=("base", ""),
        metadata={"channel_kind": channel_kind},
    )

    turn = await inject_platform_hint(turn)

    assert (_PLAIN_TEXT in _suffix(turn)) is expected
    if expected:
        assert turn.metadata["platform_markdown_hint"] == channel_kind.lower()
