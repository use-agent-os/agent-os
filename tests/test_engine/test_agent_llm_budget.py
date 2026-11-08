from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine import (
    Agent,
    AgentConfig,
    AgentState,
    DoneEvent,
    ErrorEvent,
    RunHeartbeatEvent,
    TextDeltaEvent,
    ToolCall,
    ToolResult,
    WarningEvent,
)
from agentos.engine.runtime import TurnRunner
from agentos.engine.session_sanitize import session_payload_chars
from agentos.provider import (
    ChatConfig,
    Message,
    ProviderHeartbeatEvent,
    ToolDefinition,
    ToolInputSchema,
)
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import ErrorEvent as ProviderError
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.provider import ToolUseDeltaEvent as ProviderToolUseDelta
from agentos.provider import ToolUseEndEvent as ProviderToolUseEnd
from agentos.provider import ToolUseStartEvent as ProviderToolUseStart
from agentos.provider.request_proof import (
    ProviderRequestBudgetExceeded,
    prove_provider_payload,
)

RAW_CURRENT_TURN_OVERFLOW_MESSAGE = (
    "Context overflow is in the current turn's recent tool calls or "
    "reasoning tail; history compaction cannot reduce it."
)


class _StallingProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []
        self.stream_closed = False

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        try:
            await asyncio.sleep(60.0)
            yield ProviderText(text="late")
        finally:
            self.stream_closed = True

    async def list_models(self) -> list[Any]:
        return []


class _ActiveLongToolArgumentProvider:
    provider_name = "fake"

    def __init__(
        self,
        *,
        fragment_delay: float = 0.02,
        content: str = "alpha\\nbeta\\ngamma\\n",
    ) -> None:
        self.fragment_delay = fragment_delay
        self.content = content
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number > 1:
            yield ProviderText(text="done")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        tool_use_id = "tool-1"
        yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="write_file")
        fragments = [
            '{"path":"deck.py","content":"',
            self.content,
            '"}',
        ]
        for fragment in fragments:
            await asyncio.sleep(self.fragment_delay)
            yield ProviderToolUseDelta(tool_use_id=tool_use_id, json_fragment=fragment)
        yield ProviderToolUseEnd(
            tool_use_id=tool_use_id,
            tool_name="write_file",
            arguments={},
        )
        yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=100)

    async def list_models(self) -> list[Any]:
        return []


class _ContextOverflowProvider:
    provider_name = "fake"

    def __init__(self, *, success_after: int | None = None) -> None:
        self.success_after = success_after
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if self.success_after is not None and call_number > self.success_after:
            yield ProviderText(text="ok")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        yield ProviderError(message="context length exceeded", code="400")

    async def list_models(self) -> list[Any]:
        return []


class _ProviderRequestBudgetExceededProvider:
    provider_name = "openrouter"

    def __init__(
        self,
        *,
        success_after: int | None = None,
        proof: dict[str, Any] | None = None,
    ) -> None:
        self.success_after = success_after
        self.proof = proof
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if self.success_after is not None and call_number > self.success_after:
            yield ProviderText(text="ok")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        message = (
            '{"fallback_reason":"provider_request_budget_exhausted"}'
            if self.proof is None
            else json.dumps(self.proof)
        )
        yield ProviderError(message=message, code="provider_request_budget_exhausted")

    async def list_models(self) -> list[Any]:
        return []


class _RepeatedToolFailureThenDoneProvider:
    provider_name = "fake"

    def __init__(self, *, tool_retries: int = 3) -> None:
        self.tool_retries = tool_retries
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number > self.tool_retries:
            yield ProviderText(text="handled")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        tool_use_id = f"cmd-{call_number}"
        yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="exec_command")
        yield ProviderToolUseEnd(
            tool_use_id=tool_use_id,
            tool_name="exec_command",
            arguments={"command": "python build_pptx.py", "timeout": 30},
        )
        yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _HighUsageToolLoopProvider:
    provider_name = "fake"

    def __init__(self, *, tool_rounds: int = 3, input_tokens_per_call: int = 4000) -> None:
        self.tool_rounds = tool_rounds
        self.input_tokens_per_call = input_tokens_per_call
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number > self.tool_rounds:
            yield ProviderText(text="done")
            yield ProviderDone(
                stop_reason="stop",
                input_tokens=self.input_tokens_per_call,
                output_tokens=0,
            )
            return
        tool_use_id = f"read-{call_number}"
        yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="exec_command")
        yield ProviderToolUseEnd(
            tool_use_id=tool_use_id,
            tool_name="exec_command",
            arguments={"command": f"printf round-{call_number}"},
        )
        yield ProviderDone(
            stop_reason="tool_calls",
            input_tokens=self.input_tokens_per_call,
            output_tokens=0,
        )

    async def list_models(self) -> list[Any]:
        return []


class _ConfigCapturingProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.configs: list[ChatConfig | None] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.configs.append(config)
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderText(text="ok")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _CompactingErrorSessionManager:
    def __init__(self, *, compact_raises: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.compact_raises = compact_raises

    async def compact(self, session_key: str, budget: int, config: Any | None = None) -> str:
        self.calls.append(("compact", session_key))
        assert budget > 0
        if self.compact_raises:
            raise RuntimeError("compact failed")
        return "[summary]"

    async def append_message(self, session_key: str, **kwargs: Any) -> None:
        self.calls.append(("append", session_key))
        assert kwargs["role"] == "system"
        assert kwargs["content"].startswith("Error: ")


@pytest.mark.asyncio
async def test_turn_error_persist_records_current_turn_exhaustion_without_compacting() -> None:
    session_manager = _CompactingErrorSessionManager()
    runner = TurnRunner(
        provider_selector=None,
        session_manager=session_manager,
        config=SimpleNamespace(context_budget_tokens=96_000),
    )

    await runner._persist_turn_error(
        "agent:main:webchat:test",
        ErrorEvent(
            message="Context overflow is in the current turn's recent tool calls.",
            code="current_turn_context_exhausted",
        ),
    )

    assert session_manager.calls == [("append", "agent:main:webchat:test")]


@pytest.mark.asyncio
async def test_turn_error_persist_skips_error_time_compaction_for_exhaustion() -> None:
    session_manager = _CompactingErrorSessionManager(compact_raises=True)
    runner = TurnRunner(
        provider_selector=None,
        session_manager=session_manager,
        config=SimpleNamespace(context_budget_tokens=96_000),
    )

    await runner._persist_turn_error(
        "agent:main:webchat:test",
        ErrorEvent(
            message="Context overflow is in the current turn's recent tool calls.",
            code="current_turn_context_exhausted",
        ),
    )

    assert session_manager.calls == [("append", "agent:main:webchat:test")]


@pytest.mark.asyncio
async def test_agent_blocks_repeated_identical_tool_failures_before_tail_growth() -> None:
    calls = 0

    async def _failing_tool(call: Any) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="write failed: " + ("permission denied " * 200),
            is_error=True,
        )

    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        config=AgentConfig(
            tool_failure_loop_block_threshold=3,
        ),
        tool_handler=_failing_tool,
    )
    tool_call = ToolCall(
        tool_use_id="write-1",
        tool_name="write_file",
        arguments={"path": "index.html", "content": "<html>bad</html>"},
    )

    first = await agent._execute_tool(tool_call)
    second = await agent._execute_tool(tool_call)
    third = await agent._execute_tool(tool_call)

    assert first.is_error is True
    assert second.is_error is True
    assert third.is_error is True
    assert calls == 2
    assert "tool_failure_loop_exhausted" not in third.content
    assert "Do not retry this exact call unchanged" in third.content
    assert len(third.content) < len(second.content)
    assert third.execution_status is not None
    assert third.execution_status.get("reason") == "tool_failure_loop_exhausted"


@pytest.mark.asyncio
async def test_agent_tool_failure_loop_allows_changed_arguments() -> None:
    calls = 0

    async def _failing_tool(call: Any) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="write failed",
            is_error=True,
        )

    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        config=AgentConfig(tool_failure_loop_block_threshold=3),
        tool_handler=_failing_tool,
    )

    await agent._execute_tool(
        ToolCall(
            tool_use_id="write-1",
            tool_name="write_file",
            arguments={"path": "index.html", "content": "first"},
        )
    )
    await agent._execute_tool(
        ToolCall(
            tool_use_id="write-2",
            tool_name="write_file",
            arguments={"path": "index.html", "content": "first"},
        )
    )
    changed = await agent._execute_tool(
        ToolCall(
            tool_use_id="write-3",
            tool_name="write_file",
            arguments={"path": "index.html", "content": "changed"},
        )
    )

    assert calls == 3
    assert changed.content == "write failed"


@pytest.mark.asyncio
async def test_agent_tool_failure_loop_result_returns_to_model_instead_of_terminal_error() -> None:
    provider = _RepeatedToolFailureThenDoneProvider(tool_retries=3)
    handler_calls = 0

    async def _failing_tool(call: Any) -> ToolResult:
        nonlocal handler_calls
        handler_calls += 1
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="syntax error",
            is_error=True,
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            tool_failure_loop_block_threshold=3,
            max_iterations=5,
        ),
        tool_handler=_failing_tool,
    )

    events = [event async for event in agent.run_turn("build the deck")]

    assert handler_calls == 2
    assert len(provider.calls) == 4
    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(
        isinstance(event, ErrorEvent)
        and getattr(event, "code", None) == "tool_failure_loop_exhausted"
        for event in events
    )
    assert any(
        getattr(event, "kind", None) == "tool_result"
        and (getattr(event, "execution_status", None) or {}).get("reason")
        == "tool_failure_loop_exhausted"
        for event in events
    )


@pytest.mark.asyncio
async def test_agent_tool_failure_loop_resets_after_successful_state_change() -> None:
    calls: list[str] = []

    async def _tool(call: Any) -> ToolResult:
        calls.append(call.tool_name)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="ok" if call.tool_name == "edit_file" else "syntax error",
            is_error=call.tool_name != "edit_file",
        )

    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        config=AgentConfig(tool_failure_loop_block_threshold=3),
        tool_handler=_tool,
    )
    command_call = ToolCall(
        tool_use_id="cmd-1",
        tool_name="exec_command",
        arguments={"command": "python build_pptx.py", "timeout": 30},
    )

    await agent._execute_tool(command_call)
    await agent._execute_tool(
        ToolCall(
            tool_use_id="cmd-2",
            tool_name="exec_command",
            arguments=command_call.arguments,
        )
    )
    await agent._execute_tool(
        ToolCall(
            tool_use_id="edit-1",
            tool_name="edit_file",
            arguments={"path": "build_pptx.py", "old_text": "bad", "new_text": "good"},
        )
    )
    retry_after_edit = await agent._execute_tool(
        ToolCall(
            tool_use_id="cmd-3",
            tool_name="exec_command",
            arguments=command_call.arguments,
        )
    )

    assert calls == ["exec_command", "exec_command", "edit_file", "exec_command"]
    assert retry_after_edit.content == "syntax error"
    assert retry_after_edit.execution_status is None


@pytest.mark.asyncio
async def test_agent_blocks_repeated_missing_tool_handler_failures() -> None:
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        config=AgentConfig(tool_failure_loop_block_threshold=3),
    )
    tool_call = ToolCall(
        tool_use_id="missing-1",
        tool_name="missing_tool",
        arguments={"value": "same"},
    )

    await agent._execute_tool(tool_call)
    await agent._execute_tool(tool_call)
    third = await agent._execute_tool(tool_call)

    assert "tool_failure_loop_exhausted" not in third.content
    assert "Do not retry this exact call unchanged" in third.content
    assert third.execution_status is not None
    assert third.execution_status.get("reason") == "tool_failure_loop_exhausted"


@pytest.mark.asyncio
async def test_agent_provider_request_proof_budget_is_separate_from_tool_result_cap() -> None:
    provider = _ConfigCapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=200_000,
            max_tokens=8192,
            tool_result_provider_request_max_chars=96_000,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    assert provider.configs
    assert provider.configs[0] is not None
    assert provider.configs[0].provider_request_max_chars > 96_000


@pytest.mark.asyncio
async def test_agent_provider_request_proof_budget_accepts_explicit_override() -> None:
    provider = _ConfigCapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            provider_request_proof_max_chars=123_456,
            tool_result_provider_request_max_chars=96_000,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    assert provider.configs
    assert provider.configs[0] is not None
    assert provider.configs[0].provider_request_max_chars == 123_456


class _BudgetCheckingProvider:
    provider_name = "openrouter"

    def __init__(self, *, proof_budget: int) -> None:
        self.proof_budget = proof_budget
        self.calls: list[list[Message]] = []
        self.proofs: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(messages)

    async def _stream(self, messages: list[Message]) -> AsyncIterator[Any]:
        payload = {"messages": [message.model_dump(mode="json") for message in messages]}
        try:
            proof = prove_provider_payload(
                payload,
                projection_adapter="openrouter",
                proof_budget=self.proof_budget,
            )
        except ProviderRequestBudgetExceeded as exc:
            self.proofs.append(exc.proof)
            yield ProviderError(
                message=json.dumps(exc.proof),
                code="provider_request_budget_exhausted",
            )
            return

        self.proofs.append(proof)
        yield ProviderText(text="ok")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _ProviderRaisesTimeout:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        raise TimeoutError("provider transport timeout")
        yield ProviderText(text="unreachable")

    async def list_models(self) -> list[Any]:
        return []


class _ProviderHeartbeatThenText:
    provider_name = "fake"

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderHeartbeatEvent(phase="llm_fallback", message="retrying")
        yield ProviderText(text="ok")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _ToolUseProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderToolUseStart(tool_use_id="tool-1", tool_name="slow")
        yield ProviderToolUseEnd(
            tool_use_id="tool-1",
            tool_name="slow",
            arguments={},
        )
        yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_provider_heartbeat_reaches_agent_stream() -> None:
    agent = Agent(
        provider=_ProviderHeartbeatThenText(),
        config=AgentConfig(iteration_timeout=30.0, timeout=60.0, max_provider_retries=0),
    )

    events = [event async for event in agent.run_turn("hello")]

    heartbeat_index = _event_index(
        events,
        lambda event: (
            isinstance(event, RunHeartbeatEvent)
            and event.phase == "llm_fallback"
            and event.message == "retrying"
        ),
    )
    text_index = _event_index(
        events,
        lambda event: (
            getattr(event, "kind", None) == "text_delta" and getattr(event, "text", None) == "ok"
        ),
    )
    assert heartbeat_index < text_index


@pytest.mark.asyncio
async def test_iteration_timeout_interrupts_stalled_provider_stream() -> None:
    provider = _StallingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(iteration_timeout=0.01, max_provider_retries=0),
    )

    events = await asyncio.wait_for(
        _collect_events(agent.run_turn("hello")),
        timeout=0.5,
    )

    error_index = _event_index(
        events,
        lambda event: isinstance(event, ErrorEvent) and event.code == "iteration_timeout",
    )
    state_index = _event_index(
        events,
        lambda event: (
            getattr(event, "kind", None) == "state_change"
            and getattr(event, "to_state", None) == AgentState.ERROR
        ),
    )
    assert state_index < error_index
    assert len(provider.calls) == 1
    assert provider.stream_closed is True
    # One DoneEvent still ends the turn, after the timeout error.
    assert isinstance(events[-1], DoneEvent)
    assert sum(isinstance(event, DoneEvent) for event in events) == 1
    assert events[-1].outcome.kind == "interrupted"


@pytest.mark.asyncio
async def test_iteration_timeout_does_not_interrupt_active_tool_argument_stream() -> None:
    async def write_file_tool(call: object) -> ToolResult:
        return ToolResult(
            tool_use_id=getattr(call, "tool_use_id"),
            tool_name=getattr(call, "tool_name"),
            content="written",
        )

    provider = _ActiveLongToolArgumentProvider(fragment_delay=0.02)
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            iteration_timeout=0.03,
            timeout=1.0,
            max_provider_retries=0,
        ),
        tool_definitions=[
            ToolDefinition(
                name="write_file",
                description="Write a file.",
                input_schema=ToolInputSchema(),
            )
        ],
        tool_handler=write_file_tool,
    )

    events = await asyncio.wait_for(_collect_events(agent.run_turn("hello")), timeout=1.0)

    assert len(provider.calls) == 2
    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(
        isinstance(event, ErrorEvent) and event.code == "iteration_timeout" for event in events
    )


@pytest.mark.asyncio
async def test_large_tool_argument_stream_emits_progress_heartbeat() -> None:
    async def write_file_tool(call: object) -> ToolResult:
        return ToolResult(
            tool_use_id=getattr(call, "tool_use_id"),
            tool_name=getattr(call, "tool_name"),
            content="written",
        )

    provider = _ActiveLongToolArgumentProvider(
        fragment_delay=0.0,
        content="x" * 5000,
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(iteration_timeout=1.0, timeout=2.0, max_provider_retries=0),
        tool_definitions=[
            ToolDefinition(
                name="write_file",
                description="Write a file.",
                input_schema=ToolInputSchema(),
            )
        ],
        tool_handler=write_file_tool,
    )

    events = await asyncio.wait_for(_collect_events(agent.run_turn("hello")), timeout=1.0)

    heartbeat_index = _event_index(
        events,
        lambda event: (
            isinstance(event, RunHeartbeatEvent)
            and event.phase == "llm_tool_arguments"
            and "write_file" in (event.message or "")
        ),
    )
    done_index = _event_index(events, lambda event: isinstance(event, DoneEvent))
    assert heartbeat_index < done_index


@pytest.mark.asyncio
async def test_iteration_timeout_caps_tool_execution() -> None:
    async def slow_tool(call: object) -> ToolResult:
        await asyncio.sleep(0.5)
        return ToolResult(
            tool_use_id=getattr(call, "tool_use_id"),
            tool_name=getattr(call, "tool_name"),
            content="late",
        )

    agent = Agent(
        provider=_ToolUseProvider(),
        config=AgentConfig(
            iteration_timeout=0.05,
            timeout=1.0,
            tool_timeout=5.0,
            max_provider_retries=0,
        ),
        tool_definitions=[
            ToolDefinition(
                name="slow",
                description="Slow tool.",
                input_schema=ToolInputSchema(),
            )
        ],
        tool_handler=slow_tool,
    )

    # The outer wait_for is a hang guard, not the assertion. It used to be
    # 0.25s -- below the 0.5s the tool itself sleeps -- so on a loaded runner
    # any scheduling slippage on the way to delivering the iteration_timeout
    # event tripped the guard first, and a working implementation failed with
    # a bare TimeoutError. What proves the behaviour is the ErrorEvent
    # assertion below, bounded by the turn budget above (timeout=1.0): if the
    # iteration timeout never fires, the turn ends on that budget instead and
    # the assertion fails on its own, well inside this guard.
    events = await asyncio.wait_for(_collect_events(agent.run_turn("hello")), timeout=5.0)

    assert any(
        isinstance(event, ErrorEvent) and event.code == "iteration_timeout" for event in events
    )


@pytest.mark.asyncio
async def test_provider_timeout_error_is_not_reclassified_as_iteration_timeout() -> None:
    provider = _ProviderRaisesTimeout()
    agent = Agent(
        provider=provider,
        config=AgentConfig(iteration_timeout=30.0, timeout=60.0, max_provider_retries=0),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    assert any(
        isinstance(event, ErrorEvent) and event.code == "agent_runtime_timeout" for event in events
    )
    assert not any(
        isinstance(event, ErrorEvent) and event.code == "iteration_timeout" for event in events
    )


def _patch_summarizer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    summary: str = "short summary",
    calls: list[dict[str, Any]] | None = None,
    error: Exception | None = None,
) -> None:
    """Stub the compaction service's provider-backed summarizer."""

    async def _summarize(
        provider: Any,
        messages: list[Message],
        previous_summary: str | None,
        *,
        window_tokens: int,
        timeout: float,
    ) -> tuple[str, str]:
        del provider, timeout
        if calls is not None:
            calls.append(
                {
                    "messages": list(messages),
                    "previous_summary": previous_summary,
                    "window_tokens": window_tokens,
                }
            )
        if error is not None:
            raise error
        return summary, "llm"

    monkeypatch.setattr("agentos.engine.compaction_service.summarize_messages", _summarize)


@pytest.mark.asyncio
async def test_context_overflow_noop_compaction_does_not_resend_unchanged_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing before the current turn: compaction cannot shrink the request."""
    calls: list[dict[str, Any]] = []
    _patch_summarizer(monkeypatch, calls=calls)
    provider = _ContextOverflowProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=0,
            max_overflow_retries=2,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    assert calls == []
    assert any(
        isinstance(event, ErrorEvent) and event.code == "compaction_not_smaller" for event in events
    )
    assert not any(getattr(event, "kind", None) == "compaction" for event in events)


@pytest.mark.asyncio
async def test_context_overflow_summary_only_larger_payload_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_summarizer(monkeypatch, summary="summary that is longer than the history " * 200)
    provider = _ContextOverflowProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=0,
            max_overflow_retries=2,
        ),
    )
    agent.set_history([Message(role="user", content="q"), Message(role="assistant", content="a")])

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    assert any(
        isinstance(event, ErrorEvent) and event.code == "compaction_not_smaller" for event in events
    )
    assert not any(getattr(event, "kind", None) == "compaction" for event in events)


@pytest.mark.asyncio
async def test_context_overflow_effective_compaction_allows_single_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_summarizer(monkeypatch)
    provider = _ContextOverflowProvider(success_after=1)
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=0,
            max_overflow_retries=2,
        ),
    )
    agent.set_history(list(_LARGE_HISTORY))

    events = [event async for event in agent.run_turn("x" * 4000)]

    assert len(provider.calls) == 2
    assert _provider_payload_is_smaller(provider.calls[0], provider.calls[1])
    # One summary exchange, then the current turn verbatim.
    retried = provider.calls[1]
    assert retried[0].content == "[Context summary]\nshort summary"
    assert retried[1].content == "Understood. Continuing from summary."
    assert str(retried[2].content).startswith("x" * 4000)
    assert any(event.kind == "done" and getattr(event, "text", "") == "ok" for event in events)


@pytest.mark.asyncio
async def test_inline_overflow_uses_live_context_not_cumulative_provider_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _HighUsageToolLoopProvider(tool_rounds=3, input_tokens_per_call=4000)
    summarize_calls: list[dict[str, Any]] = []

    async def _tool(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=20_000,
            context_overflow_threshold=0.5,
            max_iterations=10,
        ),
        tool_handler=_tool,
    )
    _patch_summarizer(monkeypatch, calls=summarize_calls)

    events = [event async for event in agent.run_turn("read the files one by one")]

    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.text == "done"
    assert done.input_tokens == 16_000
    assert len(provider.calls) == 4
    assert summarize_calls == []


@pytest.mark.asyncio
async def test_inline_overflow_still_triggers_for_large_live_provider_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _HighUsageToolLoopProvider(tool_rounds=0, input_tokens_per_call=1)
    large_tool = ToolDefinition(
        name="large_context_tool",
        description="large live request surface " + ("z" * 2000),
        input_schema=ToolInputSchema(),
    )
    summarize_calls: list[dict[str, Any]] = []

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=12_000,
            context_overflow_threshold=0.5,
            system_prompt="live request system context " + ("s" * 1000),
        ),
        tool_definitions=[large_tool],
    )
    earlier = [
        Message(role="user", content="earlier question " + "q" * 3000),
        Message(role="assistant", content="earlier answer " + "a" * 3000),
    ]
    agent.set_history(list(earlier))
    _patch_summarizer(monkeypatch, calls=summarize_calls)

    events = [event async for event in agent.run_turn("hello")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert len(provider.calls) == 1
    # The check runs before the call over the whole live request: the history
    # alone is under the limit, the system prompt and tools push it over.
    # This turn's input is not folded in.
    assert len(summarize_calls) == 1
    assert summarize_calls[0]["messages"] == earlier
    sent = provider.calls[0]
    assert sent[0].content == "[Context summary]\nshort summary"
    assert str(sent[-1].content).startswith("hello")


class _TextThenToolProvider:
    """Streams visible text plus a tool call, then a plain final answer."""

    provider_name = "fake"

    def __init__(self, *, tool_rounds: int = 1) -> None:
        self.tool_rounds = tool_rounds
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(list(messages))
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number > self.tool_rounds:
            yield ProviderText(text="done")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text=f"reply-{call_number}")
        tool_use_id = f"read-{call_number}"
        yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="exec_command")
        yield ProviderToolUseEnd(
            tool_use_id=tool_use_id,
            tool_name="exec_command",
            arguments={"command": f"printf round-{call_number}"},
        )
        yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


async def _ok_tool(call: Any) -> ToolResult:
    return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="ok")


_LARGE_HISTORY = [
    Message(role="user", content="old question " + ("h" * 8000)),
    Message(role="assistant", content="old answer " + ("a" * 8000)),
]


def _overflowing_agent(provider: Any, *, max_overflow_retries: int, **config: Any) -> Agent:
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=2000,
            context_overflow_threshold=0.5,
            max_provider_retries=0,
            max_overflow_retries=max_overflow_retries,
            max_iterations=10,
            **config,
        ),
        tool_handler=_ok_tool,
    )
    agent.set_history(list(_LARGE_HISTORY))
    return agent


@pytest.mark.asyncio
async def test_failed_overflow_compaction_never_replays_a_streamed_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#3644: the overflow check used to run after the response had streamed.

    A failed compaction then re-called the provider for the same iteration, so
    the consumer saw the first reply's text and tool_use twice.
    """
    compact_calls: list[dict[str, Any]] = []
    # A raising summarizer now falls back to the digest; an empty summary is
    # what still fails a compaction.
    _patch_summarizer(monkeypatch, calls=compact_calls, summary="")
    provider = _TextThenToolProvider(tool_rounds=1)
    agent = _overflowing_agent(provider, max_overflow_retries=2)

    events = [event async for event in agent.run_turn("read the file")]

    texts = [event.text for event in events if isinstance(event, TextDeltaEvent)]
    tool_starts = [event for event in events if getattr(event, "kind", None) == "tool_use_start"]
    assert len(provider.calls) == 2
    assert texts == ["reply-1", "done"]
    assert len(tool_starts) == 1
    assert len(compact_calls) == 2
    assert not any(isinstance(event, ErrorEvent) for event in events)
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert "done" in done.text


@pytest.mark.asyncio
async def test_overflow_compaction_runs_before_the_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.engine.types import CompactionEvent

    _patch_summarizer(monkeypatch)
    prompt_states: list[list[Message]] = []

    def _record_prompt_state(*, messages: list[Message], **_kwargs: Any) -> None:
        prompt_states.append(list(messages))
        return None

    monkeypatch.setattr("agentos.engine.agent.record_prompt_state", _record_prompt_state)
    monkeypatch.setattr(
        "agentos.engine.compaction_service.notify_compaction", lambda *_a, **_k: None
    )
    provider = _TextThenToolProvider(tool_rounds=0)
    agent = _overflowing_agent(provider, max_overflow_retries=2)
    agent._session_key = "agent:main:preflight"

    events = [event async for event in agent.run_turn("read the file")]

    assert len(provider.calls) == 1
    sent = [m.content for m in provider.calls[0] if isinstance(m.content, str)]
    assert sent[:2] == ["[Context summary]\nshort summary", "Understood. Continuing from summary."]
    assert sent[-1].startswith("read the file")
    assert not any("h" * 8000 in content for content in sent)
    # The cache-break monitor's baseline is the compacted request that was sent.
    assert prompt_states == [provider.calls[0]]
    compaction_index = _event_index(events, lambda event: isinstance(event, CompactionEvent))
    text_index = _event_index(events, lambda event: isinstance(event, TextDeltaEvent))
    assert compaction_index < text_index
    compaction = events[compaction_index]
    assert compaction.removed_count == 2
    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == ["done"]
    # The turn's structured record replaces the transcript (reset mode).
    record = agent.transcript_record()
    assert record is not None
    assert record["mode"] == "reset"
    assert record["compaction_id"] == compaction.compaction_id
    assert record["messages"][0]["content"] == "[Context summary]\nshort summary"


@pytest.mark.asyncio
async def test_overflow_compaction_keeps_unseen_tool_results_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summarize_calls: list[dict[str, Any]] = []
    _patch_summarizer(monkeypatch, calls=summarize_calls)
    provider = _TextThenToolProvider(tool_rounds=1)
    # Only the second call overflows: the first sees the small history alone.
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=2000,
            context_overflow_threshold=0.5,
            max_provider_retries=0,
            max_overflow_retries=2,
        ),
        tool_handler=_big_tool,
    )
    history = [
        Message(role="user", content="q" * 1200),
        Message(role="assistant", content="a" * 1200),
    ]
    agent.set_history(list(history))

    events = [event async for event in agent.run_turn("read the file")]

    assert len(provider.calls) == 2
    assert len(summarize_calls) == 1
    # Compaction summarizes everything before the current turn; the current
    # turn (its input and the new tool round) is kept verbatim.
    assert summarize_calls[0]["messages"] == history
    second = provider.calls[1]
    assert str(second[2].content).startswith("read the file")
    tool_results = [
        block
        for message in second
        if isinstance(message.content, list)
        for block in message.content
        if getattr(block, "type", None) == "tool_result"
    ]
    # Still a structured tool_result, not the 200-char text flattening.
    assert [block.tool_use_id for block in tool_results] == ["read-1"]
    assert "r" * 1000 in str(tool_results[0].content)
    assert any(isinstance(event, DoneEvent) for event in events)


@pytest.mark.asyncio
async def test_preflight_never_summarizes_history_for_a_turn_too_large_on_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.engine.types import CompactionEvent

    summarize_calls: list[dict[str, Any]] = []
    _patch_summarizer(monkeypatch, calls=summarize_calls)
    provider = _TextThenToolProvider(tool_rounds=0)
    agent = _overflowing_agent(provider, max_overflow_retries=2)

    events = [event async for event in agent.run_turn("p" * 20_000)]

    assert summarize_calls == []
    assert not any(isinstance(event, CompactionEvent) for event in events)
    # The request goes out untouched; the provider has the final word.
    assert provider.calls[0][: len(_LARGE_HISTORY)] == _LARGE_HISTORY
    assert agent.transcript_record()["mode"] == "append"


async def _big_tool(call: Any) -> ToolResult:
    return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="r" * 1500)


@pytest.mark.asyncio
@pytest.mark.parametrize(("max_overflow_retries", "expected_calls"), [(0, 0), (1, 1)])
async def test_exhausted_overflow_retries_stop_before_the_provider_call(
    monkeypatch: pytest.MonkeyPatch,
    max_overflow_retries: int,
    expected_calls: int,
) -> None:
    _patch_summarizer(monkeypatch, summary="")
    provider = _TextThenToolProvider(tool_rounds=3)
    agent = _overflowing_agent(provider, max_overflow_retries=max_overflow_retries)

    events = [event async for event in agent.run_turn("read the file")]

    assert len(provider.calls) == expected_calls
    error_index = _event_index(events, lambda event: isinstance(event, ErrorEvent))
    assert events[error_index].code == "compaction_refused_empty_summary"
    # Nothing streams after the terminal error, and nothing streamed twice.
    assert not any(isinstance(event, TextDeltaEvent) for event in events[error_index:])
    texts = [event.text for event in events if isinstance(event, TextDeltaEvent)]
    assert texts == [f"reply-{n}" for n in range(1, expected_calls + 1)]


@pytest.mark.asyncio
async def test_provider_request_budget_exhausted_compacts_warns_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compaction_events: list[tuple[str, dict[str, Any]]] = []
    _patch_summarizer(monkeypatch)
    monkeypatch.setattr(
        "agentos.engine.compaction_service.notify_compaction",
        lambda session_key, **payload: compaction_events.append((session_key, payload)),
    )
    provider = _ProviderRequestBudgetExceededProvider(success_after=1)
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=0,
            max_overflow_retries=2,
        ),
        session_key="agent:main:budget",
    )
    agent.set_history(list(_LARGE_HISTORY))

    events = [event async for event in agent.run_turn("x" * 4000)]
    warning_codes = [event.code for event in events if isinstance(event, WarningEvent)]

    assert len(provider.calls) == 2
    assert _provider_payload_is_smaller(provider.calls[0], provider.calls[1])
    assert warning_codes == [
        "context_auto_compaction_start",
        "context_auto_compaction_retry",
    ]
    assert any(event.kind == "done" and getattr(event, "text", "") == "ok" for event in events)
    assert not any(
        isinstance(event, ErrorEvent) and event.code == "provider_request_budget_exhausted"
        for event in events
    )
    assert [(key, payload["status"]) for key, payload in compaction_events] == [
        ("agent:main:budget", "started"),
        ("agent:main:budget", "observed"),
        ("agent:main:budget", "observed"),
    ]
    compaction_ids = {payload.get("compaction_id") for _, payload in compaction_events}
    assert len(compaction_ids) == 1
    assert None not in compaction_ids
    assert [payload["event"] for _, payload in compaction_events] == [
        "compaction.triggered",
        "compaction.chunk_summarized",
        "compaction.summary_verified",
    ]


@pytest.mark.asyncio
async def test_provider_request_budget_uses_provider_window_for_compaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summarize_calls: list[dict[str, Any]] = []
    _patch_summarizer(monkeypatch, calls=summarize_calls)
    provider = _ProviderRequestBudgetExceededProvider(
        success_after=1,
        proof={
            "fallback_reason": "provider_request_budget_exhausted",
            "estimated_chars": 109_055,
            "estimated_tokens": 27_263,
            "proof_budget": 96_000,
            "recent_tail_too_large": True,
        },
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=1_048_576,
            max_provider_retries=0,
            max_overflow_retries=2,
        ),
    )
    agent.set_history(list(_LARGE_HISTORY))

    events = [event async for event in agent.run_turn("x" * 4000)]

    assert summarize_calls
    assert summarize_calls[0]["window_tokens"] < 100_000
    assert len(provider.calls) == 2
    assert any(event.kind == "done" and getattr(event, "text", "") == "ok" for event in events)


@pytest.mark.asyncio
async def test_provider_request_budget_retry_payload_is_rechecked_against_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summarize_calls: list[dict[str, Any]] = []
    _patch_summarizer(monkeypatch, calls=summarize_calls)
    provider = _BudgetCheckingProvider(proof_budget=2_500)
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=1_048_576,
            max_provider_retries=0,
            max_overflow_retries=2,
        ),
    )
    agent.set_history(list(_LARGE_HISTORY))

    events = [event async for event in agent.run_turn("x" * 1000)]

    assert [proof["fits"] for proof in provider.proofs] == [False, True]
    assert summarize_calls
    assert summarize_calls[0]["window_tokens"] < agent.config.context_window_tokens
    assert len(provider.calls) == 2
    assert provider.proofs[1]["estimated_chars"] <= provider.proof_budget
    assert session_payload_chars(provider.calls[1]) < provider.proof_budget
    assert any(event.kind == "done" and getattr(event, "text", "") == "ok" for event in events)


@pytest.mark.asyncio
async def test_provider_budget_retry_uses_effective_proof_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summarize_calls: list[dict[str, Any]] = []
    _patch_summarizer(monkeypatch, calls=summarize_calls)
    provider = _ProviderRequestBudgetExceededProvider(
        success_after=1,
        proof={
            "fallback_reason": "provider_request_budget_exhausted",
            "estimated_chars": 100_000,
            "estimated_tokens": 25_000,
            "proof_budget": 96_000,
            "raw_proof_budget": 96_000,
            "effective_proof_budget": 86_400,
            "proof_headroom_chars": 9_600,
            "recent_tail_too_large": False,
        },
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=1_048_576,
            max_provider_retries=0,
            max_overflow_retries=2,
        ),
    )
    agent.set_history(list(_LARGE_HISTORY))

    events = [event async for event in agent.run_turn("x" * 4000)]

    assert [call["window_tokens"] for call in summarize_calls] == [21_600]
    assert any(event.kind == "done" and getattr(event, "text", "") == "ok" for event in events)


@pytest.mark.asyncio
async def test_provider_request_budget_recent_tail_reason_survives_noop_compaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no history the current turn alone is the tail: nothing to compact."""
    summarize_calls: list[dict[str, Any]] = []
    _patch_summarizer(monkeypatch, calls=summarize_calls)
    provider = _ProviderRequestBudgetExceededProvider(
        proof={
            "fallback_reason": "provider_request_budget_exhausted",
            "estimated_chars": 109_055,
            "estimated_tokens": 27_263,
            "proof_budget": 96_000,
            "recent_tail_too_large": True,
        },
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=1_048_576,
            max_provider_retries=0,
            max_overflow_retries=2,
        ),
    )

    events = [event async for event in agent.run_turn("x" * 4000)]
    errors = [event for event in events if isinstance(event, ErrorEvent)]

    assert len(provider.calls) == 1
    assert summarize_calls == []
    assert errors[-1].code == "provider_request_too_large"
    assert RAW_CURRENT_TURN_OVERFLOW_MESSAGE not in errors[-1].message
    assert not any(
        isinstance(event, ErrorEvent) and event.code == "compaction_not_smaller" for event in events
    )


@pytest.mark.asyncio
async def test_provider_request_budget_recent_tail_exhaustion_is_reported_as_controlled_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_summarizer(monkeypatch)
    provider = _ProviderRequestBudgetExceededProvider(
        proof={
            "fallback_reason": "provider_request_budget_exhausted",
            "recent_tail_too_large": True,
            "estimated_chars": 100_000,
            "proof_budget": 96_000,
        }
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=0,
            max_overflow_retries=1,
        ),
    )
    agent.set_history(list(_LARGE_HISTORY))

    events = [event async for event in agent.run_turn("x" * 4000)]
    errors = [event for event in events if isinstance(event, ErrorEvent)]

    assert len(provider.calls) == 2
    assert errors[-1].code == "provider_request_too_large"
    assert "current turn" not in errors[-1].message.lower()
    assert RAW_CURRENT_TURN_OVERFLOW_MESSAGE not in errors[-1].message


@pytest.mark.asyncio
async def test_context_overflow_degraded_flush_still_runs_live_compaction_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summarize_calls: list[dict[str, Any]] = []
    _patch_summarizer(monkeypatch, calls=summarize_calls)
    provider = _ContextOverflowProvider(success_after=1)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_provider_retries=0, max_overflow_retries=2),
    )
    agent.set_history(list(_LARGE_HISTORY))

    events = [event async for event in agent.run_turn("x" * 4000)]

    assert summarize_calls
    assert len(provider.calls) == 2
    assert any(event.kind == "done" and getattr(event, "text", "") == "ok" for event in events)
    assert not any(
        isinstance(event, ErrorEvent)
        and event.code in {"compaction_refused_memory_flush", "compaction_refused_flush_timeout"}
        for event in events
    )


async def _collect_events(stream: AsyncIterator[Any]) -> list[Any]:
    return [event async for event in stream]


def _event_index(events: list[Any], predicate: Any) -> int:
    return next(index for index, event in enumerate(events) if predicate(event))


def _provider_payload_is_smaller(before: list[Message], after: list[Message]) -> bool:
    return len(after) < len(before) or session_payload_chars(after) < session_payload_chars(before)
