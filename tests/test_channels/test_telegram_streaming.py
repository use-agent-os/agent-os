"""Streaming reply behaviour for the Telegram adapter.

Mirrors the Discord streaming coverage in ``test_discord_interactions.py``:
drive ``send_streaming`` with a stubbed ``_api`` and assert on the Bot API
calls it actually emits.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from agentos.channels._telegram_formatting import render_telegram_html
from agentos.channels.stream_policy import resolve_channel_stream_policy
from agentos.channels.telegram import (
    TelegramChannel,
    TelegramChannelConfig,
    TelegramFloodError,
)
from agentos.channels.types import IncomingMessage

ApiCall = tuple[str, dict[str, Any]]


def _install_fake_api(
    channel: TelegramChannel,
    *,
    fail: Any = None,
) -> list[ApiCall]:
    """Record Bot API calls; ``fail(method, payload)`` may raise to simulate errors."""
    calls: list[ApiCall] = []
    next_message_id = [100]

    async def fake_api(method: str, payload: dict[str, Any] | None = None) -> Any:
        calls.append((method, dict(payload or {})))
        if fail is not None:
            fail(method, dict(payload or {}))
        if method == "sendMessage":
            next_message_id[0] += 1
            return {"message_id": next_message_id[0]}
        return True

    channel._api = fake_api  # type: ignore[method-assign]  # noqa: SLF001
    return calls


async def _stream(*chunks: str) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


def _texts(calls: list[ApiCall], method: str) -> list[str]:
    return [payload["text"] for name, payload in calls if name == method]


def test_telegram_send_streaming_selects_adapter_stream_policy() -> None:
    policy = resolve_channel_stream_policy(TelegramChannel(TelegramChannelConfig()))

    assert policy.mode == "adapter_stream"
    assert policy.relay_stream is True


def test_telegram_streaming_reply_kwargs_target_the_inbound_chat() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    inbound = IncomingMessage(sender_id="user-1", channel_id="-100123", content="hi")

    assert channel.streaming_reply_kwargs(inbound) == {"chat_id": "-100123"}


def test_telegram_streaming_reply_kwargs_preserve_forum_topic() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    inbound = IncomingMessage(
        sender_id="user-1",
        channel_id="-100123",
        content="hi",
        metadata={"is_group": True, "thread_id": "777"},
    )

    assert channel.streaming_reply_kwargs(inbound) == {
        "chat_id": "-100123",
        "thread_id": "777",
    }


@pytest.mark.asyncio
async def test_telegram_send_streaming_edits_one_message_per_chunk() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    calls = _install_fake_api(channel)

    ref = await channel.send_streaming(
        _stream("Hello", " streaming", " world"),
        chat_id="-100123",
        thread_id="777",
        update_interval_ms=0,
    )

    methods = [name for name, _ in calls]
    assert methods == ["sendMessage", "editMessageText", "editMessageText"]
    # One message, edited in place — not one message per chunk.
    edited_ids = {payload["message_id"] for name, payload in calls if name == "editMessageText"}
    assert edited_ids == {101}
    assert calls[0][1]["message_thread_id"] == 777
    assert calls[0][1]["chat_id"] == "-100123"
    assert _texts(calls, "editMessageText")[-1] == render_telegram_html("Hello streaming world")
    assert ref == "-100123|101"
    assert channel._split_message_ref(ref) == ("-100123", "101")  # noqa: SLF001


@pytest.mark.asyncio
async def test_telegram_send_streaming_throttles_edits() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    calls = _install_fake_api(channel)

    await channel.send_streaming(
        _stream("one ", "two ", "three ", "four"),
        chat_id="-100123",
        update_interval_ms=60_000,
    )

    # Chunks arriving inside the interval coalesce: open once, flush once at end.
    assert [name for name, _ in calls] == ["sendMessage", "editMessageText"]
    assert _texts(calls, "editMessageText") == [render_telegram_html("one two three four")]


@pytest.mark.asyncio
async def test_telegram_send_streaming_without_target_raises() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    calls = _install_fake_api(channel)

    with pytest.raises(RuntimeError, match="no target chat"):
        await channel.send_streaming(_stream("dropped?"), update_interval_ms=0)

    # Raising is what makes dispatch replay the reply through ``channel.send``.
    assert calls == []


@pytest.mark.asyncio
async def test_telegram_send_streaming_empty_stream_sends_nothing() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    calls = _install_fake_api(channel)

    ref = await channel.send_streaming(_stream(), chat_id="-100123", update_interval_ms=0)

    assert ref is None
    assert calls == []


@pytest.mark.asyncio
async def test_telegram_send_streaming_degrades_to_final_send_on_flood() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))

    def fail(method: str, _payload: dict[str, Any]) -> None:
        if method == "editMessageText":
            raise TelegramFloodError("Too Many Requests", retry_after=30.0)

    calls = _install_fake_api(channel, fail=fail)

    ref = await channel.send_streaming(
        _stream("a", "b", "c", "d", "e"),
        chat_id="-100123",
        update_interval_ms=0,
    )

    # Three rejected edits trip the circuit; the rest arrives as a plain send.
    assert [name for name, _ in calls] == [
        "sendMessage",
        "editMessageText",
        "editMessageText",
        "editMessageText",
        "sendMessage",
    ]
    delivered = "".join(_texts(calls, "sendMessage"))
    assert delivered == render_telegram_html("a") + render_telegram_html("bcde")
    assert ref == "-100123|102"


@pytest.mark.asyncio
async def test_telegram_send_streaming_rolls_over_at_the_message_limit() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    calls = _install_fake_api(channel)
    long_text = "word " * 1200  # ~6000 chars, past Telegram's 4096 ceiling

    ref = await channel.send_streaming(
        _stream(long_text),
        chat_id="-100123",
        update_interval_ms=0,
    )

    sends = _texts(calls, "sendMessage")
    assert len(sends) == 2
    assert all(len(text) <= 4096 for text in sends)
    assert "".join(sends) == render_telegram_html(long_text)
    # The stream keeps editing the newest message after rolling over.
    assert ref == "-100123|102"


@pytest.mark.asyncio
async def test_telegram_send_streaming_keeps_editing_after_rollover() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    calls = _install_fake_api(channel)
    long_text = "word " * 1200

    await channel.send_streaming(
        _stream(long_text, "tail"),
        chat_id="-100123",
        update_interval_ms=0,
    )

    assert [name for name, _ in calls] == [
        "sendMessage",
        "sendMessage",
        "editMessageText",
    ]
    edit_payload = next(payload for name, payload in calls if name == "editMessageText")
    assert edit_payload["message_id"] == 102
    # The edit carries only the second message's segment, not the whole answer.
    assert edit_payload["text"].endswith(render_telegram_html("tail"))
    assert len(edit_payload["text"]) <= 4096


@pytest.mark.asyncio
async def test_telegram_send_streaming_ignores_message_not_modified_error() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))

    def fail(method: str, payload: dict[str, Any]) -> None:
        if method == "editMessageText":
            from agentos.channels.telegram import TelegramApiError

            raise TelegramApiError(
                "Bad Request: message is not modified: specified new message content "
                "and reply markup are exactly the same as a current content and reply markup"
            )

    calls = _install_fake_api(channel, fail=fail)

    ref = await channel.send_streaming(
        _stream("same text", "same text"),
        chat_id="-100123",
        update_interval_ms=0,
    )

    assert ref == "-100123|101"
    assert [name for name, _ in calls] == ["sendMessage", "editMessageText"]
