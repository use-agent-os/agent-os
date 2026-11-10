"""The projector keeps every field of the calls and results it rewrites."""

from __future__ import annotations

from typing import Any

import pytest

from agentos.engine import Agent, AgentConfig, ToolCall, ToolResult


class _Provider:
    provider_name = "fake"

    def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
        raise AssertionError("provider should not be used")

    async def list_models(self) -> list[Any]:
        return []


def _agent(**config: Any) -> Agent:
    return Agent(provider=_Provider(), config=AgentConfig(**config))


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "[tool_use_argument_projection]\nhead:\nabc"},
        {"_invalid_provider_context_arguments": True, "tool": "read"},
    ],
    ids=["placeholder-string", "compacted-marker"],
)
def test_sanitized_tool_call_keeps_its_thought_signature(arguments: dict[str, Any]) -> None:
    call = ToolCall(
        tool_use_id="t1",
        tool_name="read",
        arguments=arguments,
        origin_trace="trace",
        thought_signature="sig-123",
    )

    sanitized = _agent()._projection._sanitize_projected_tool_call_arguments(call)

    assert sanitized is not call
    assert sanitized.arguments != arguments
    assert sanitized.thought_signature == "sig-123"
    assert sanitized.origin_trace == "trace"
    assert sanitized.tool_use_id == "t1"


async def test_projection_keeps_the_effective_arguments() -> None:
    result = ToolResult(
        tool_use_id="t1",
        tool_name="read",
        content='{"blob": "' + ("x" * 200_000) + '"}',
        effective_arguments={"path": "safe.txt"},
    )

    projected = await _agent(context_window_tokens=2_000)._projection._project_tool_result_for_llm(
        result
    )

    assert projected.content != result.content
    assert projected.effective_arguments == {"path": "safe.txt"}
