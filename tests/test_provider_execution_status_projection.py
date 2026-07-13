from __future__ import annotations

import json

from agentos.provider.anthropic import _build_message_payload
from agentos.provider.openai import _build_openai_messages
from agentos.provider.types import ContentBlockToolResult, Message


def _failure_status() -> dict[str, object]:
    return {
        "version": 1,
        "status": "error",
        "exit_code": 1,
        "timed_out": False,
        "truncated": True,
        "reason": "nonzero_exit",
        "source": "adapter",
        "preservation_class": "diagnostic",
    }


def test_anthropic_projects_native_is_error_from_execution_status() -> None:
    message = Message(
        role="user",
        content=[
            ContentBlockToolResult(
                tool_use_id="call_provider_1",
                content="command failed",
                is_error=False,
                execution_status=_failure_status(),
            )
        ],
    )

    payload = _build_message_payload(message)

    assert payload["content"][0]["is_error"] is True


def test_openai_failure_tool_result_includes_bounded_execution_status_envelope() -> None:
    large_output = "failure details\n" + ("x" * 20_000)
    message = Message(
        role="user",
        content=[
            ContentBlockToolResult(
                tool_use_id="call_provider_2",
                content=large_output,
                is_error=True,
                execution_status=_failure_status(),
            )
        ],
    )

    payload = _build_openai_messages(message)

    tool_content = json.loads(payload[0]["content"])
    assert tool_content["execution_status"] == {
        "version": 1,
        "status": "error",
        "exit_code": 1,
        "timed_out": False,
        "truncated": True,
        "reason": "nonzero_exit",
    }
    assert tool_content["output"].startswith("failure details")
    assert len(tool_content["output"]) < len(large_output)
