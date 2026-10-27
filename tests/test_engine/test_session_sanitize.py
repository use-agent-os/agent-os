from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine import Agent, AgentConfig, ToolResult, ToolResultEvent
from agentos.engine.history import limit_turns
from agentos.engine.session_sanitize import (
    project_historical_tool_payloads,
    sanitize_session_messages,
)
from agentos.engine.transcript import legacy_migration_for, scrub_projected_tool_arguments
from agentos.engine.types import ThinkingLevel
from agentos.provider import (
    ChatConfig,
    ContentBlockRedactedThinking,
    ContentBlockText,
    ContentBlockThinking,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    ModelCapabilities,
)
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.provider import ToolUseEndEvent as ProviderToolUseEnd
from agentos.provider import ToolUseStartEvent as ProviderToolUseStart


def test_agent_config_disables_tool_argument_projection_by_default() -> None:
    assert AgentConfig().tool_use_argument_projection_enabled is False


class CapturingProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append({"messages": messages, "tools": tools, "config": config})
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderText(text="ok")
        yield ProviderDone(stop_reason="end_turn", input_tokens=3, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class StaticCostProvider(CapturingProvider):
    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderText(text="ok")
        yield ProviderDone(
            stop_reason="end_turn",
            input_tokens=1000,
            output_tokens=1000,
            billed_cost=0.0,
            model="deepseek-v4-flash",
        )


class ToolLoopCapturingProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        call_number = len(self.calls) + 1
        self.calls.append({"messages": messages, "tools": tools, "config": config})
        return self._stream(call_number)

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderToolUseStart(tool_use_id="tool-1", tool_name="echo")
            yield ProviderToolUseEnd(
                tool_use_id="tool-1",
                tool_name="echo",
                arguments={"value": "ok"},
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1)
            return
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="end_turn", input_tokens=4, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class ReasoningToolLoopCapturingProvider(ToolLoopCapturingProvider):
    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderToolUseStart(tool_use_id="tool-1", tool_name="echo")
            yield ProviderToolUseEnd(
                tool_use_id="tool-1",
                tool_name="echo",
                arguments={"value": "ok"},
            )
            yield ProviderDone(
                stop_reason="tool_use",
                input_tokens=3,
                output_tokens=1,
                reasoning_content="I should call echo before finalizing.",
            )
            return
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="end_turn", input_tokens=4, output_tokens=1)


class LargeArgumentToolLoopCapturingProvider(ToolLoopCapturingProvider):
    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderToolUseStart(tool_use_id="tool-1", tool_name="execute_code")
            yield ProviderToolUseEnd(
                tool_use_id="tool-1",
                tool_name="execute_code",
                arguments={"code": self.code, "timeout": 10},
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1)
            return
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="end_turn", input_tokens=4, output_tokens=1)


class CopiedProjectionToolLoopCapturingProvider(LargeArgumentToolLoopCapturingProvider):
    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            async for event in super()._stream(call_number):
                yield event
            return
        if call_number == 2:
            projection = self._legacy_projected_code_argument()
            yield ProviderToolUseStart(tool_use_id="tool-2", tool_name="execute_code")
            yield ProviderToolUseEnd(
                tool_use_id="tool-2",
                tool_name="execute_code",
                arguments={"code": projection, "timeout": 99},
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1)
            return
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="end_turn", input_tokens=4, output_tokens=1)

    def _legacy_projected_code_argument(self) -> str:
        return (
            "[tool_use_argument_projection]\n"
            "tool: execute_code\n"
            "tool_use_id: tool-1\n"
            "field: code\n"
            f"original_chars: {len(self.code)}\n"
            f"original_input_chars: {len(self.code)}\n"
            "sha256: " + hashlib.sha256(self.code.encode("utf-8")).hexdigest() + "\n"
            "tool_argument_handle: tr-1234567890abcdef1234567890abcdef\n"
            "omitted_chars: 123\n"
            "reason: legacy test marker\n"
        )


class CompactedToolArgumentsProvider(ToolLoopCapturingProvider):
    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderToolUseStart(tool_use_id="tool-compact", tool_name="exec_command")
            yield ProviderToolUseEnd(
                tool_use_id="tool-compact",
                tool_name="exec_command",
                arguments={
                    "_agentos_compacted_tool_arguments": True,
                    "original_chars": 549,
                    "sha256": "0" * 64,
                    "argument_keys": ["command", "timeout"],
                },
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1)
            return
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="end_turn", input_tokens=4, output_tokens=1)


class TextThenCompactedToolArgumentsProvider(ToolLoopCapturingProvider):
    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderText(text="I will prepare the file, then run the tool.")
            yield ProviderToolUseStart(tool_use_id="tool-compact", tool_name="write_file")
            yield ProviderToolUseEnd(
                tool_use_id="tool-compact",
                tool_name="write_file",
                arguments={
                    "_agentos_compacted_tool_arguments": True,
                    "tool": "write_file",
                    "reason": "provider_context_omitted",
                },
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1)
            return
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="end_turn", input_tokens=4, output_tokens=1)


class CapturingTurnLog:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, kind: str, payload: dict[str, Any]) -> None:
        self.records.append({"kind": kind, "payload": payload})


def test_session_sanitize_strips_block_metadata_without_compressing_content() -> None:
    message = Message.model_construct(
        role="user",
        content=[
            {
                "type": "tool_result",
                "tool_use_id": "call_1",
                "content": "result text with details that must remain factual",
                "is_error": False,
                "details": {"raw_provider": "debug-only"},
                "timestamp": "2026-04-28T14:35:00Z",
            }
        ],
        reasoning_content=None,
    )

    sanitized, result = sanitize_session_messages([message])

    assert result.metadata_keys_removed == 2
    block = sanitized[0].content[0]
    assert isinstance(block, ContentBlockToolResult)
    assert block.content == "result text with details that must remain factual"
    assert "details" not in block.model_dump(mode="json")
    assert "timestamp" not in block.model_dump(mode="json")


def test_historical_replay_projection_compacts_tool_payloads_and_reasoning() -> None:
    large_argument = "STALE_ARGUMENT_START\n" + ("x" * 6000)
    large_result = "STALE_RESULT_START\n" + ("y" * 6000)
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="write-1",
                    name="write_file",
                    input={"path": "index.html", "content": large_argument},
                )
            ],
            reasoning_content="hidden reasoning\n" + ("r" * 4000),
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="write-1",
                    content=large_result,
                    is_error=False,
                )
            ],
        ),
    ]

    projected, result = project_historical_tool_payloads(messages)

    assert result.tool_uses_projected == 1
    assert result.tool_results_projected == 1
    assert result.reasoning_chars_removed > 0
    assert projected[0].reasoning_content is None
    tool_use = projected[0].content[0]
    assert isinstance(tool_use, ContentBlockToolUse)
    assert tool_use.input["path"] == "index.html"
    assert tool_use.input["content"].startswith("[historical_tool_argument_omitted]\n")
    assert large_argument not in tool_use.input["content"]
    tool_result = projected[1].content[0]
    assert isinstance(tool_result, ContentBlockToolResult)
    assert str(tool_result.content).startswith("[historical_tool_result_compacted]")
    assert large_result not in str(tool_result.content)
    assert messages[0].reasoning_content is not None
    assert messages[0].content[0].input["content"] == large_argument


def test_historical_replay_projection_compacts_nested_tool_payloads() -> None:
    large_content = "CONTENT_START\n" + ("c" * 3000)
    large_nested = {"blob": "NESTED_START\n" + ("n" * 20_000)}
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="nested-1",
                    name="write_file",
                    input={
                        "path": "index.html",
                        "content": large_content,
                        "metadata": large_nested,
                    },
                )
            ],
        )
    ]

    projected, result = project_historical_tool_payloads(messages)

    assert result.tool_uses_projected == 1
    tool_use = projected[0].content[0]
    assert isinstance(tool_use, ContentBlockToolUse)
    assert tool_use.input["path"] == "index.html"
    assert tool_use.input["content"].startswith("[historical_tool_argument_omitted]\n")
    assert tool_use.input["metadata"].startswith("[historical_tool_argument_omitted]\n")
    payload = json.dumps(tool_use.input, ensure_ascii=False)
    assert "c" * 1000 not in payload
    assert "n" * 1000 not in payload
    assert messages[0].content[0].input["metadata"] == large_nested


def test_historical_replay_projection_compacts_list_tool_results() -> None:
    large_result = [{"type": "text", "text": "LIST_RESULT_START\n" + ("y" * 8000)}]
    messages = [
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="list-result-1",
                    content=large_result,
                    is_error=False,
                )
            ],
        )
    ]

    projected, result = project_historical_tool_payloads(messages)

    assert result.tool_results_projected == 1
    tool_result = projected[0].content[0]
    assert isinstance(tool_result, ContentBlockToolResult)
    assert isinstance(tool_result.content, str)
    assert tool_result.content.startswith("[historical_tool_result_compacted]")
    assert "y" * 1000 not in tool_result.content
    assert messages[0].content[0].content == large_result


def _seal(agent: Agent, messages: list[Message], start: int = 0) -> list[Message]:
    """Seal *messages* the way the agent does before their first request."""
    sealed = list(messages)
    agent._seal_transcript(sealed, start)
    return sealed


def _tool_pair(
    tool_id: str,
    body: str,
    *,
    name: str = "execute_code",
    is_error: bool = False,
    tool_input: dict[str, Any] | None = None,
) -> list[Message]:
    return [
        Message(
            role="assistant",
            content=[ContentBlockToolUse(id=tool_id, name=name, input=tool_input or {})],
        ),
        Message(
            role="user",
            content=[ContentBlockToolResult(tool_use_id=tool_id, content=body, is_error=is_error)],
        ),
    ]


def test_seal_projects_an_oversized_result_once_and_keeps_errors() -> None:
    """A tool result over its budget is shrunk when appended; errors are kept."""
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(context_window_tokens=200),
    )
    messages = [
        *_tool_pair("old-1", "old bulky output\n" + ("x" * 9000)),
        *_tool_pair("err-1", "Traceback\n" + ("e" * 9000), is_error=True),
    ]

    sealed = _seal(agent, messages)

    old_result = sealed[1].content[0]
    err_result = sealed[3].content[0]
    assert isinstance(old_result, ContentBlockToolResult)
    assert isinstance(err_result, ContentBlockToolResult)
    assert "[tool_result_projection]" in old_result.content
    assert "tool_result_handle:" not in old_result.content
    assert len(old_result.content) < len(messages[1].content[0].content)
    assert "Traceback" in err_result.content
    assert len(err_result.content) > 9000
    assert agent.config.metadata["tool_provider_guard_projection_applied"] is True
    assert agent.config.metadata["tool_projection_applied"] is True


def test_seal_never_rewrites_an_already_sealed_message() -> None:
    """Shrinking happens at append time only; sealed messages are immutable."""
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(context_window_tokens=200),
    )
    first = _tool_pair("old-1", "old bulky output\n" + ("x" * 9000))
    transcript = list(first)
    agent._seal_transcript(transcript, 0)
    sealed_first = list(transcript)

    transcript.extend(_tool_pair("new-1", "new bulky output\n" + ("y" * 9000)))
    agent._seal_transcript(transcript, len(sealed_first))

    # The earlier pair is byte-identical (same objects) after the second seal.
    assert all(a is b for a, b in zip(transcript[:2], sealed_first, strict=True))
    second_result = transcript[3].content[0]
    assert isinstance(second_result, ContentBlockToolResult)
    assert "[tool_result_projection]" in second_result.content


def test_seal_keeps_under_cap_results_verbatim_on_a_large_context() -> None:
    """No aggregate rewrite: results under their budget stay as appended.

    Accumulated size is the compaction service's job (one summary + the
    current turn), not a per-request projection of older results.
    """
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(context_window_tokens=200_000),
    )
    messages = [
        *_tool_pair("old-1", "old local output\n" + ("x" * 70_000), name="local_tool"),
        *_tool_pair(
            "err-1", "Traceback preserved\n" + ("e" * 20_000), name="local_tool", is_error=True
        ),
        *_tool_pair("mid-1", "middle local output\n" + ("m" * 50_000), name="local_tool"),
        *_tool_pair("new-1", "recent local output\n" + ("r" * 50_000), name="local_tool"),
    ]

    sealed = _seal(agent, messages)

    assert [message.content for message in sealed] == [message.content for message in messages]
    assert "tool_provider_guard_projection_applied" not in agent.config.metadata


@pytest.mark.asyncio
async def test_agent_single_tool_result_projection_does_not_store_raw_content(tmp_path) -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(
            context_window_tokens=200,
        ),
    )
    raw_output = "single bulky output\n" + ("x" * 8000)

    projected = _seal(agent, _tool_pair("tool-1", raw_output))
    result = projected[1].content[0]
    assert isinstance(result, ContentBlockToolResult)

    assert "[tool_result_projection]" in result.content
    assert "tool_result_handle:" not in result.content
    assert raw_output not in result.content
    assert not (tmp_path / "tool-results").exists()


@pytest.mark.asyncio
async def test_agent_tool_result_projection_never_writes_raw_store(
    tmp_path,
) -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(
            context_window_tokens=200,
        ),
    )

    projected = _seal(agent, _tool_pair("tool-1", "single bulky output\n" + ("x" * 8000)))
    result = projected[1].content[0]
    assert isinstance(result, ContentBlockToolResult)

    assert "[tool_result_projection]" in result.content
    assert "tool_result_handle:" not in result.content
    assert not list((tmp_path / "tool-results").rglob("content.txt"))


def test_seal_budgets_each_result_on_its_own_not_the_total() -> None:
    """Five results that are each under the cap are not rewritten."""
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(
            context_window_tokens=1200,
        ),
    )
    messages: list[Message] = []
    for index in range(5):
        messages.extend(_tool_pair(f"tool-{index}", f"chunk {index}\n" + ("x" * 800)))

    sealed = _seal(agent, messages)

    contents = [
        message.content[0].content
        for message in sealed
        if isinstance(message.content, list)
        and message.content
        and isinstance(message.content[0], ContentBlockToolResult)
    ]
    assert contents == [f"chunk {index}\n" + ("x" * 800) for index in range(5)]


def test_seal_classifies_external_results_from_tool_use_names() -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(
            context_window_tokens=1_000_000,
            tool_result_provider_request_max_chars=1300,
        ),
    )
    messages: list[Message] = []
    for index in range(2):
        messages.extend(
            _tool_pair(
                f"fetch-{index}",
                f"fetch {index}\n" + ("x" * 5000),
                name="web_fetch",
                tool_input={"url": f"https://example.com/{index}"},
            )
        )

    sealed = _seal(agent, messages)

    external_contents = [
        message.content[0].content
        for message in sealed
        if isinstance(message.content, list)
        and message.content
        and isinstance(message.content[0], ContentBlockToolResult)
    ]
    assert all("[tool_result_projection]" in content for content in external_contents)
    assert all(
        "external tool result compacted for provider request context" in content
        for content in external_contents
    )
    assert sum(len(content) for content in external_contents) < 2 * (len("fetch 0\n") + 5000)


def test_seal_preserves_sessions_yield_control_json() -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(
            context_window_tokens=1_000_000,
            tool_result_provider_request_max_chars=300,
        ),
    )
    control_payload = json.dumps(
        {
            "status": "yielded",
            "waited": False,
            "message": "Current turn yielded; wait for pushed session events.",
            "yield_message": "y" * 1000,
        }
    )
    messages = [
        *_tool_pair("yield-1", control_payload, name="sessions_yield"),
        *_tool_pair(
            "fetch-1",
            "fetch\n" + ("x" * 1000),
            name="web_fetch",
            tool_input={"url": "https://example.com"},
        ),
    ]

    sealed = _seal(agent, messages)
    control_result = sealed[1].content[0]

    assert isinstance(control_result, ContentBlockToolResult)
    payload = json.loads(control_result.content)
    assert payload["status"] == "yielded"
    assert payload["waited"] is False


def test_seal_keeps_large_tool_use_arguments() -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(
            tool_use_argument_provider_request_max_chars=1200,
        ),
    )
    large_code = "print('start')\n" + ("x = 1\n" * 500) + "print('end')\n"
    messages = _tool_pair(
        "code-1",
        "ok",
        tool_input={"code": large_code, "timeout": 10},
    )

    sealed = _seal(agent, messages)
    scrubbed, count = scrub_projected_tool_arguments(messages)

    for view in (sealed, scrubbed):
        block = view[0].content[0]
        assert isinstance(block, ContentBlockToolUse)
        assert block.input["code"] == large_code
    assert count == 0
    assert "tool_argument_projection_applied" not in agent.config.metadata


def test_legacy_scrub_replaces_projected_tool_argument() -> None:
    projection = (
        "[tool_use_argument_projection]\n"
        "tool: execute_code\n"
        "tool_use_id: tool-legacy\n"
        "field: code\n"
        "sha256: missing\n"
        "tool_argument_handle: tr-missing\n"
        "head:\nprint('legacy')"
    )
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="tool-legacy",
                    name="execute_code",
                    input={"code": projection, "timeout": 99},
                )
            ],
            reasoning_details=[{"type": "reasoning.text", "text": "kept"}],
        )
    ]

    scrubbed, count = scrub_projected_tool_arguments(messages)

    block = scrubbed[0].content[0]
    assert count == 1
    assert isinstance(block, ContentBlockToolUse)
    assert "tool_use_argument_projection" not in block.input["code"]
    assert "invalid_provider_context_projection:execute_code.code" in block.input["code"]
    assert scrubbed[0].reasoning_details == [{"type": "reasoning.text", "text": "kept"}]


def test_seal_does_not_project_many_tool_use_arguments() -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(
            tool_use_argument_provider_request_max_chars=1200,
            tool_use_argument_projection_enabled=True,
        ),
    )
    for size, count in ((700, 5), (200, 6)):
        messages = [
            Message(
                role="assistant",
                content=[
                    ContentBlockToolUse(
                        id=f"write-{index}",
                        name="write_file",
                        input={
                            "path": f"generated/file-{index}.html",
                            "content": "x" * size,
                        },
                    )
                    for index in range(count)
                ],
            ),
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(tool_use_id=f"write-{index}", content="ok")
                    for index in range(count)
                ],
            ),
        ]

        sealed = _seal(agent, messages)

        blocks = [block for block in sealed[0].content if isinstance(block, ContentBlockToolUse)]
        assert all(block.input["content"] == "x" * size for block in blocks)
        assert all(block.input["path"].startswith("generated/") for block in blocks)
    assert "tool_argument_projection_applied" not in agent.config.metadata
    assert "tool_argument_projection_calls" not in agent.config.metadata


def test_seal_derives_tool_result_budget_above_legacy_default() -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(
            context_window_tokens=200_000,
            max_tokens=8192,
        ),
    )
    messages: list[Message] = []
    for index in range(3):
        messages.extend(
            _tool_pair(
                f"fetch-derived-{index}",
                f"FETCH_DERIVED_{index}\n" + ("x" * 40_000),
                name="web_fetch",
                tool_input={"url": f"https://example.com/{index}"},
            )
        )

    sealed = _seal(agent, messages)

    first_result = sealed[1].content[0]
    assert isinstance(first_result, ContentBlockToolResult)
    assert first_result.content.startswith("FETCH_DERIVED_0")
    assert "external_tool_result_compacted" not in first_result.content


def test_seal_keeps_successful_file_write_argument_executable(tmp_path) -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(
            tool_use_argument_provider_request_max_chars=8000,
        ),
    )
    large_html = "<html>\n" + ("<p>word</p>\n" * 900) + "</html>\n"
    messages = _tool_pair(
        "write-1",
        '{"status":"ok","path":"index.html"}',
        name="write_file",
        tool_input={"path": "index.html", "content": large_html},
    )

    first = _seal(agent, messages)
    second = _seal(agent, messages)

    for view in (first, second):
        tool_use = next(
            block for block in view[0].content if isinstance(block, ContentBlockToolUse)
        )
        assert tool_use.id == "write-1"
        assert tool_use.input["path"] == "index.html"
        assert tool_use.input["content"] == large_html
        result = next(
            block for block in view[1].content if isinstance(block, ContentBlockToolResult)
        )
        assert result.tool_use_id == "write-1"
        assert view[-1].role == "user"
    assert not list((tmp_path / "tool-results").glob("**/meta.json"))
    assert "tool_argument_projection_applied" not in agent.config.metadata
    assert "tool_argument_projection_calls" not in agent.config.metadata


@pytest.mark.asyncio
async def test_agent_static_cost_source_is_explicitly_distinct_from_provider_billed() -> None:
    provider = StaticCostProvider()
    agent = Agent(provider=provider, config=AgentConfig(model_id="deepseek-v4-flash"))

    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if event.kind == "done")

    assert done.billed_cost == 0.0
    assert done.cost_usd > 0.0
    assert done.cost_source == "agentos_static_estimate"


@pytest.mark.asyncio
async def test_agent_uses_sanitized_request_view_and_records_context_stages() -> None:
    provider = CapturingProvider()
    turn_log = CapturingTurnLog()
    agent = Agent(
        provider=provider,
        config=AgentConfig(),
        turn_call_logger=turn_log,  # type: ignore[arg-type]
    )
    agent.set_history(
        [
            Message.model_construct(
                role="assistant",
                content=[
                    {
                        "type": "text",
                        "text": "previous answer",
                        "details": {"debug": True},
                    }
                ],
                reasoning_content=None,
            )
        ]
    )

    events = [event async for event in agent.run_turn("continue")]

    assert any(event.kind == "done" for event in events)
    assert provider.calls
    sent_history_block = provider.calls[0]["messages"][0].content[0]
    assert isinstance(sent_history_block, ContentBlockText)
    assert sent_history_block.text == "previous answer"
    assert "details" not in sent_history_block.model_dump(mode="json")

    stages = [
        record["payload"]["stage"]
        for record in turn_log.records
        if record["kind"] == "context_stage"
    ]
    assert stages == [
        "session:loaded",
        "session:sanitized",
        "session:limited",
        "prompt:before",
        "prompt:images",
        "stream:context",
        "session:after",
    ]


@pytest.mark.asyncio
async def test_agent_provider_view_omits_loaded_history_tool_arguments() -> None:
    provider = CapturingProvider()
    large_argument = "STALE_HISTORY_ARGUMENT\n" + ("x" * 20_000)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1),
    )
    # Rows without a structured record are migrated once, on load.
    agent.set_history(
        legacy_migration_for(agent)(
            [
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(
                            id="write-stale",
                            name="write_file",
                            input={"path": "index.html", "content": large_argument},
                        )
                    ],
                    reasoning_content="old reasoning\n" + ("r" * 10_000),
                ),
                Message(
                    role="user",
                    content=[
                        ContentBlockToolResult(
                            tool_use_id="write-stale",
                            content='{"status":"ok"}',
                            is_error=False,
                        )
                    ],
                ),
            ]
        )
    )

    events = [event async for event in agent.run_turn("new task")]

    assert any(event.kind == "done" for event in events)
    assert provider.calls
    payload = json.dumps(
        [message.model_dump(mode="json") for message in provider.calls[0]["messages"]],
        ensure_ascii=False,
    )
    assert large_argument not in payload
    assert "x" * 1000 not in payload
    assert "old reasoning" not in payload
    assert "historical_tool_argument_omitted" not in payload
    assert "invalid_provider_context_projection:write_file.content" in payload


@pytest.mark.asyncio
async def test_agent_preserves_deepseek_reasoning_while_projecting_history_payload() -> None:
    provider = CapturingProvider()
    large_argument = "DEEPSEEK_STALE_ARGUMENT\n" + ("x" * 20_000)
    reasoning = "I reasoned before the historical tool call."
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            thinking=ThinkingLevel.HIGH,
            model_id="deepseek-v4-flash",
            model_capabilities=ModelCapabilities(
                supports_reasoning=True,
                supports_tools=True,
                reasoning_format="deepseek",
            ),
        ),
    )
    agent.set_history(
        legacy_migration_for(agent)(
            [
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(
                            id="deepseek-stale",
                            name="write_file",
                            input={"path": "index.html", "content": large_argument},
                        )
                    ],
                    reasoning_content=reasoning,
                ),
                Message(
                    role="user",
                    content=[
                        ContentBlockToolResult(
                            tool_use_id="deepseek-stale",
                            content='{"status":"ok"}',
                            is_error=False,
                        )
                    ],
                ),
            ]
        )
    )

    events = [event async for event in agent.run_turn("continue")]

    assert any(event.kind == "done" for event in events)
    sent_assistant = provider.calls[0]["messages"][0]
    assert sent_assistant.reasoning_content == reasoning
    payload = json.dumps(
        [message.model_dump(mode="json") for message in provider.calls[0]["messages"]],
        ensure_ascii=False,
    )
    assert large_argument not in payload
    assert "x" * 1000 not in payload
    assert "historical_tool_argument_omitted" not in payload
    assert "invalid_provider_context_projection:write_file.content" in payload


@pytest.mark.asyncio
async def test_agent_runtime_context_is_request_only_and_not_system_prefix() -> None:
    provider = CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            system_prompt="stable system",
            cache_breakpoints=[{"text": "stable system", "cache": "true"}],
            cache_mode="auto",
            max_iterations=1,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    call = provider.calls[0]
    assert call["config"].system == "stable system"
    assert call["config"].cache_breakpoints == [{"text": "stable system", "cache": "true"}]
    assert call["messages"][0].role == "user"
    assert call["messages"][0].content.startswith("hello")
    assert "[Runtime context for this turn]" in call["messages"][0].content
    assert len(call["messages"]) == 1
    # The timestamp is a trailing block of the CURRENT user turn and is kept
    # with it: the next request replays the exact same bytes.
    assert agent._history[0] == call["messages"][0]


@pytest.mark.asyncio
async def test_agent_runtime_context_does_not_precede_current_user_text() -> None:
    provider = CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            system_prompt="stable system",
            cache_breakpoints=[{"text": "stable system", "cache": "true"}],
            cache_mode="auto",
            max_iterations=1,
        ),
    )
    first_prompt = "first prompt " + ("x" * 2000)

    first_events = [event async for event in agent.run_turn(first_prompt)]
    second_events = [event async for event in agent.run_turn("second prompt")]

    assert any(event.kind == "done" for event in first_events)
    assert any(event.kind == "done" for event in second_events)
    first_call = provider.calls[0]["messages"]
    assert first_call[-1].role == "user"
    assert first_call[-1].content.startswith(first_prompt)
    assert "[Runtime context for this turn]" in first_call[-1].content
    assert not any(
        isinstance(message.content, str)
        and message.content.startswith("[Runtime context for this turn]")
        for message in first_call[:-1]
    )
    assert agent._history[0] == first_call[-1]

    second_call = provider.calls[1]["messages"]
    # The first turn is replayed byte for byte, its timestamp included; the
    # second turn carries its own timestamp. Nothing older is rewritten.
    assert second_call[0] == first_call[-1]
    assert second_call[1] == Message(
        role="assistant",
        content=[ContentBlockText(text="ok")],
    )
    assert second_call[-1].role == "user"
    assert second_call[-1].content.startswith("second prompt")
    assert "[Runtime context for this turn]" in second_call[-1].content


def test_limit_turns_ignores_synthetic_user_messages_when_counting_turns() -> None:
    messages = [
        Message(role="user", content="first real user"),
        Message(role="assistant", content="first answer"),
        Message(role="user", content="[Available skills for this turn]\n<skill />"),
        Message(role="user", content="second real user"),
        Message(role="assistant", content="second answer"),
    ]

    assert limit_turns(messages, 2) == messages
    assert limit_turns(messages, 1) == messages[3:]


def test_turn_runner_keeps_dynamic_prompt_out_of_system_when_cache_enabled() -> None:
    from agentos.engine.runtime import TurnRunner

    runner = TurnRunner.__new__(TurnRunner)
    turn = SimpleNamespace(
        system_prompt=("stable base", "<memory_context>volatile recall</memory_context>"),
        metadata={"cache_enabled": True},
    )

    final_prompt, cache_breakpoints, request_context_prompt = runner._resolve_prompt_config(turn)

    assert final_prompt == "stable base"
    assert cache_breakpoints == [{"text": "stable base", "cache": "true"}]
    assert request_context_prompt == "<memory_context>volatile recall</memory_context>"


def test_turn_runner_preserves_joined_system_prompt_when_cache_disabled() -> None:
    from agentos.engine.runtime import TurnRunner

    runner = TurnRunner.__new__(TurnRunner)
    turn = SimpleNamespace(
        system_prompt=("stable base", "<memory_context>volatile recall</memory_context>"),
        metadata={},
    )

    final_prompt, cache_breakpoints, request_context_prompt = runner._resolve_prompt_config(turn)

    assert final_prompt == "stable base\n\n<memory_context>volatile recall</memory_context>"
    assert cache_breakpoints is None
    assert request_context_prompt is None


@pytest.mark.asyncio
async def test_compaction_keeps_the_current_turn_with_its_context_blocks() -> None:
    """The single-summary shape keeps the current turn's input message --
    volatile context blocks included -- verbatim after the summary exchange."""

    async def _summarizer(_messages: list[Message], _previous: str | None) -> tuple[str, str]:
        return "summary", "llm"

    from agentos.engine.compaction_service import CompactionService

    current = Message(
        role="user",
        content=(
            "current question\n\n[Request context for this turn]\nvolatile"
            "\n\n[Runtime context for this turn]\nnow"
        ),
    )
    messages = [
        Message(role="user", content="old question"),
        Message(role="assistant", content="old answer"),
        current,
    ]
    service = CompactionService(provider=None, summarizer=_summarizer)

    result = await service.compact(
        messages,
        turn_input_index=2,
        estimated_tokens=10_000,
        window_tokens=1_000,
        message_tokens=lambda items: sum(
            4_950 if str(m.content).startswith("old") else 10 for m in items
        ),
    )

    assert result.compacted
    assert [message.content for message in result.messages] == [
        "[Context summary]\nsummary",
        "Understood. Continuing from summary.",
        current.content,
    ]
    assert result.messages[2] is current
    assert result.turn_input_index == 2


@pytest.mark.asyncio
async def test_agent_request_context_is_request_only_after_history() -> None:
    provider = CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            system_prompt="stable system",
            request_context_prompt="<memory_context>volatile recall</memory_context>",
            skills_context_prompt="<skill id='memory'>Memory helper</skill>",
            cache_breakpoints=[{"text": "stable system", "cache": "true"}],
            cache_mode="auto",
            max_iterations=1,
        ),
    )
    agent.set_history(
        [
            Message(role="user", content="old question"),
            Message(role="assistant", content="old answer"),
        ]
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    call = provider.calls[0]
    assert call["config"].system == "stable system"
    assert call["config"].cache_breakpoints == [{"text": "stable system", "cache": "true"}]
    assert "<memory_context>volatile recall</memory_context>" not in call["config"].system
    # History is untouched; skills, request context and the timestamp are
    # trailing blocks of the CURRENT user message, in that order.
    assert [message.role for message in call["messages"]] == ["user", "assistant", "user"]
    assert call["messages"][0] == Message(role="user", content="old question")
    assert call["messages"][1] == Message(role="assistant", content="old answer")
    current = call["messages"][2].content
    assert current.startswith("hello\n\n[Available skills for this turn]")
    assert current.index("[Available skills for this turn]") < current.index(
        "[Request context for this turn]"
    )
    assert current.index("[Request context for this turn]") < current.index(
        "[Runtime context for this turn]"
    )
    assert "<memory_context>volatile recall</memory_context>" in current
    # ... and persisted with that message, so the next turn replays it as is.
    assert agent._history[2] == call["messages"][2]


@pytest.mark.asyncio
async def test_agent_provider_view_moves_non_adjacent_tool_result_next_to_its_call() -> None:
    provider = CapturingProvider()
    agent = Agent(provider=provider, config=AgentConfig(max_iterations=1))
    agent.set_history(
        [
            Message(role="user", content="old task"),
            Message(
                role="assistant",
                content=[
                    ContentBlockToolUse(
                        id="call_lookup",
                        name="lookup",
                        input={"q": "old"},
                    )
                ],
            ),
            Message(role="user", content="ordinary user message before the tool result"),
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(
                        tool_use_id="call_lookup",
                        content="lookup result",
                        is_error=False,
                    )
                ],
            ),
        ]
    )

    events = [event async for event in agent.run_turn("continue")]

    assert any(event.kind == "done" for event in events)
    sent = provider.calls[0]["messages"]
    # The call is kept (it may have had effects) and its real result is moved
    # right after it; the ordinary user message follows.
    assert [message.role for message in sent[:4]] == ["user", "assistant", "user", "user"]
    assert isinstance(sent[1].content[0], ContentBlockToolUse)
    result = sent[2].content[0]
    assert isinstance(result, ContentBlockToolResult)
    assert result.tool_use_id == "call_lookup"
    assert result.content == "lookup result"
    assert sent[3].content == "ordinary user message before the tool result"


@pytest.mark.asyncio
async def test_agent_provider_view_preserves_split_adjacent_tool_results() -> None:
    provider = CapturingProvider()
    agent = Agent(provider=provider, config=AgentConfig(max_iterations=1))
    agent.set_history(
        [
            Message(role="user", content="old task"),
            Message(
                role="assistant",
                content=[
                    ContentBlockToolUse(
                        id="call_alpha",
                        name="lookup",
                        input={"q": "alpha"},
                    ),
                    ContentBlockToolUse(
                        id="call_beta",
                        name="lookup",
                        input={"q": "beta"},
                    ),
                ],
            ),
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(
                        tool_use_id="call_alpha",
                        content="alpha result",
                        is_error=False,
                    )
                ],
            ),
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(
                        tool_use_id="call_beta",
                        content="beta result",
                        is_error=False,
                    )
                ],
            ),
        ]
    )

    events = [event async for event in agent.run_turn("continue")]

    assert any(event.kind == "done" for event in events)
    replay_payload = json.dumps(
        [message.model_dump(mode="json") for message in provider.calls[0]["messages"]],
        ensure_ascii=False,
    )
    assert "call_alpha" in replay_payload
    assert "alpha result" in replay_payload
    assert "call_beta" in replay_payload
    assert "beta result" in replay_payload


@pytest.mark.asyncio
async def test_agent_request_context_repeats_across_tool_loop_without_persisting() -> None:
    provider = ToolLoopCapturingProvider()

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            system_prompt="stable system",
            request_context_prompt="<memory_context>volatile recall</memory_context>",
            max_iterations=2,
        ),
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 2
    for call in provider.calls:
        request_context_messages = [
            message
            for message in call["messages"]
            if isinstance(message.content, str)
            and "<memory_context>volatile recall</memory_context>" in message.content
        ]
        assert len(request_context_messages) == 1
        assert "[Request context for this turn]" in request_context_messages[0].content
    # The second request repeats the first byte for byte as its prefix.
    first, second = (call["messages"] for call in provider.calls)
    assert second[: len(first)] == first


@pytest.mark.asyncio
async def test_unchanged_skills_and_request_context_are_not_repeated_next_turn() -> None:
    provider = CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            system_prompt="stable system",
            request_context_prompt="<memory_context>stable recall</memory_context>",
            skills_context_prompt="<skill id='memory'>Memory helper</skill>",
            max_iterations=1,
        ),
    )

    _ = [event async for event in agent.run_turn("first")]
    _ = [event async for event in agent.run_turn("second")]
    agent.config.skills_context_prompt = "<skill id='memory'>Memory helper v2</skill>"
    _ = [event async for event in agent.run_turn("third")]

    second_input = provider.calls[1]["messages"][-1].content
    third_input = provider.calls[2]["messages"][-1].content
    assert "[Available skills for this turn]" not in second_input
    assert "[Request context for this turn]" not in second_input
    assert "[Runtime context for this turn]" in second_input
    assert "Memory helper v2" in third_input
    assert "[Request context for this turn]" not in third_input
    # Every request starts with the previous request's messages, unchanged.
    for earlier, later in zip(provider.calls, provider.calls[1:], strict=False):
        assert later["messages"][: len(earlier["messages"])] == earlier["messages"]


@pytest.mark.asyncio
async def test_agent_canonicalizes_large_tool_result_for_event_history_and_provider() -> None:
    provider = ToolLoopCapturingProvider()
    raw_output = "single bulky output\n" + ("x" * 5000)

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content=raw_output,
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=200,
            max_iterations=2,
            tool_result_projection_max_inline_chars=1000,
        ),
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("hello")]

    result_event = next(
        event
        for event in events
        if isinstance(event, ToolResultEvent) and event.tool_use_id == "tool-1"
    )
    assert result_event.result != raw_output
    assert "tool_result_handle:" not in result_event.result
    history_result = next(
        block
        for message in agent._history
        if message.role == "user" and isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolResult) and block.tool_use_id == "tool-1"
    )
    assert history_result.content == result_event.result
    assert history_result.content != raw_output

    assert len(provider.calls) == 2
    replay_result = next(
        block
        for message in provider.calls[1]["messages"]
        if message.role == "user" and isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolResult) and block.tool_use_id == "tool-1"
    )
    assert replay_result.content == result_event.result
    assert replay_result.content != raw_output
    assert "tool_result_handle:" not in replay_result.content
    assert "single bulky output" in replay_result.content
    assert len(replay_result.content) < len(raw_output)


def test_agent_provider_request_messages_are_the_sealed_transcript() -> None:
    """The request view does no per-call rewriting: a sealed transcript is sent
    as is, and an unsealed oversized result is shrunk by the seal, not here."""
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(
            context_window_tokens=200,
        ),
    )
    raw_output = "overflow retry bulky output\n" + ("x" * 8000)
    messages = [
        Message(
            role="assistant",
            content=[ContentBlockToolUse(id="tool-1", name="execute_code", input={})],
        ),
        Message(
            role="user",
            content=[ContentBlockToolResult(tool_use_id="tool-1", content=raw_output)],
        ),
    ]

    unsealed_view = agent._provider_request_messages(messages)
    assert unsealed_view == messages

    sealed = list(messages)
    agent._seal_transcript(sealed, 0)
    request_messages = agent._provider_request_messages(sealed)

    original_result = messages[1].content[0]
    assert isinstance(original_result, ContentBlockToolResult)
    assert original_result.content == raw_output
    request_result = next(
        block
        for message in request_messages
        if message.role == "user" and isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolResult) and block.tool_use_id == "tool-1"
    )
    assert request_result.content != raw_output
    assert "[tool_result_projection]" in request_result.content
    assert "tool_result_handle:" not in request_result.content
    assert len(request_result.content) < len(raw_output)
    assert agent._provider_request_messages(sealed) == request_messages


@pytest.mark.asyncio
async def test_agent_keeps_large_tool_arguments_during_tool_replay(tmp_path) -> None:
    large_code = "print('start')\n" + ("x = 1\n" * 500) + "print('end')\n"
    provider = LargeArgumentToolLoopCapturingProvider(large_code)

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
            tool_use_argument_provider_request_max_chars=1200,
            tool_use_argument_projection_enabled=True,
        ),
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    replay_messages = provider.calls[1]["messages"]
    assistant_replay = next(
        message
        for message in replay_messages
        if message.role == "assistant"
        and isinstance(message.content, list)
        and any(getattr(block, "type", None) == "tool_use" for block in message.content)
    )
    replay_block = next(
        block for block in assistant_replay.content if isinstance(block, ContentBlockToolUse)
    )
    assert replay_block.input["code"] == large_code
    assert "tool_use_argument_projection" not in replay_block.input["code"]
    history_block = next(
        block
        for message in agent._history
        if message.role == "assistant" and isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolUse)
    )
    assert history_block.input["code"] == large_code


@pytest.mark.asyncio
async def test_agent_refuses_copied_tool_argument_projection_without_dispatch(
    tmp_path,
) -> None:
    large_code = "print('start')\n" + ("x = 1\n" * 500) + "print('end')\n"
    provider = CopiedProjectionToolLoopCapturingProvider(large_code)
    dispatched_code_arguments: list[str] = []
    dispatched_arguments: list[dict[str, Any]] = []

    async def tool_handler(call: Any) -> ToolResult:
        dispatched_code_arguments.append(call.arguments["code"])
        dispatched_arguments.append(call.arguments)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=3,
            tool_use_argument_provider_request_max_chars=1200,
            tool_use_argument_projection_enabled=True,
        ),
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("hello")]

    done_event = next(event for event in events if event.kind == "done")
    assert done_event.text == "done"
    assert len(provider.calls) == 3
    assert dispatched_code_arguments == [large_code]
    assert dispatched_arguments == [{"code": large_code, "timeout": 10}]
    assert all(
        not value.startswith("[tool_use_argument_projection]\n")
        for value in dispatched_code_arguments
    )
    result_event = next(
        event
        for event in events
        if isinstance(event, ToolResultEvent) and event.tool_use_id == "tool-2"
    )
    assert result_event.is_error is True
    assert "provider-only compacted tool argument" not in result_event.result
    assert "Projected tool argument" not in result_event.result
    assert result_event.arguments["code"] != large_code
    assert not result_event.arguments["code"].startswith("[tool_use_argument_projection]\n")
    assert "tool_use_argument_projection" not in result_event.arguments["code"]
    assert result_event.arguments["timeout"] == 99
    # The refused call is stripped from the transcript before any request
    # carries it (append time), so it is neither sent nor persisted.
    assert not _history_has_tool_use(agent, "tool-2")
    assert not any(
        "tool-2" in json.dumps(message.model_dump(mode="json"))
        for call in provider.calls
        for message in call["messages"]
    )

    follow_up_events = [event async for event in agent.run_turn("continue")]
    assert any(event.kind == "done" for event in follow_up_events)
    replay_payload = json.dumps(
        [message.model_dump(mode="json") for message in provider.calls[-1]["messages"]],
        ensure_ascii=False,
    )
    assert "tool-2" not in replay_payload
    assert "tool_use_argument_projection" not in replay_payload


@pytest.mark.asyncio
async def test_agent_refuses_unrestorable_tool_argument_projection(tmp_path) -> None:
    large_code = "print('start')\n" + ("x = 1\n" * 500) + "print('end')\n"
    provider = CopiedProjectionToolLoopCapturingProvider(large_code)
    dispatched_tool_ids: list[str] = []

    original_legacy_projection = provider._legacy_projected_code_argument

    def corrupted_projection() -> str:
        projection = original_legacy_projection()
        bad_hash = "0" * 64
        return projection.replace(
            "sha256: ",
            f"sha256: {bad_hash}\nprevious_sha256: ",
            1,
        )

    provider._legacy_projected_code_argument = corrupted_projection  # type: ignore[method-assign]

    async def tool_handler(call: Any) -> ToolResult:
        dispatched_tool_ids.append(call.tool_use_id)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=3,
            tool_use_argument_provider_request_max_chars=1200,
            tool_use_argument_projection_enabled=True,
        ),
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("hello")]

    result_event = next(
        event
        for event in events
        if isinstance(event, ToolResultEvent) and event.tool_use_id == "tool-2"
    )
    assert dispatched_tool_ids == ["tool-1"]
    assert result_event.is_error is True
    assert "provider-only compacted tool argument" not in result_event.result
    assert "Projected tool argument" not in result_event.result
    assert not result_event.arguments["code"].startswith("[tool_use_argument_projection]\n")
    assert "tool_use_argument_projection" not in result_event.arguments["code"]
    assert not _history_has_tool_use(agent, "tool-2")

    second_events = [event async for event in agent.run_turn("hi")]

    assert any(event.kind == "done" for event in second_events)
    replay_payload = json.dumps(
        [message.model_dump(mode="json") for message in provider.calls[-1]["messages"]],
        ensure_ascii=False,
    )
    assert "tool_use_id: tool-2" not in replay_payload
    stored_contents = [
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "tool-results").rglob("content.txt")
    ]
    assert all("tool_use_argument_projection" not in content for content in stored_contents)


@pytest.mark.asyncio
async def test_agent_refuses_copied_provider_compacted_tool_arguments(tmp_path) -> None:
    provider = CompactedToolArgumentsProvider()
    dispatched: list[dict[str, Any]] = []

    async def tool_handler(call: Any) -> ToolResult:
        dispatched.append(call.arguments)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
        ),
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("open in chrome")]

    assert any(event.kind == "done" for event in events)
    done_event = next(event for event in events if event.kind == "done")
    assert done_event.text == "done"
    assert "provider-only compacted tool arguments" not in done_event.text
    assert len(provider.calls) == 2
    assert dispatched == []
    result_event = next(
        event
        for event in events
        if isinstance(event, ToolResultEvent) and event.tool_use_id == "tool-compact"
    )
    assert result_event.is_error is True
    assert "provider-only compacted tool arguments" not in result_event.result
    assert "ProjectedToolArgumentsError" not in result_event.result
    assert "_agentos_compacted_tool_arguments" not in result_event.arguments
    assert not _history_has_tool_use(agent, "tool-compact")

    follow_up_events = [event async for event in agent.run_turn("continue")]
    assert any(event.kind == "done" for event in follow_up_events)
    replay_payload = json.dumps(
        [message.model_dump(mode="json") for message in provider.calls[-1]["messages"]],
        ensure_ascii=False,
    )
    assert "_agentos_compacted_tool_arguments" not in replay_payload
    assert "_invalid_provider_context_arguments" not in replay_payload
    assert "provider_context_omitted" not in replay_payload
    assert "tool-compact" not in replay_payload


@pytest.mark.asyncio
async def test_agent_repair_prompt_keeps_provider_request_from_ending_on_assistant(
    tmp_path,
) -> None:
    provider = TextThenCompactedToolArgumentsProvider()

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
        ),
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("make a deck")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 2
    repair_messages = provider.calls[1]["messages"]
    assert repair_messages[-1].role == "user"
    assert isinstance(repair_messages[-1].content, str)
    assert "Regenerate the complete tool arguments" in repair_messages[-1].content
    replay_payload = json.dumps(
        [message.model_dump(mode="json") for message in repair_messages],
        ensure_ascii=False,
    )
    assert "tool-compact" not in replay_payload
    assert "_invalid_provider_context_arguments" not in replay_payload
    assert "provider_context_omitted" not in replay_payload


def _history_has_tool_use(agent: Agent, tool_use_id: str) -> bool:
    return any(
        isinstance(block, ContentBlockToolUse) and block.id == tool_use_id
        for message in agent._history
        if isinstance(message.content, list)
        for block in message.content
    )


def test_agent_repair_prompt_handles_tool_use_without_tool_result() -> None:
    agent = Agent(provider=CapturingProvider(), config=AgentConfig())
    messages = [
        Message(role="user", content="make a deck"),
        Message(
            role="assistant",
            content=[
                ContentBlockText(text="I will prepare the file."),
                ContentBlockToolUse(
                    id="tool-compact",
                    name="write_file",
                    input={
                        "_agentos_compacted_tool_arguments": True,
                        "reason": "provider_context_omitted",
                    },
                ),
            ],
        ),
    ]

    stripped = list(messages)
    agent._seal_transcript(stripped, 1)

    assert stripped[-1].role == "user"
    assert isinstance(stripped[-1].content, str)
    assert "Regenerate the complete tool arguments" in stripped[-1].content
    replay_payload = json.dumps(
        [message.model_dump(mode="json") for message in stripped],
        ensure_ascii=False,
    )
    assert "tool-compact" not in replay_payload
    assert "provider_context_omitted" not in replay_payload


@pytest.mark.asyncio
async def test_agent_preserves_reasoning_content_for_deepseek_tool_replay() -> None:
    provider = ReasoningToolLoopCapturingProvider()

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
            thinking=ThinkingLevel.HIGH,
            model_id="deepseek-v4-flash",
            model_capabilities=ModelCapabilities(
                supports_reasoning=True,
                supports_tools=True,
                reasoning_format="deepseek",
            ),
        ),
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 2
    replay_messages = provider.calls[1]["messages"]
    assistant_replay = next(
        message
        for message in replay_messages
        if message.role == "assistant"
        and isinstance(message.content, list)
        and any(getattr(block, "type", None) == "tool_use" for block in message.content)
    )
    assert assistant_replay.reasoning_content == "I should call echo before finalizing."


@pytest.mark.asyncio
async def test_agent_preserves_reasoning_content_for_deepseek_text_replay() -> None:
    provider = CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            thinking=ThinkingLevel.HIGH,
            model_id="deepseek-v4-flash",
            model_capabilities=ModelCapabilities(
                supports_reasoning=True,
                supports_tools=True,
                reasoning_format="deepseek",
            ),
        ),
    )
    agent.set_history(
        [
            Message(role="user", content="old question"),
            Message(
                role="assistant",
                content=[ContentBlockText(text="old answer")],
                reasoning_content="I reasoned before answering.",
            ),
        ]
    )

    events = [event async for event in agent.run_turn("continue")]

    assert any(event.kind == "done" for event in events)
    assert provider.calls
    sent_assistant = provider.calls[0]["messages"][1]
    assert sent_assistant.reasoning_content == "I reasoned before answering."


@pytest.mark.asyncio
async def test_agent_preserves_direct_deepseek_v4_reasoning_without_capabilities() -> None:
    provider = CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            thinking=ThinkingLevel.HIGH,
            model_id="deepseek-v4-flash",
            model_capabilities=None,
        ),
    )
    agent.set_history(
        [
            Message(role="user", content="old question"),
            Message(
                role="assistant",
                content=[ContentBlockText(text="old answer")],
                reasoning_content="I reasoned before answering.",
            ),
        ]
    )

    events = [event async for event in agent.run_turn("continue")]

    assert any(event.kind == "done" for event in events)
    assert provider.calls
    sent_assistant = provider.calls[0]["messages"][1]
    assert sent_assistant.reasoning_content == "I reasoned before answering."


@pytest.mark.asyncio
async def test_agent_keeps_reasoning_in_the_transcript_for_reasoning_models() -> None:
    """Replayed reasoning is no longer dropped on load by default: it stays in
    the append-only transcript and each provider decides what to send (the
    OpenAI-compatible provider still omits ``reasoning_content`` for models
    that do not take it)."""
    provider = CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            thinking=ThinkingLevel.HIGH,
            model_id="custom-reasoning-model",
            model_capabilities=ModelCapabilities(
                supports_reasoning=True,
                supports_tools=True,
                reasoning_format="deepseek",
            ),
        ),
    )
    agent.set_history(
        [
            Message(role="user", content="old question"),
            Message(
                role="assistant",
                content=[
                    ContentBlockThinking(thinking="block one", signature="sig-1"),
                    ContentBlockRedactedThinking(data="opaque"),
                    ContentBlockText(text="old answer"),
                ],
                reasoning_content="I reasoned before answering.",
                reasoning_details=[{"type": "reasoning.text", "text": "r", "index": 0}],
            ),
        ]
    )

    events = [event async for event in agent.run_turn("continue")]

    assert any(event.kind == "done" for event in events)
    sent_assistant = provider.calls[0]["messages"][1]
    assert sent_assistant == agent._history[1]
    assert sent_assistant.reasoning_content == "I reasoned before answering."
    assert sent_assistant.reasoning_details == [
        {"type": "reasoning.text", "text": "r", "index": 0}
    ]
    assert [getattr(block, "type", None) for block in sent_assistant.content] == [
        "thinking",
        "redacted_thinking",
        "text",
    ]


@pytest.mark.asyncio
async def test_agent_drops_replayed_reasoning_only_for_models_without_reasoning() -> None:
    """Dropping reasoning on load is opt-in, decided by model capabilities."""
    provider = CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_id="plain-chat-model",
            model_capabilities=ModelCapabilities(
                supports_reasoning=False,
                supports_tools=True,
                reasoning_format="none",
            ),
        ),
    )
    agent.set_history(
        [
            Message(role="user", content="old question"),
            Message(
                role="assistant",
                content=[
                    ContentBlockThinking(thinking="block one", signature="sig-1"),
                    ContentBlockText(text="old answer"),
                ],
                reasoning_content="I reasoned before answering.",
            ),
        ]
    )

    events = [event async for event in agent.run_turn("continue")]

    assert any(event.kind == "done" for event in events)
    sent_assistant = provider.calls[0]["messages"][1]
    assert sent_assistant.reasoning_content is None
    assert [getattr(block, "type", None) for block in sent_assistant.content] == ["text"]
