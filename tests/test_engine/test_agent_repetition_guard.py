"""Agent-level behaviour of the repetition guard: cut, close, continue, stop."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
import structlog.testing

from agentos.engine import Agent, AgentConfig, ToolResult
from agentos.engine.repetition_guard import RepetitionGuardSettings
from agentos.engine.runtime import TurnRunner
from agentos.provider import (
    ChatConfig,
    Message,
    ToolDefinition,
    ToolInputSchema,
)
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.provider import ThinkingDeltaEvent as ProviderThinking
from agentos.provider import ToolUseEndEvent as ProviderToolUseEnd
from agentos.provider import ToolUseStartEvent as ProviderToolUseStart
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage
from agentos.tools.types import CallerKind, ToolContext

_LEAD = "Here is what I found in the logs so far:\n"
_LINE = "I will now check the configuration file again to be sure.\n"


class _TrackedStream:
    def __init__(self, events: list[Any]) -> None:
        self.events = events
        self.pulled = 0
        self.closed = False

    async def iterate(self) -> AsyncIterator[Any]:
        try:
            for event in self.events:
                self.pulled += 1
                yield event
        finally:
            self.closed = True


class _Provider:
    provider_name = "fake"

    def __init__(self, streams: list[list[Any]]) -> None:
        self.streams = [_TrackedStream(events) for events in streams]
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        index = len(self.calls)
        self.calls.append(list(messages))
        return self.streams[index].iterate()

    async def list_models(self) -> list[Any]:
        return []


def _looping_text(copies: int = 3_000) -> list[Any]:
    events: list[Any] = [ProviderText(text=_LEAD)]
    events.extend(ProviderText(text=_LINE) for _ in range(copies))
    events.append(ProviderDone(stop_reason="length", input_tokens=10, output_tokens=40_000))
    return events


def _finish(text: str = "All checks passed.") -> list[Any]:
    return [
        ProviderText(text=text),
        ProviderDone(stop_reason="stop", input_tokens=12, output_tokens=5),
    ]


def _config(**overrides: Any) -> AgentConfig:
    return AgentConfig(retry_base_backoff_ms=0, retry_max_backoff_ms=0, **overrides)


def _message_text(message: Message) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(getattr(block, "text", "") for block in content)


@pytest.mark.asyncio
async def test_looping_text_is_cut_closed_and_continued_once() -> None:
    provider = _Provider([_looping_text(), _finish()])
    agent = Agent(provider=provider, config=_config())

    looping = provider.streams[0]
    closed_when_cut: bool | None = None
    events: list[Any] = []
    with structlog.testing.capture_logs() as captured:
        async for event in agent.run_turn("check the logs"):
            if event.kind == "repetition_stop":
                closed_when_cut = looping.closed
            events.append(event)

    # The stream was closed as soon as it was cut, not read to max_tokens.
    assert closed_when_cut is True
    assert looping.pulled < len(looping.events) // 4
    assert len(provider.calls) == 2

    stop = next(event for event in events if event.kind == "repetition_stop")
    assert stop.channel == "text"
    assert stop.signal == "repeated_lines"
    streamed = "".join(
        event.text for event in events[: events.index(stop)] if event.kind == "text_delta"
    )
    assert streamed.endswith(stop.removed_text)
    kept = streamed[: len(streamed) - len(stop.removed_text)]
    assert kept.startswith(_LEAD.rstrip("\n")[:30])
    assert kept.count("check the configuration file again") == 1

    assert any(event.kind == "warning" and event.code == "repetition_detected" for event in events)
    assert not any(event.kind == "error" for event in events)
    done = next(event for event in events if event.kind == "done")
    assert done.text == kept + "All checks passed."

    continuation = provider.calls[1]
    assert _message_text(continuation[-2]) == kept
    assert continuation[-2].role == "assistant"
    assert continuation[-1].role == "user"
    assert "repeating" in _message_text(continuation[-1])

    detected = [entry for entry in captured if entry.get("event") == "provider.repetition_detected"]
    assert len(detected) == 1
    assert detected[0]["action"] == "continue"
    assert detected[0]["signal"] == "repeated_lines"
    assert detected[0]["kept_chars"] == len(kept)


@pytest.mark.asyncio
async def test_second_trip_ends_the_turn_with_repetition_detected() -> None:
    provider = _Provider([_looping_text(), _looping_text()])
    agent = Agent(provider=provider, config=_config())

    with structlog.testing.capture_logs() as captured:
        events = [event async for event in agent.run_turn("check the logs")]

    assert len(provider.calls) == 2
    assert all(stream.closed for stream in provider.streams)
    errors = [event for event in events if event.kind == "error"]
    assert [error.code for error in errors] == ["repetition_detected"]
    assert sum(1 for event in events if event.kind == "repetition_stop") == 2
    actions = [
        entry["action"]
        for entry in captured
        if entry.get("event") == "provider.repetition_detected"
    ]
    assert actions == ["continue", "stop"]


@pytest.mark.asyncio
async def test_tool_calls_from_a_cut_response_never_run() -> None:
    # A complete tool call streamed after the loop began, plus one that the
    # stream would only have reached after the trip point.
    events: list[Any] = [ProviderText(text=_LEAD)]
    events.extend(ProviderText(text=_LINE) for _ in range(150))
    events.append(ProviderToolUseStart(tool_use_id="early", tool_name="echo"))
    events.append(ProviderToolUseEnd(tool_use_id="early", tool_name="echo", arguments={}))
    events.extend(ProviderText(text=_LINE) for _ in range(3_000))
    events.append(ProviderToolUseStart(tool_use_id="late", tool_name="echo"))
    events.append(ProviderToolUseEnd(tool_use_id="late", tool_name="echo", arguments={}))
    events.append(ProviderDone(stop_reason="tool_use", input_tokens=10, output_tokens=40_000))
    provider = _Provider([events, _finish("Nothing else to do.")])
    calls: list[str] = []

    async def tool_handler(call: Any) -> ToolResult:
        calls.append(call.tool_use_id)
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="ok")

    agent = Agent(
        provider=provider,
        config=_config(),
        tool_definitions=[
            ToolDefinition(name="echo", description="Echo.", input_schema=ToolInputSchema())
        ],
        tool_handler=tool_handler,
    )

    out = [event async for event in agent.run_turn("check the logs")]

    assert calls == []
    started = [event.tool_use_id for event in out if event.kind == "tool_use_start"]
    assert started == ["early"]
    assert provider.streams[0].closed is True
    assert not any(event.kind == "tool_result" for event in out)
    done = next(event for event in out if event.kind == "done")
    assert done.text.endswith("Nothing else to do.")


@pytest.mark.asyncio
async def test_looping_reasoning_trips_on_its_own_channel() -> None:
    events: list[Any] = [ProviderThinking(text="Let me think. ")]
    events.extend(ProviderThinking(text="Wait, maybe the cache is stale. ") for _ in range(3_000))
    events.append(ProviderText(text="never reached"))
    events.append(ProviderDone(stop_reason="stop", input_tokens=10, output_tokens=30_000))
    provider = _Provider([events, _finish("The cache was stale.")])
    agent = Agent(provider=provider, config=_config())

    out = [event async for event in agent.run_turn("why is it slow?")]

    stop = next(event for event in out if event.kind == "repetition_stop")
    assert stop.channel == "reasoning"
    assert stop.removed_text == ""
    assert provider.streams[0].closed is True
    assert not any(event.kind == "text_delta" and event.text == "never reached" for event in out)
    done = next(event for event in out if event.kind == "done")
    assert done.text == "The cache was stale."


@pytest.mark.asyncio
async def test_disabled_guard_reads_the_whole_stream() -> None:
    provider = _Provider([_looping_text(copies=600), _finish()])
    agent = Agent(
        provider=provider,
        config=_config(
            repetition_guard=RepetitionGuardSettings(enabled=False),
            length_capped_continuations=1,
        ),
    )

    out = [event async for event in agent.run_turn("check the logs")]

    assert provider.streams[0].pulled == len(provider.streams[0].events)
    assert not any(event.kind == "repetition_stop" for event in out)
    assert not any(event.kind == "warning" and event.code == "repetition_detected" for event in out)


@pytest.mark.asyncio
async def test_long_legitimate_answer_streams_to_the_end() -> None:
    rows = "".join(
        f"| {i} | node-{i:04d} | {(i * 37) % 100} ms | healthy since build {1000 + i} |\n"
        for i in range(1, 1_200)
    )
    table = "| # | Node | Latency | Notes |\n|---|---|---|---|\n" + rows
    chunks = [table[i : i + 50] for i in range(0, len(table), 50)]
    events: list[Any] = [ProviderText(text=chunk) for chunk in chunks]
    events.append(ProviderDone(stop_reason="stop", input_tokens=10, output_tokens=20_000))
    provider = _Provider([events])
    agent = Agent(provider=provider, config=_config())

    out = [event async for event in agent.run_turn("list the nodes")]

    assert len(table) > 60_000
    assert provider.streams[0].pulled == len(events)
    assert not any(event.kind == "repetition_stop" for event in out)
    done = next(event for event in out if event.kind == "done")
    assert done.text == table


class _SelectorClone:
    def __init__(self, provider: _Provider) -> None:
        self.provider = provider
        self.current_config = SimpleNamespace(model="fake-model")

    def resolve(self) -> _Provider:
        return self.provider

    def override_model(self, model: str) -> None:
        self.current_config.model = model

    def next_fallback_after_failure(self, primary_failure: Exception) -> _Provider:
        raise IndexError("No fallback configured")


class _ProviderSelector:
    def __init__(self, provider: _Provider) -> None:
        self.provider = provider

    def clone(self) -> _SelectorClone:
        return _SelectorClone(self.provider)


async def _run_turn_runner(
    provider: _Provider,
    session_key: str,
    *,
    config: Any = None,
) -> tuple[list[Any], list[Any]]:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    await manager.create(session_key)
    runner = TurnRunner(
        provider_selector=_ProviderSelector(provider),
        session_manager=manager,
        config=config,
    )
    try:
        events = [
            event
            async for event in runner.run(
                "check the logs",
                session_key,
                ToolContext(caller_kind=CallerKind.CLI),
                history_has_persisted_user=False,
                no_memory_capture=True,
            )
        ]
        transcript = await manager.get_transcript(session_key)
    finally:
        await storage.close()
    return events, transcript


@pytest.mark.asyncio
async def test_turn_runner_persists_the_reply_without_the_loop() -> None:
    provider = _Provider([_looping_text(), _finish()])

    events, transcript = await _run_turn_runner(provider, "agent:main:repetition-continue")

    looping = provider.streams[0]
    assert looping.closed is True
    assert looping.pulled < len(looping.events) // 4
    assert not any(event.kind == "repetition_stop" for event in events)
    assert any(event.kind == "warning" and event.code == "repetition_detected" for event in events)
    replies = [entry.content for entry in transcript if entry.role == "assistant"]
    assert len(replies) == 1
    assert replies[0].count("check the configuration file again") == 1
    assert replies[0].endswith("All checks passed.")


@pytest.mark.asyncio
async def test_turn_runner_reads_guard_settings_from_config() -> None:
    from agentos.gateway.config import GatewayConfig

    config = GatewayConfig()
    config.llm.repetition_guard.enabled = False
    provider = _Provider([_looping_text(copies=600), _finish()])

    events, _ = await _run_turn_runner(provider, "agent:main:repetition-off", config=config)

    assert provider.streams[0].pulled == len(provider.streams[0].events)
    assert not any(
        event.kind == "warning" and event.code == "repetition_detected" for event in events
    )


@pytest.mark.asyncio
async def test_cut_response_counts_estimated_usage() -> None:
    from agentos.engine.usage import UsageTracker

    provider = _Provider([_looping_text(), _finish()])
    usage = UsageTracker()
    agent = Agent(
        provider=provider,
        config=_config(),
        usage_tracker=usage,
        session_key="agent:test:repetition-usage",
    )

    events = [event async for event in agent.run_turn("check the logs")]

    streamed = "".join(
        event.text
        for event in events[: next(i for i, e in enumerate(events) if e.kind == "repetition_stop")]
        if event.kind == "text_delta"
    )
    estimated_output = len(streamed) // 4
    done = next(event for event in events if event.kind == "done")
    # The finishing call reported 12 in / 5 out; the cut call is an estimate.
    assert done.output_tokens == estimated_output + 5
    assert done.input_tokens > 12
    tracked = usage.get("agent:test:repetition-usage")
    assert tracked is not None
    assert tracked.output_tokens == estimated_output + 5
    assert tracked.input_tokens == done.input_tokens


@pytest.mark.asyncio
async def test_cut_response_usage_counts_toward_turn_budgets() -> None:
    provider = _Provider([_looping_text(), _finish()])
    agent = Agent(provider=provider, config=_config(max_turn_output_tokens=1_000))

    events = [event async for event in agent.run_turn("check the logs")]

    # The cut call's estimated output (~4k tokens) is over budget, so the turn
    # stops instead of paying for a continuation.
    assert len(provider.calls) == 1
    errors = [event.code for event in events if event.kind == "error"]
    assert errors == ["turn_output_token_budget_exceeded"]


@pytest.mark.asyncio
async def test_reasoning_loop_usage_counts_reasoning_tokens() -> None:
    events: list[Any] = [
        ProviderThinking(text="Wait, maybe the cache is stale. ") for _ in range(3_000)
    ]
    events.append(ProviderDone(stop_reason="stop", input_tokens=10, output_tokens=30_000))
    provider = _Provider([events, _finish("The cache was stale.")])
    agent = Agent(provider=provider, config=_config())

    out = [event async for event in agent.run_turn("why is it slow?")]

    done = next(event for event in out if event.kind == "done")
    assert done.reasoning_tokens > 3_000
    assert done.output_tokens == done.reasoning_tokens + 5


@pytest.mark.asyncio
async def test_subagent_result_drops_the_cut_loop() -> None:
    from agentos.engine.subagent import SubagentManager, SubagentSpec

    provider = _Provider([_looping_text(), _finish()])
    manager = SubagentManager()

    handle = await manager.spawn(
        SubagentSpec(task="check the logs"),
        agent_factory=lambda spec, depth: Agent(provider=provider, config=_config()),
    )
    result = await handle.task

    assert result.count("check the configuration file again") == 1
    assert result.endswith("All checks passed.")
