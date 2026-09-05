"""Streaming reply behavior for the MS Teams adapter."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

# Mock botbuilder before importing msteams channel
sys.modules.setdefault("botbuilder", MagicMock())
sys.modules.setdefault("botbuilder.schema", MagicMock())

from agentos.channels.msteams import MSTeamsChannel, MSTeamsChannelConfig  # noqa: E402


async def _stream(*chunks: str) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


@pytest.mark.asyncio
async def test_msteams_send_streaming_first_chunk_binds_accumulated_by_value() -> None:
    """The _send closure must bind _text=accumulated by value (GH #1046)."""
    channel = MSTeamsChannel(config=MSTeamsChannelConfig(name="msteams"))
    channel._references["conv-1"] = SimpleNamespace()

    sent_texts: list[str] = []
    saved_send_callback: Any = None

    class FakeTurnContext:
        async def send_activity(self, text_or_activity: Any) -> Any:
            sent_texts.append(str(text_or_activity))
            return SimpleNamespace(id="msg-101")

        async def update_activity(self, activity: Any) -> Any:
            return None

    class FakeAdapter:
        async def continue_conversation(
            self,
            ref: Any,
            callback: Any,
            bot_id: Any = None,
        ) -> None:
            nonlocal saved_send_callback
            if saved_send_callback is None:
                saved_send_callback = callback
            # Run the callback
            await callback(FakeTurnContext())

    channel._adapter = FakeAdapter()  # type: ignore[assignment]

    await channel.send_streaming(_stream("chunk1", "chunk2"), reply_to="conv-1")

    # Verify first chunk was sent with "chunk1"
    assert sent_texts[0] == "chunk1"

    # Verify that the closure parameter _text was bound by value with default="chunk1"
    assert saved_send_callback is not None
    import inspect

    sig = inspect.signature(saved_send_callback)
    assert "_text" in sig.parameters
    assert sig.parameters["_text"].default == "chunk1"

    # If invoked directly after accumulated has changed to "chunk1chunk2",
    # it still uses the bound _text ("chunk1") rather than the mutated outer variable
    second_context = FakeTurnContext()
    await saved_send_callback(second_context)
    assert sent_texts[-1] == "chunk1"


@pytest.mark.asyncio
async def test_msteams_send_streaming_requires_start() -> None:
    channel = MSTeamsChannel(config=MSTeamsChannelConfig(name="msteams"))
    with pytest.raises(RuntimeError, match="requires start"):
        await channel.send_streaming(_stream("hello"))


@pytest.mark.asyncio
async def test_msteams_send_streaming_no_cached_reference_raises() -> None:
    channel = MSTeamsChannel(config=MSTeamsChannelConfig(name="msteams"))
    channel._adapter = MagicMock()
    with pytest.raises(RuntimeError, match="no conversation reference cached"):
        await channel.send_streaming(_stream("hello"))

