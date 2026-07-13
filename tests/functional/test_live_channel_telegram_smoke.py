"""Opt-in live Telegram channel smoke.

This is a maintainer-only release gate. It sends one real message through the
AgentOS Telegram adapter when explicitly enabled and credentialed.
"""

from __future__ import annotations

import os
import time

import pytest

from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig
from agentos.channels.types import OutgoingMessage

pytestmark = pytest.mark.live_channel


@pytest.mark.asyncio
async def test_telegram_adapter_can_send_real_message() -> None:
    if os.environ.get("AGENTOS_LIVE_TELEGRAM_E2E") != "1":
        pytest.skip("set AGENTOS_LIVE_TELEGRAM_E2E=1 to run live Telegram smoke")
    token = os.environ.get("AGENTOS_LIVE_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("AGENTOS_LIVE_TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        pytest.skip("Telegram token/chat id not set")

    marker = f"agentos live channel smoke {int(time.time())}"
    channel = TelegramChannel(
        TelegramChannelConfig(
            token=token,
            default_chat_id=chat_id,
            drop_pending_updates=True,
        )
    )
    try:
        result = await channel.send(OutgoingMessage(content=marker))
    finally:
        await channel.stop()

    assert isinstance(result.get("message_id"), int)
