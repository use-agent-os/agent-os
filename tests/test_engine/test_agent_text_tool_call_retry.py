"""Agent retries once when a reply writes a tool call as text and nothing runs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from agentos.engine import Agent, AgentConfig, ToolResult
from agentos.engine.turn_timing import TurnTiming
from agentos.provider import ChatConfig, Message, ToolDefinition, ToolInputSchema
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.provider import ToolUseEndEvent as ProviderToolUseEnd
from agentos.provider import ToolUseStartEvent as ProviderToolUseStart

WRITTEN_CALL = '```json\n{"name": "read_file", "arguments": {"path": "notes.txt"}}\n```'
NUDGE_MARKER = "tool-calling interface"


class _SequenceProvider:
    provider_name = "fake"

    def __init__(self, streams: list[list[Any]]) -> None:
        self.streams = streams
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        index = len(self.calls)
        self.calls.append({"messages": list(messages), "tools": tools})
        events = self.streams[index] if index < len(self.streams) else self.streams[-1]
        return self._stream(events)

    async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
        for event in events:
            yield event

    async def list_models(self) -> list[Any]:
        return []


class _CallLog:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def write(self, kind: str, payload: dict[str, Any]) -> None:
        self.records.append((kind, payload))

    def decisions(self) -> list[dict[str, Any]]:
        return [payload for kind, payload in self.records if kind == "turn_policy_decision"]


def _chunks(text: str, size: int = 7) -> list[ProviderText]:
    return [ProviderText(text=text[i : i + size]) for i in range(0, len(text), size)]


def _done(stop: str = "end_turn") -> ProviderDone:
    return ProviderDone(stop_reason=stop, input_tokens=5, output_tokens=3)


def _native_read_file() -> list[Any]:
    return [
        ProviderToolUseStart(tool_use_id="call-1", tool_name="read_file"),
        ProviderToolUseEnd(
            tool_use_id="call-1",
            tool_name="read_file",
            arguments={"path": "notes.txt"},
        ),
        _done("tool_use"),
    ]


def _agent(provider: _SequenceProvider, **config: Any) -> tuple[Agent, list[Any]]:
    executed: list[Any] = []

    async def tool_handler(call: Any) -> ToolResult:
        executed.append(call)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="notes: hello",
        )

    settings: dict[str, Any] = {
        "max_iterations": 4,
        "max_provider_retries": 1,
        "retry_base_backoff_ms": 0,
        "retry_max_backoff_ms": 0,
    }
    settings.update(config)
    agent = Agent(
        provider=provider,
        config=AgentConfig(**settings),
        tool_definitions=[
            ToolDefinition(
                name="read_file",
                description="Read a file.",
                input_schema=ToolInputSchema(
                    properties={"path": {"type": "string"}},
                    required=["path"],
                ),
            )
        ],
        tool_handler=tool_handler,
    )
    return agent, executed


def _streamed_text(events: list[Any]) -> str:
    return "".join(event.text for event in events if event.kind == "text_delta")


def _message_text(message: Message) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(getattr(block, "text", "") or "" for block in content)


@pytest.mark.asyncio
async def test_written_out_call_is_retried_once_and_the_tool_runs() -> None:
    provider = _SequenceProvider(
        [
            [*_chunks("Let me open it.\n\n" + WRITTEN_CALL), _done()],
            _native_read_file(),
            [ProviderText(text="The notes say hello."), _done()],
        ]
    )
    agent, executed = _agent(provider)
    call_log = _CallLog()
    agent._turn_call_logger = call_log  # type: ignore[assignment]
    timing = TurnTiming()
    agent.turn_timing = timing

    events = [event async for event in agent.run_turn("what is in notes.txt?")]

    assert len(provider.calls) == 3
    assert [call.tool_name for call in executed] == ["read_file"]
    # The written-out call never reached the user; the intent line did.
    streamed = _streamed_text(events)
    assert '"arguments"' not in streamed
    assert streamed == "Let me open it.\n\nThe notes say hello."
    done = next(event for event in events if event.kind == "done")
    assert '"arguments"' not in done.text
    assert done.text.endswith("The notes say hello.")
    warnings = [event.code for event in events if event.kind == "warning"]
    assert warnings == ["provider_text_tool_call_retry"]

    # The retried request carries the bad reply and the nudge, after the
    # original request unchanged.
    first, retried, after_tool = (call["messages"] for call in provider.calls)
    assert retried[: len(first)] == first
    assert [m.role for m in retried[len(first) :]] == ["assistant", "user"]
    assert '"read_file"' in _message_text(retried[-2])
    assert NUDGE_MARKER in _message_text(retried[-1])
    # Neither is kept in the conversation.
    assert not any(NUDGE_MARKER in _message_text(m) for m in after_tool)
    assert not any('"arguments"' in _message_text(m) for m in after_tool)

    decisions = [d for d in call_log.decisions() if d.get("action") == "retry_text_tool_call"]
    assert len(decisions) == 1
    assert decisions[0]["code"] == "text_tool_call"
    assert decisions[0]["tool_name"] == "read_file"
    assert decisions[0]["shape"] == "json_call"
    assert timing.summary()["retries"] == {"text_tool_call": 1}
    assert timing.log_fields()["retry_text_tool_call"] == 1


@pytest.mark.asyncio
async def test_second_miss_ends_the_turn_as_before() -> None:
    provider = _SequenceProvider(
        [
            [ProviderText(text=WRITTEN_CALL), _done()],
            [ProviderText(text=WRITTEN_CALL), _done()],
            [ProviderText(text="never requested"), _done()],
        ]
    )
    agent, executed = _agent(provider, max_provider_retries=3)

    events = [event async for event in agent.run_turn("read notes.txt")]

    assert len(provider.calls) == 2
    assert executed == []
    warnings = [event.code for event in events if event.kind == "warning"]
    assert warnings == ["provider_text_tool_call_retry"]
    assert not any(event.kind == "error" for event in events)
    # Only the second reply was shown, exactly as a reply is shown today.
    assert _streamed_text(events) == WRITTEN_CALL
    done = next(event for event in events if event.kind == "done")
    assert done.text == WRITTEN_CALL


@pytest.mark.asyncio
async def test_retry_budget_of_zero_disables_the_retry() -> None:
    provider = _SequenceProvider([[ProviderText(text=WRITTEN_CALL), _done()]])
    agent, _ = _agent(provider, max_provider_retries=0)

    events = [event async for event in agent.run_turn("read notes.txt")]

    assert len(provider.calls) == 1
    assert _streamed_text(events) == WRITTEN_CALL
    assert not any(event.kind == "warning" for event in events)


@pytest.mark.asyncio
async def test_turn_llm_call_budget_is_respected() -> None:
    provider = _SequenceProvider([[ProviderText(text=WRITTEN_CALL), _done()]])
    agent, _ = _agent(provider, max_turn_llm_calls=1)

    events = [event async for event in agent.run_turn("read notes.txt")]

    assert len(provider.calls) == 1
    assert not any(event.kind == "error" for event in events)
    assert _streamed_text(events) == WRITTEN_CALL


@pytest.mark.asyncio
async def test_retry_is_once_per_model_call_not_once_per_turn() -> None:
    provider = _SequenceProvider(
        [
            [ProviderText(text=WRITTEN_CALL), _done()],
            _native_read_file(),
            [ProviderText(text=WRITTEN_CALL), _done()],
            _native_read_file(),
            [ProviderText(text="Read it twice."), _done()],
        ]
    )
    agent, executed = _agent(provider, max_iterations=6)

    events = [event async for event in agent.run_turn("read notes.txt twice")]

    assert len(provider.calls) == 5
    assert len(executed) == 2
    warnings = [event.code for event in events if event.kind == "warning"]
    assert warnings == ["provider_text_tool_call_retry", "provider_text_tool_call_retry"]
    assert _streamed_text(events) == "Read it twice."


@pytest.mark.asyncio
async def test_prose_explaining_the_call_syntax_is_not_retried() -> None:
    reply = "To read a file you can send a request like this:\n\n" + WRITTEN_CALL
    provider = _SequenceProvider([[*_chunks(reply), _done()]])
    agent, _ = _agent(provider)

    events = [event async for event in agent.run_turn("how do I call read_file?")]

    assert len(provider.calls) == 1
    assert _streamed_text(events) == reply
    assert not any(event.kind == "warning" for event in events)


@pytest.mark.asyncio
async def test_native_call_after_held_text_keeps_the_text_in_order() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderText(text="Let me run read_file:\n"),
                *_native_read_file(),
            ],
            [ProviderText(text="Done."), _done()],
        ]
    )
    agent, executed = _agent(provider)

    events = [event async for event in agent.run_turn("read notes.txt")]

    assert len(provider.calls) == 2
    assert len(executed) == 1
    kinds = [
        (event.kind, getattr(event, "text", ""))
        for event in events
        if event.kind in {"text_delta", "tool_use_start"}
    ]
    assert kinds[0] == ("text_delta", "Let me run read_file:\n")
    assert kinds[1][0] == "tool_use_start"
    assert not any(event.kind == "warning" for event in events)


@pytest.mark.asyncio
async def test_text_promoted_to_a_call_by_the_provider_is_not_retried() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderText(text=WRITTEN_CALL),
                ProviderToolUseStart(
                    tool_use_id="call-1", tool_name="read_file", synthetic_from_text=True
                ),
                ProviderToolUseEnd(
                    tool_use_id="call-1",
                    tool_name="read_file",
                    arguments={"path": "notes.txt"},
                    synthetic_from_text=True,
                ),
                _done("tool_use"),
            ],
            [ProviderText(text="Done."), _done()],
        ]
    )
    agent, executed = _agent(provider)

    events = [event async for event in agent.run_turn("read notes.txt")]

    assert len(provider.calls) == 2
    assert len(executed) == 1
    assert not any(event.kind == "warning" for event in events)


@pytest.mark.asyncio
async def test_length_capped_reply_is_not_treated_as_a_written_call() -> None:
    provider = _SequenceProvider(
        [
            [ProviderText(text=WRITTEN_CALL), _done("max_tokens")],
            [ProviderText(text=" continued"), _done()],
        ]
    )
    agent, _ = _agent(provider)

    events = [event async for event in agent.run_turn("read notes.txt")]

    codes = [event.code for event in events if event.kind == "warning"]
    assert "provider_text_tool_call_retry" not in codes
    assert _streamed_text(events).startswith(WRITTEN_CALL)


# ── TurnRunner: what is persisted and logged ─────────────────────────────


class _OfferedToolProvider(_SequenceProvider):
    """First reply writes a call to whichever tool the runtime offered."""

    def __init__(self) -> None:
        super().__init__([])
        self.written_tool: str | None = None

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append({"messages": list(messages), "tools": tools})
        if len(self.calls) == 1:
            names = sorted(tool.name for tool in tools or [])
            assert names, "the runtime offered no tools"
            self.written_tool = "read_file" if "read_file" in names else names[0]
            reply = (
                f'Checking now.\n```json\n{{"name": "{self.written_tool}", "arguments": {{}}}}\n```'
            )
            return self._stream([*_chunks(reply), _done()])
        return self._stream([ProviderText(text="All done."), _done()])


class _Selector:
    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self.current_config = type("_Cfg", (), {"model": "fake-model"})()

    def clone(self) -> _Selector:
        return self

    def resolve(self) -> Any:
        return self.provider

    def override_model(self, model: str) -> None:
        self.current_config.model = model


@pytest.mark.asyncio
async def test_turn_runner_persists_only_what_the_user_saw(monkeypatch) -> None:
    from agentos.engine.runtime import TurnRunner
    from agentos.session.manager import SessionManager
    from agentos.session.storage import SessionStorage
    from agentos.tools.registry import ToolRegistry, ToolSpec
    from agentos.tools.types import CallerKind, ToolContext

    registry = ToolRegistry()

    async def lookup(query: str) -> str:
        return f"result for {query}"

    registry.register(
        ToolSpec(
            name="lookup",
            description="Look something up",
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        ),
        lookup,
    )
    entries: list[Any] = []
    monkeypatch.setattr("agentos.engine.runtime.write_decision_entry", entries.append)
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:text-tool-call"
    await manager.create(session_key)
    provider = _OfferedToolProvider()
    runner = TurnRunner(
        provider_selector=_Selector(provider), tool_registry=registry, session_manager=manager
    )

    try:
        events = [
            event
            async for event in runner.run(
                "check it",
                session_key,
                ToolContext(caller_kind=CallerKind.CLI),
                history_has_persisted_user=False,
                no_memory_capture=True,
            )
        ]
        transcript = await manager.get_transcript(session_key)
    finally:
        await storage.close()

    assert len(provider.calls) == 2
    assert _streamed_text(events) == "Checking now.\nAll done."
    assistant = [entry for entry in transcript if entry.role == "assistant"]
    assert len(assistant) == 1
    assert '"arguments"' not in assistant[0].content
    assert assistant[0].content.endswith("All done.")
    assert not any(NUDGE_MARKER in (entry.content or "") for entry in transcript)
    assert len(entries) == 1
    assert entries[0].timing["retries"] == {"text_tool_call": 1}
