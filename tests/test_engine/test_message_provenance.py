"""Assistant messages record the provider / model that actually produced them.

The stamp is taken after the provider stream finished, so when the failover
wrapper switched to the next provider mid-turn the message names the fallback
model, not the one that failed. The stamp travels with the structured
transcript record and reads back as unknown for rows written before it existed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from agentos.engine import Agent, AgentConfig, ToolResult
from agentos.engine.runtime import _SelectorFallbackProvider
from agentos.engine.transcript import (
    decode_message,
    decode_transcript_record,
    encode_message,
)
from agentos.provider import (
    ContentBlockText,
    ContentBlockThinking,
    Message,
    MessageProvenance,
)
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import ErrorEvent as ProviderError
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.provider import ToolUseEndEvent as ProviderToolUseEnd
from agentos.provider import ToolUseStartEvent as ProviderToolUseStart


class _Provider:
    def __init__(self, kind: str, model: str, scripts: list[list[Any]]) -> None:
        self.provider_name = kind
        self.provider_kind = kind
        self.model = model
        self._scripts = scripts
        self.calls = 0

    def chat(self, messages: list[Message], tools: Any = None, config: Any = None) -> Any:
        script = self._scripts[min(self.calls, len(self._scripts) - 1)]
        self.calls += 1
        return self._stream(script)

    async def _stream(self, script: list[Any]) -> AsyncIterator[Any]:
        for event in script:
            yield event

    async def list_models(self) -> list[Any]:
        return []


class _Selector:
    def __init__(self, backup: _Provider) -> None:
        self._backup = backup
        self.failovers = 0

    def has_fallback(self) -> bool:
        return self.failovers == 0

    def next_fallback_after_failure(self, failure: Exception) -> _Provider:
        if self.failovers:
            raise IndexError("no fallback left")
        self.failovers += 1
        return self._backup

    def record_provider_failure(self, *args: Any, **kwargs: Any) -> None:
        return None

    def record_provider_success(self) -> None:
        return None


async def _tool(call: Any) -> ToolResult:
    return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="ok")


def _assistants(record: dict[str, Any] | None) -> list[Message]:
    decoded = decode_transcript_record(record)
    assert decoded is not None
    return [message for message in decoded.messages if message.role == "assistant"]


@pytest.mark.asyncio
async def test_assistant_messages_name_the_provider_that_answered() -> None:
    provider = _Provider(
        "anthropic",
        "claude-opus-5-5",
        [
            [
                ProviderToolUseStart(tool_use_id="t1", tool_name="lookup"),
                ProviderToolUseEnd(tool_use_id="t1", tool_name="lookup", arguments={}),
                ProviderDone(
                    stop_reason="tool_use",
                    thinking_blocks=[ContentBlockThinking(thinking="plan", signature="sig")],
                ),
            ],
            [ProviderText(text="done"), ProviderDone(stop_reason="end_turn")],
        ],
    )
    agent = Agent(provider=provider, config=AgentConfig(max_iterations=3), tool_handler=_tool)
    _ = [event async for event in agent.run_turn("look it up")]

    stamp = MessageProvenance(provider="anthropic", model="claude-opus-5-5", api="anthropic")
    assistants = _assistants(agent.transcript_record())
    assert len(assistants) == 2
    assert all(message.provenance == stamp for message in assistants)


@pytest.mark.asyncio
async def test_failover_mid_turn_stamps_the_fallback_model() -> None:
    primary = _Provider(
        "opencap", "claude-opus-5-5", [[ProviderError(code="503", message="upstream 503")]]
    )
    backup = _Provider(
        "opencap",
        "deepseek-v4-pro",
        [[ProviderText(text="from backup"), ProviderDone(stop_reason="end_turn")]],
    )
    selector = _Selector(backup)
    agent = Agent(
        provider=_SelectorFallbackProvider(primary, selector),
        config=AgentConfig(max_iterations=2),
        tool_handler=_tool,
    )
    _ = [event async for event in agent.run_turn("hello")]

    assert selector.failovers == 1
    assert backup.calls == 1
    (assistant,) = _assistants(agent.transcript_record())
    assert assistant.provenance == MessageProvenance(
        provider="opencap", model="deepseek-v4-pro", api="deepseek"
    )


def test_provenance_round_trips_through_the_record_codec() -> None:
    message = Message(
        role="assistant",
        content=[ContentBlockText(text="hi")],
        provenance=MessageProvenance(provider="openrouter", model="openrouter/auto"),
    )
    encoded = encode_message(message)
    assert encoded["provenance"] == {"provider": "openrouter", "model": "openrouter/auto"}
    assert decode_message(encoded) == message


@pytest.mark.parametrize("stored", [None, "garbage", {}, {"model": 3}, {"model": "  "}])
def test_rows_without_usable_provenance_read_as_unknown(stored: Any) -> None:
    payload: dict[str, Any] = {"role": "assistant", "content": [{"type": "text", "text": "x"}]}
    if stored is not None:
        payload["provenance"] = stored
    decoded = decode_message(payload)
    assert decoded is not None
    assert decoded.provenance is None
