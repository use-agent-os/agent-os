"""Where the ``message`` tool's ``target`` actually delivers.

``target`` is a required parameter of the tool, but for Slack it reached
neither ``OutgoingMessage.metadata`` nor ``reply_to``: ``_outgoing_metadata``
returned only ``thread_ts``, so ``SlackChannel.send`` fell through to its
statically configured ``slack_channel_id`` every time. The tool then reported
``{"status": "sent", "target": ...}`` with the target the caller asked for, so
nothing in the response revealed that the message had gone somewhere else.

These tests drive the real ``SlackChannel.send`` with a stubbed HTTP client and
assert on the ``chat.postMessage`` payload, so they pin where the message
lands, not merely what the helper returns.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from agentos.channels.slack import SlackChannel
from agentos.tools.builtin import messaging

_REQUEST = httpx.Request("POST", "https://slack.test/api")


def _ok_response() -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "ts": "1.0"}, request=_REQUEST)


@pytest.fixture
async def slack_post() -> AsyncIterator[AsyncMock]:
    """Register a real ``SlackChannel`` as the tool's ``slack`` adapter."""
    channel = SlackChannel(token="xoxb-test", slack_channel_id="C_DEFAULT")
    channel.bot_user_id = "UBOT"
    client = AsyncMock()
    client.post = AsyncMock(return_value=_ok_response())
    channel._client = client
    messaging.register_channel("slack", channel)
    try:
        yield client.post
    finally:
        messaging.unregister_channel("slack")


async def _send(**kwargs: Any) -> dict[str, Any]:
    """Call the tool body, past the decorators the registry wraps it in."""
    fn: Any = messaging.message
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return json.loads(await fn(**kwargs))


@pytest.mark.asyncio
async def test_a_slack_send_reaches_the_channel_it_was_given(slack_post: AsyncMock) -> None:
    """The reported case: a channel other than the configured default."""
    result = await _send(
        channel="slack", target="C99999999", text="Deployment complete", action="send"
    )

    payload = slack_post.await_args.kwargs["json"]
    assert payload["channel"] == "C99999999"
    assert payload["text"] == "Deployment complete"
    # The response has always echoed the requested target; now it is true.
    assert result == {"status": "sent", "channel": "slack", "target": "C99999999"}


@pytest.mark.asyncio
async def test_a_slack_send_can_target_a_channel_and_a_thread_at_once(
    slack_post: AsyncMock,
) -> None:
    """Both arguments have to survive: the thread anchor alone routed nowhere."""
    await _send(
        channel="slack",
        target="C99999999",
        text="in thread",
        action="send",
        thread_id="1700000000.000100",
    )

    payload = slack_post.await_args.kwargs["json"]
    assert payload["channel"] == "C99999999"
    assert payload["thread_ts"] == "1700000000.000100"


@pytest.mark.asyncio
async def test_a_group_or_dm_target_is_honoured_too(slack_post: AsyncMock) -> None:
    """Slack conversation ids are ``C``/``G``/``D``; none of them is special here."""
    for target in ("G12345678", "D87654321"):
        await _send(channel="slack", target=target, text="hi", action="send")
        assert slack_post.await_args.kwargs["json"]["channel"] == target


@pytest.mark.asyncio
async def test_an_empty_target_still_falls_back_to_the_configured_channel(
    slack_post: AsyncMock,
) -> None:
    """Passes either way by design: the behaviour a static deployment relies on."""
    await _send(channel="slack", target="", text="to the default", action="send")

    assert slack_post.await_args.kwargs["json"]["channel"] == "C_DEFAULT"


@pytest.mark.asyncio
async def test_a_thread_only_send_still_anchors_in_the_configured_channel(
    slack_post: AsyncMock,
) -> None:
    """Passes either way by design: the thread path the old metadata served."""
    await _send(
        channel="slack",
        target="",
        text="threaded",
        action="send",
        thread_id="1700000000.000100",
    )

    payload = slack_post.await_args.kwargs["json"]
    assert payload["channel"] == "C_DEFAULT"
    assert payload["thread_ts"] == "1700000000.000100"


def test_slack_metadata_carries_the_target() -> None:
    assert messaging._outgoing_metadata("slack", "C99999999", None) == {"channel": "C99999999"}
    assert messaging._outgoing_metadata("slack", "C99999999", "1700000000.000100") == {
        "channel": "C99999999",
        "thread_ts": "1700000000.000100",
    }


def test_the_other_channels_keep_the_metadata_they_had() -> None:
    """Passes either way by design: only the Slack branch changed."""
    assert messaging._outgoing_metadata("telegram", "12345", None) == {"chat_id": "12345"}
    assert messaging._outgoing_metadata("telegram", "12345", "7") == {
        "chat_id": "12345",
        "thread_id": "7",
    }
    assert messaging._outgoing_metadata("discord", "9876", None) == {"recipient": "9876"}
    assert messaging._reply_to_target("telegram", "12345", None) == "12345"
    assert messaging._reply_to_target("discord", "9876", None) == "9876"
