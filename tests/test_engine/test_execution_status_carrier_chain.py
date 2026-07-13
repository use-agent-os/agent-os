from __future__ import annotations

from dataclasses import fields

from agentos.engine.history import reconstruct_messages_from_entry
from agentos.engine.runtime import _persisted_tool_result_segment
from agentos.engine.session_sanitize import sanitize_session_messages
from agentos.engine.types import ToolResultEvent
from agentos.provider.types import ContentBlockToolResult, Message
from agentos.tool_boundary import ToolResult


def _failure_status() -> dict[str, object]:
    return {
        "version": 1,
        "status": "error",
        "exit_code": 2,
        "timed_out": False,
        "truncated": False,
        "reason": "nonzero_exit",
        "source": "adapter",
        "preservation_class": "diagnostic",
    }


def test_tool_result_declares_nested_execution_status_field() -> None:
    assert "execution_status" in {field.name for field in fields(ToolResult)}


def test_tool_result_event_declares_nested_execution_status_field() -> None:
    assert "execution_status" in {field.name for field in fields(ToolResultEvent)}


def test_persisted_tool_result_segment_includes_nested_execution_status() -> None:
    status = _failure_status()

    segment = _persisted_tool_result_segment(
        ToolResultEvent(
            tool_use_id="call_status_1",
            tool_name="exec_command",
            result="failed",
            is_error=True,
            execution_status=status,
        )
    )

    assert segment["execution_status"] == status


def test_persisted_tool_result_segment_marks_sidecar_truncated() -> None:
    status = {
        "version": 1,
        "status": "success",
        "exit_code": 0,
        "timed_out": False,
        "truncated": False,
        "reason": None,
        "source": "adapter",
        "preservation_class": "normal",
    }

    segment = _persisted_tool_result_segment(
        ToolResultEvent(
            tool_use_id="call_status_truncated",
            tool_name="exec_command",
            result="exit_code=0\n" + ("x" * 1000),
            is_error=False,
            execution_status=status,
        ),
        max_chars=120,
    )

    assert segment["result_truncated"] is True
    assert segment["execution_status"]["status"] == "success"
    assert segment["execution_status"]["truncated"] is True
    assert segment["execution_status"]["preservation_class"] == "retain_summary"


def test_history_reconstructs_tool_result_execution_status() -> None:
    status = _failure_status()

    messages = reconstruct_messages_from_entry(
        "assistant",
        "",
        [
            {
                "type": "tool_use",
                "tool_use_id": "call_status_2",
                "name": "exec_command",
                "input": {"cmd": "false"},
            },
            {
                "type": "tool_result",
                "tool_use_id": "call_status_2",
                "name": "exec_command",
                "result": "failed",
                "is_error": True,
                "execution_status": status,
            },
        ],
    )

    result_block = messages[1].content[0]
    assert isinstance(result_block, ContentBlockToolResult)
    assert result_block.execution_status == status


def test_history_reconstructs_legacy_tool_result_execution_status() -> None:
    messages = reconstruct_messages_from_entry(
        "assistant",
        "",
        [
            {
                "type": "tool_use",
                "tool_use_id": "call_status_legacy",
                "name": "exec_command",
                "input": {"cmd": "false"},
            },
            {
                "type": "tool_result",
                "tool_use_id": "call_status_legacy",
                "name": "exec_command",
                "result": "failed",
                "is_error": True,
            },
        ],
    )

    result_block = messages[1].content[0]
    assert isinstance(result_block, ContentBlockToolResult)
    assert result_block.execution_status is not None
    assert result_block.execution_status["source"] == "legacy"
    assert result_block.execution_status["reason"] == "legacy_missing_status"
    assert result_block.execution_status["status"] == "error"


def test_session_sanitize_preserves_tool_result_execution_status() -> None:
    status = _failure_status()
    message = Message.model_construct(
        role="user",
        content=[
            {
                "type": "tool_result",
                "tool_use_id": "call_status_3",
                "content": "failed",
                "is_error": True,
                "execution_status": status,
                "debug_metadata": {"drop": True},
            }
        ],
        reasoning_content=None,
    )

    sanitized, result = sanitize_session_messages([message])

    block = sanitized[0].content[0]
    assert result.metadata_keys_removed == 1
    assert isinstance(block, ContentBlockToolResult)
    assert block.execution_status == status
