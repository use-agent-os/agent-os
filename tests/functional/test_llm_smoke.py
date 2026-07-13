"""Opt-in live LLM smoke.

This is intentionally the only live LLM smoke restored for open-source
readiness. It skips unless credentials are explicitly present.
"""

from __future__ import annotations

import os

import pytest

from agentos.provider.openai import OpenAIProvider
from agentos.provider.types import ChatConfig, DoneEvent, ErrorEvent, Message, TextDeltaEvent

pytestmark = [pytest.mark.llm, pytest.mark.llm_smoke]

_EXPECTED_TOKEN = "agentos-live-smoke-ok"


@pytest.mark.asyncio
async def test_openrouter_live_smoke_returns_expected_token() -> None:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        pytest.skip("OPENROUTER_API_KEY not set")

    provider = OpenAIProvider(
        api_key=api_key,
        model=os.environ.get("LLM_TEST_MODEL", "openai/gpt-4o-mini"),
        base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        provider_kind="openrouter",
    )
    text_parts: list[str] = []
    done = False

    async for event in provider.chat(
        [Message(role="user", content=f"Reply with exactly {_EXPECTED_TOKEN}.")],
        config=ChatConfig(max_tokens=32, temperature=0.0, timeout=45.0),
    ):
        if isinstance(event, ErrorEvent):
            pytest.fail(f"live LLM smoke failed: {event.code} {event.message}")
        if isinstance(event, TextDeltaEvent):
            text_parts.append(event.text)
        if isinstance(event, DoneEvent):
            done = True

    assert done is True
    assert _EXPECTED_TOKEN in "".join(text_parts).strip().lower()
