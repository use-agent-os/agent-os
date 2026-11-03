from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.channels.contract import ChannelCapabilities, ChannelSendStatus
from agentos.channels.stream_policy import resolve_channel_stream_policy
from agentos.channels.telegram import TelegramApiError, TelegramChannel, TelegramChannelConfig
from agentos.channels.types import IncomingMessage
from agentos.gateway import channel_dispatch


def _install_blocking_keepalive_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[float], asyncio.Event]:
    sleep_intervals: list[float] = []
    sleep_started = asyncio.Event()
    block_sleep = asyncio.Event()

    async def fake_sleep(interval: float) -> None:
        sleep_intervals.append(interval)
        sleep_started.set()
        await block_sleep.wait()

    monkeypatch.setattr(
        channel_dispatch,
        "asyncio",
        SimpleNamespace(create_task=asyncio.create_task, sleep=fake_sleep),
    )
    return sleep_intervals, sleep_started


@pytest.mark.asyncio
async def test_telegram_send_typing_posts_native_chat_action_for_topic() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_api(method: str, payload: dict[str, Any] | None = None) -> bool:
        calls.append((method, payload))
        return True

    channel._api = fake_api  # type: ignore[method-assign]  # noqa: SLF001

    result = await channel.send_typing(channel_id="-100123", thread_id="777")

    assert calls == [
        (
            "sendChatAction",
            {
                "chat_id": "-100123",
                "action": "typing",
                "message_thread_id": 777,
            },
        )
    ]
    assert result.status == ChannelSendStatus.SENT
    assert result.capability == ChannelCapabilities.TYPING_INDICATOR
    assert result.target_id == "-100123"


@pytest.mark.asyncio
async def test_telegram_send_typing_uses_default_target() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token", default_chat_id="default-chat"))
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_api(method: str, payload: dict[str, Any] | None = None) -> bool:
        calls.append((method, payload))
        return True

    channel._api = fake_api  # type: ignore[method-assign]  # noqa: SLF001

    result = await channel.send_typing()

    assert calls == [("sendChatAction", {"chat_id": "default-chat", "action": "typing"})]
    assert result.status == ChannelSendStatus.SENT
    assert result.target_id == "default-chat"


@pytest.mark.asyncio
async def test_telegram_send_typing_without_target_is_unsupported() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))

    async def unexpected_api(_method: str, _payload: dict[str, Any] | None = None) -> bool:
        raise AssertionError("sendChatAction must not run without a target")

    channel._api = unexpected_api  # type: ignore[method-assign]  # noqa: SLF001

    result = await channel.send_typing()

    assert result.status == ChannelSendStatus.UNSUPPORTED
    assert result.capability == ChannelCapabilities.TYPING_INDICATOR
    assert result.reason == "no chat target"


def test_telegram_defaults_to_typing_then_one_final_message() -> None:
    """``send_streaming`` exists, but a default channel must not live-edit.

    Without the adapter's own strategy the policy picks ``adapter_stream`` for
    any adapter exposing ``send_streaming``; the default Telegram reply is the
    typing status for the whole run and the finished text sent once.
    """
    channel = TelegramChannel(TelegramChannelConfig())

    policy = resolve_channel_stream_policy(channel)

    assert policy.mode == "typing_final"
    assert policy.relay_stream is False
    assert policy.typing_keepalive is True
    assert policy.typing_preamble is False
    assert 0 < channel.typing_keepalive_interval_s < 5


def test_telegram_stream_replies_selects_adapter_stream_policy() -> None:
    channel = TelegramChannel(TelegramChannelConfig(stream_replies=True))

    policy = resolve_channel_stream_policy(channel)

    assert channel.capability_profile.typing_indicator is True
    assert ChannelCapabilities.TYPING_INDICATOR in channel.capabilities
    assert policy.mode == "adapter_stream"
    assert policy.relay_stream is True
    assert policy.typing_keepalive is False
    assert policy.typing_preamble is True
    assert 0 < channel.typing_keepalive_interval_s < 5


@pytest.mark.asyncio
async def test_telegram_keepalive_uses_inbound_chat_topic_and_adapter_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    api_calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_api(method: str, payload: dict[str, Any] | None = None) -> bool:
        api_calls.append((method, payload))
        return True

    channel._api = fake_api  # type: ignore[method-assign]  # noqa: SLF001
    sleep_intervals, sleep_started = _install_blocking_keepalive_sleep(monkeypatch)
    inbound = IncomingMessage(
        sender_id="user-1",
        channel_id="-100123",
        content="hello",
        metadata={"is_group": True, "thread_id": "777"},
    )

    task = channel_dispatch._start_typing_keepalive(channel, inbound)  # noqa: SLF001

    assert task is not None
    await asyncio.wait_for(sleep_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert api_calls == [
        (
            "sendChatAction",
            {
                "chat_id": "-100123",
                "action": "typing",
                "message_thread_id": 777,
            },
        )
    ]
    assert sleep_intervals == [4.0]


@pytest.mark.asyncio
async def test_telegram_keepalive_treats_api_failure_as_best_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    attempts = 0

    async def failing_api(_method: str, _payload: dict[str, Any] | None = None) -> bool:
        nonlocal attempts
        attempts += 1
        raise TelegramApiError("rate limited")

    channel._api = failing_api  # type: ignore[method-assign]  # noqa: SLF001
    sleep_intervals, sleep_started = _install_blocking_keepalive_sleep(monkeypatch)
    inbound = IncomingMessage(
        sender_id="user-1",
        channel_id="chat-1",
        content="hello",
    )

    task = channel_dispatch._start_typing_keepalive(channel, inbound)  # noqa: SLF001

    assert task is not None
    await asyncio.wait_for(sleep_started.wait(), timeout=1)
    assert task.done() is False
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert attempts == 1
    assert sleep_intervals == [4.0]


@pytest.mark.asyncio
async def test_telegram_streaming_shows_typing_until_the_first_chunk() -> None:
    """A streaming Telegram turn still gets the indicator during model latency.

    Nothing can be edited into the chat before the first token exists, and with
    tool calls in the loop that wait is routinely tens of seconds — the
    preamble covers exactly that gap, then releases on the stop signal.
    """
    channel = TelegramChannel(TelegramChannelConfig(token="token", stream_replies=True))
    api_calls: list[tuple[str, dict[str, Any] | None]] = []
    first_call = asyncio.Event()

    async def fake_api(method: str, payload: dict[str, Any] | None = None) -> bool:
        api_calls.append((method, payload))
        first_call.set()
        return True

    channel._api = fake_api  # type: ignore[method-assign]  # noqa: SLF001
    inbound = IncomingMessage(sender_id="user-1", channel_id="-100123", content="hello")
    first_chunk_sent = asyncio.Event()

    task = channel_dispatch._start_typing_keepalive(  # noqa: SLF001
        channel,
        inbound,
        stop_signal=first_chunk_sent,
    )

    assert task is not None
    await asyncio.wait_for(first_call.wait(), timeout=1)
    assert api_calls == [("sendChatAction", {"chat_id": "-100123", "action": "typing"})]

    # The adapter now has text on screen: the indicator must stop on its own,
    # without waiting to be cancelled.
    first_chunk_sent.set()
    await asyncio.wait_for(task, timeout=1)

    assert task.cancelled() is False
    assert api_calls == [("sendChatAction", {"chat_id": "-100123", "action": "typing"})]


@pytest.mark.asyncio
async def test_telegram_streaming_typing_needs_a_stop_signal() -> None:
    """No signal, no preamble — never leave typing ticking under a live stream."""
    channel = TelegramChannel(TelegramChannelConfig(token="token", stream_replies=True))

    async def unexpected_api(_method: str, _payload: dict[str, Any] | None = None) -> bool:
        raise AssertionError("typing must not start without a stop signal")

    channel._api = unexpected_api  # type: ignore[method-assign]  # noqa: SLF001
    inbound = IncomingMessage(sender_id="user-1", channel_id="-100123", content="hello")

    assert channel_dispatch._start_typing_keepalive(channel, inbound) is None  # noqa: SLF001


@pytest.mark.parametrize(
    ("raw", "expected_mode"),
    [({}, "typing_final"), ({"stream_replies": True}, "adapter_stream")],
)
def test_telegram_stream_replies_reaches_the_adapter_from_gateway_config(
    raw: dict[str, Any],
    expected_mode: str,
) -> None:
    """The gateway entry is what users edit; the adapter config is what dispatch reads."""
    from agentos.channels.registry import build_managed_channel, parse_channel_entry

    entry = parse_channel_entry({"type": "telegram", "name": "tg", "token": "token", **raw})
    channel = build_managed_channel(entry)

    assert isinstance(channel, TelegramChannel)
    assert resolve_channel_stream_policy(channel).mode == expected_mode
