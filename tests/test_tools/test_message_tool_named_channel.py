"""The ``message`` tool shapes a target by channel *type*, not channel name.

The tool is called with the configured channel name, and that name is free:
``docs/channels.md`` itself adds ``agentos channels add telegram --name
personal``. The target helpers compared that name with ``"telegram"`` /
``"slack"`` / ``"discord"``, so a renamed channel fell into the generic branch.
There, a forum-topic send put the topic number in Telegram's ``chat_id``, and
a delete lost its ``<target>|`` prefix. The adapter then deleted the message
with that id in its *default* chat instead of in the one the caller named.

These tests drive the real adapters and assert on the API payloads, so they
pin where a message lands, not what a helper returns.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from agentos.channels.discord import DiscordChannel, DiscordChannelConfig
from agentos.channels.manager import ChannelManager
from agentos.channels.slack import SlackChannel
from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig
from agentos.gateway.config import TelegramChannelEntry
from agentos.tools.builtin import messaging

_REQUEST = httpx.Request("POST", "https://api.test/")


def _tool() -> Any:
    """The tool body, past the decorators the registry wraps it in."""
    fn: Any = messaging.message
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _telegram(default_chat_id: str = "111") -> tuple[TelegramChannel, list[tuple[str, dict]]]:
    channel = TelegramChannel(TelegramChannelConfig(token="t", default_chat_id=default_chat_id))
    calls: list[tuple[str, dict]] = []

    async def fake_api(method: str, payload: dict[str, Any] | None = None) -> Any:
        calls.append((method, dict(payload or {})))
        return {"message_id": 1}

    channel._api = fake_api  # type: ignore[method-assign]
    return channel, calls


def _slack() -> tuple[SlackChannel, AsyncMock]:
    channel = SlackChannel(token="xoxb-test", slack_channel_id="C_DEFAULT")
    channel.bot_user_id = "UBOT"
    client = AsyncMock()
    client.post = AsyncMock(
        return_value=httpx.Response(200, json={"ok": True, "ts": "1.0"}, request=_REQUEST)
    )
    channel._client = client
    return channel, client.post


def _discord() -> tuple[DiscordChannel, AsyncMock]:
    channel = DiscordChannel(DiscordChannelConfig(token="t", default_channel_id="222"))
    client = AsyncMock()
    client.delete = AsyncMock(return_value=httpx.Response(204, request=_REQUEST))
    channel._client = client
    channel._get_client = lambda: client  # type: ignore[method-assign]
    return channel, client.delete


@pytest.fixture(autouse=True)
def _isolated_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from an empty adapter registry, whatever ran before."""
    monkeypatch.setattr(messaging, "_channels", {})


@pytest.fixture
def register() -> Iterator[Any]:
    names: list[str] = []

    def _register(name: str, adapter: object) -> None:
        messaging.register_channel(name, adapter)
        names.append(name)

    yield _register
    for name in names:
        messaging.unregister_channel(name)


# --- Fail on main: a renamed channel lost its type ------------------------


async def test_renamed_telegram_sends_to_the_target_chat_and_topic(register: Any) -> None:
    channel, calls = _telegram()
    register("personal", channel)

    await _tool()(channel="personal", target="-100222", text="hi", thread_id="42")

    [(method, payload)] = calls
    assert method == "sendMessage"
    assert payload["chat_id"] == "-100222"
    assert payload["message_thread_id"] == 42


async def test_renamed_telegram_deletes_in_the_target_chat(register: Any) -> None:
    """On main this deleted message 7 of the default chat ``111`` instead."""
    channel, calls = _telegram()
    register("personal", channel)

    await _tool()(channel="personal", target="-100222", action="delete", message_id="7")

    assert calls == [("deleteMessage", {"chat_id": "-100222", "message_id": 7})]


async def test_renamed_slack_deletes_in_the_target_conversation(register: Any) -> None:
    """On main this deleted ts ``1700.2`` in ``C_DEFAULT`` instead."""
    channel, post = _slack()
    register("team", channel)

    await _tool()(channel="team", target="C_TARGET", action="delete", message_id="1700.2")

    [call] = post.call_args_list
    assert call.args[0] == "/chat.delete"
    assert call.kwargs["json"] == {"channel": "C_TARGET", "ts": "1700.2"}


async def test_renamed_discord_deletes_in_the_target_channel(register: Any) -> None:
    """On main this deleted message 555 in the default channel ``222`` instead."""
    channel, delete = _discord()
    register("guild", channel)

    await _tool()(channel="guild", target="999", action="delete", message_id="555")

    [call] = delete.call_args_list
    assert call.args[0] == "/channels/999/messages/555"


async def test_a_duck_typed_adapter_is_addressed_by_its_profile(register: Any) -> None:
    """Any adapter exposing ``capability_profile.channel_type`` is honoured."""

    class OpsBot:
        capability_profile = SimpleNamespace(channel_type="Telegram")

        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def delete(self, message_id: str) -> None:
            self.deleted.append(message_id)

    adapter = OpsBot()
    register("ops", adapter)

    await _tool()(channel="ops", target="-100222", action="delete", message_id="7")

    assert adapter.deleted == ["-100222|7"]


async def test_renamed_channel_from_config_deletes_in_the_target_chat() -> None:
    """End to end from a config entry named as ``docs/channels.md`` shows."""
    entry = TelegramChannelEntry(name="personal", token="t", default_chat_id="111")
    manager = ChannelManager.from_config([entry], turn_runner=None, session_manager=None)
    adapter = manager.get("personal")
    assert isinstance(adapter, TelegramChannel)
    calls: list[tuple[str, dict]] = []

    async def fake_api(method: str, payload: dict[str, Any] | None = None) -> Any:
        calls.append((method, dict(payload or {})))
        return {}

    adapter._api = fake_api  # type: ignore[method-assign]
    try:
        await _tool()(channel="personal", target="-100222", action="delete", message_id="7")
    finally:
        manager._unregister_tool_channel("personal", adapter)

    assert calls == [("deleteMessage", {"chat_id": "-100222", "message_id": 7})]


async def test_renamed_channel_is_routed_after_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_safe_start`` unregisters and re-registers the adapter with the tool.

    This is the path a running gateway takes for every channel, after
    ``from_config`` has already registered it.
    """
    entry = TelegramChannelEntry(name="personal", token="t", default_chat_id="111")
    manager = ChannelManager.from_config([entry], turn_runner=None, session_manager=None)
    adapter = manager.get("personal")
    assert isinstance(adapter, TelegramChannel)
    calls: list[tuple[str, dict]] = []

    async def fake_api(method: str, payload: dict[str, Any] | None = None) -> Any:
        calls.append((method, dict(payload or {})))
        return {}

    async def idle(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.Event().wait()

    adapter._api = fake_api  # type: ignore[method-assign]
    monkeypatch.setattr(adapter, "start", AsyncMock())
    monkeypatch.setattr(manager, "_dispatch_with_retry", idle)
    try:
        await manager._safe_start("personal")
        await _tool()(channel="personal", target="-100222", action="delete", message_id="7")
    finally:
        task = manager._tasks.pop("personal", None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        manager._unregister_tool_channel("personal", adapter)

    assert calls == [("deleteMessage", {"chat_id": "-100222", "message_id": 7})]


async def test_a_reused_name_follows_the_adapter_now_registered(register: Any) -> None:
    """The type comes from the adapter, so re-registering a name re-types it."""
    old, _ = _telegram()
    messaging.register_channel("shared", old)
    messaging.unregister_channel("shared")

    channel, post = _slack()
    register("shared", channel)
    await _tool()(channel="shared", target="C_TARGET", action="delete", message_id="1700.2")

    assert post.call_args_list[0].kwargs["json"] == {"channel": "C_TARGET", "ts": "1700.2"}


# --- Guards: pass on main and with the fix, by design ---------------------


async def test_channel_named_after_its_type_is_unchanged(register: Any) -> None:
    """Guard: the common single-account setup behaves as before."""
    channel, calls = _telegram()
    register("telegram", channel)

    await _tool()(channel="telegram", target="-100222", text="hi", thread_id="42")
    await _tool()(channel="telegram", target="-100222", action="delete", message_id="7")

    assert calls[0][1]["chat_id"] == "-100222"
    assert calls[0][1]["message_thread_id"] == 42
    assert calls[1] == ("deleteMessage", {"chat_id": "-100222", "message_id": 7})


async def test_adapter_without_a_profile_is_addressed_by_its_name(register: Any) -> None:
    """Guard: a mock or duck-typed adapter registered as ``telegram`` works as before."""
    channel = AsyncMock()
    register("telegram", channel)

    await _tool()(channel="telegram", target="-100222", action="delete", message_id="7")
    await _tool()(channel="telegram", target="-100222", text="hi", thread_id="42")

    channel.delete.assert_awaited_once_with("-100222|7")
    sent = channel.send.await_args.args[0]
    assert sent.metadata == {"chat_id": "-100222", "thread_id": "42"}
    assert sent.reply_to == "-100222"


async def test_renamed_telegram_plain_send_still_reaches_the_target(register: Any) -> None:
    """Guard: without a topic the generic branch happened to pick the target too."""
    channel, calls = _telegram()
    register("personal", channel)

    await _tool()(channel="personal", target="-100222", text="hi")

    assert calls[0][1]["chat_id"] == "-100222"
    assert "message_thread_id" not in calls[0][1]


async def test_react_passes_the_target_through_for_any_name(register: Any) -> None:
    """Guard: react never branched on the channel, renamed or not."""
    channel = AsyncMock()
    register("personal", channel)

    await _tool()(
        channel="personal", target="-100222", action="react", message_id="7", reaction="👍"
    )

    channel.react.assert_awaited_once_with("-100222", "7", "👍")


async def test_unknown_channel_still_lists_the_configured_names(register: Any) -> None:
    """Guard: the error names what the caller can pass, which is the name."""
    channel, _ = _telegram()
    register("personal", channel)

    with pytest.raises(messaging.ToolError, match="Available: personal"):
        await _tool()(channel="telegram", target="-100222", text="hi")
