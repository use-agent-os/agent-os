"""Slack ``edit``/``delete`` honour the target conversation (#1807).

The ``message`` tool sends to whatever ``target`` the agent names, but
``action="delete"`` handed Slack the bare ``ts`` and ``SlackChannel.delete``
hardcoded ``channel=self.slack_channel_id``. Deleting a message that lives in
any other conversation therefore hit ``chat.delete`` with the wrong channel
and Slack answered ``message_not_found``. Telegram already solves this by
carrying the chat in the id (``<chat_id>|<message_id>``); Slack now accepts
the same ``<channel_id>|<ts>`` shape, and the tool builds it from ``target``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from agentos.channels.slack import SlackChannel
from agentos.tools.builtin import messaging

_REQUEST = httpx.Request("POST", "https://slack.test/api")


def _resp(body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(200, json=body if body is not None else {"ok": True}, request=_REQUEST)


def _channel(default: str = "C00000001") -> tuple[SlackChannel, AsyncMock]:
    channel = SlackChannel(token="xoxb-test", slack_channel_id=default)
    client = AsyncMock()
    client.post = AsyncMock(return_value=_resp())
    channel._client = client
    return channel, client.post


def _payload(post: AsyncMock) -> dict[str, Any]:
    assert post.await_count == 1
    return dict(post.await_args.kwargs["json"])


# --- adapter ---------------------------------------------------------------


async def test_delete_uses_channel_from_composite_id() -> None:
    channel, post = _channel()

    await channel.delete("C99999999|1712345678.123456")

    assert post.await_args.args[0] == "/chat.delete"
    assert _payload(post) == {"channel": "C99999999", "ts": "1712345678.123456"}


async def test_delete_bare_ts_falls_back_to_default_channel() -> None:
    channel, post = _channel()

    await channel.delete("1712345678.123456")

    assert _payload(post) == {"channel": "C00000001", "ts": "1712345678.123456"}


async def test_edit_uses_channel_from_composite_id() -> None:
    channel, post = _channel()

    await channel.edit("C99999999|1712345678.123456", "updated")

    assert post.await_args.args[0] == "/chat.update"
    assert _payload(post) == {
        "channel": "C99999999",
        "ts": "1712345678.123456",
        "text": "updated",
    }


async def test_edit_bare_ts_falls_back_to_default_channel() -> None:
    channel, post = _channel()

    await channel.edit("1712345678.123456", "updated")

    assert _payload(post)["channel"] == "C00000001"


async def test_composite_id_without_channel_part_uses_default() -> None:
    """``|ts`` (empty channel) must not send ``channel=""`` to Slack."""
    channel, post = _channel()

    await channel.delete("|1712345678.123456")

    assert _payload(post) == {"channel": "C00000001", "ts": "1712345678.123456"}


async def test_delete_without_any_channel_raises_before_calling_slack() -> None:
    channel, post = _channel(default="")

    with pytest.raises(RuntimeError, match="no target channel"):
        await channel.delete("1712345678.123456")

    assert post.await_count == 0


async def test_delete_api_error_reports_message_id() -> None:
    channel, post = _channel()
    post.return_value = _resp({"ok": False, "error": "message_not_found"})

    with pytest.raises(RuntimeError, match="message_not_found"):
        await channel.delete("C99999999|1712345678.123456")


# --- message tool ----------------------------------------------------------


@pytest.mark.parametrize(
    ("channel", "target", "message_id", "expected"),
    [
        ("slack", "C99999999", "1712345678.123456", "C99999999|1712345678.123456"),
        ("slack", "C99999999", "C11111111|1712345678.123456", "C11111111|1712345678.123456"),
        ("slack", "", "1712345678.123456", "1712345678.123456"),
        ("telegram", "12345", "678", "12345|678"),
        ("feishu", "oc_demo", "678", "678"),
    ],
)
def test_delete_message_id_encodes_target(channel, target, message_id, expected) -> None:
    assert messaging._delete_message_id(channel, target, message_id) == expected


async def test_message_tool_delete_routes_slack_target(monkeypatch: pytest.MonkeyPatch) -> None:
    channel, post = _channel()
    monkeypatch.setattr(messaging, "_channels", {"slack": channel})

    out = json.loads(
        await messaging.message(
            channel="slack",
            target="C99999999",
            message_id="1712345678.123456",
            action="delete",
        )
    )

    assert out == {
        "status": "deleted",
        "channel": "slack",
        "target": "C99999999",
        "message_id": "1712345678.123456",
    }
    assert _payload(post) == {"channel": "C99999999", "ts": "1712345678.123456"}
