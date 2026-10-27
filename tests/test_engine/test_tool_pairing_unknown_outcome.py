"""``repair_tool_pairing`` keeps calls and answers the unanswered ones."""

from __future__ import annotations

from agentos.engine.history import (
    UNKNOWN_TOOL_OUTCOME_TEXT,
    is_unknown_tool_outcome_result,
    repair_tool_pairing,
)
from agentos.provider import (
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
)
from agentos.provider.anthropic import _build_message_payload
from agentos.provider.openai import _build_openai_messages


def _call(*ids: str, text: str = "Placing the order now.") -> Message:
    return Message(
        role="assistant",
        content=[
            ContentBlockText(text=text),
            *(ContentBlockToolUse(id=i, name="trade", input={"amount": 1}) for i in ids),
        ],
    )


def _results(*ids: str) -> Message:
    return Message(
        role="user",
        content=[ContentBlockToolResult(tool_use_id=i, content=f"done {i}") for i in ids],
    )


def test_paired_history_is_returned_unchanged() -> None:
    messages = [Message(role="user", content="go"), _call("a", "b"), _results("a"), _results("b")]
    assert repair_tool_pairing(messages) is messages


def test_trailing_call_keeps_its_text_and_gets_an_unknown_outcome() -> None:
    messages = [Message(role="user", content="buy"), _call("a")]
    repaired = repair_tool_pairing(messages)

    assert repaired[1] is messages[1]  # text and call kept as they were
    stub = repaired[2].content[0]
    assert isinstance(stub, ContentBlockToolResult)
    assert stub.tool_use_id == "a"
    assert stub.is_error is True
    assert stub.content == UNKNOWN_TOOL_OUTCOME_TEXT
    assert is_unknown_tool_outcome_result(stub)
    # Deterministic: a reload repairs to the same messages.
    assert repair_tool_pairing(messages) == repaired


def test_missing_results_are_filled_in_call_order() -> None:
    messages = [Message(role="user", content="go"), _call("a", "b", "c"), _results("b")]
    repaired = repair_tool_pairing(messages)

    results = repaired[2].content
    assert [block.tool_use_id for block in results] == ["a", "b", "c"]
    assert [is_unknown_tool_outcome_result(block) for block in results] == [True, False, True]
    assert results[1].content == "done b"
    assert len(repaired) == 3


def test_orphan_and_foreign_results_are_dropped() -> None:
    messages = [
        _results("ghost"),
        Message(role="user", content="go"),
        _call("a"),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(tool_use_id="a", content="done a"),
                ContentBlockToolResult(tool_use_id="other", content="stray"),
            ],
        ),
    ]
    repaired = repair_tool_pairing(messages)

    assert [m.role for m in repaired] == ["user", "assistant", "user"]
    assert [block.tool_use_id for block in repaired[2].content] == ["a"]


def test_call_followed_by_a_user_message_gets_its_stub_before_it() -> None:
    messages = [Message(role="user", content="go"), _call("a"), Message(role="user", content="?")]
    repaired = repair_tool_pairing(messages)
    assert [m.role for m in repaired] == ["user", "assistant", "user", "user"]
    assert is_unknown_tool_outcome_result(repaired[2].content[0])
    assert repaired[3].content == "?"


def _repaired_turn() -> list[Message]:
    return repair_tool_pairing(
        [Message(role="user", content="buy"), _call("call_1"), Message(role="user", content="?")]
    )


def test_anthropic_accepts_the_unknown_outcome_result() -> None:
    payloads = [_build_message_payload(message) for message in _repaired_turn()]

    assert [payload["role"] for payload in payloads] == ["user", "assistant", "user", "user"]
    assert payloads[1]["content"][-1] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "trade",
        "input": {"amount": 1},
    }
    result = payloads[2]["content"][0]
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "call_1"
    assert result["is_error"] is True
    assert result["content"] == UNKNOWN_TOOL_OUTCOME_TEXT


def test_openai_accepts_the_unknown_outcome_result() -> None:
    payloads = [
        payload for message in _repaired_turn() for payload in _build_openai_messages(message)
    ]

    roles = [payload["role"] for payload in payloads]
    assert roles == ["user", "assistant", "tool", "user"]
    assert payloads[1]["tool_calls"][0]["id"] == "call_1"
    assert payloads[1]["content"]  # the assistant text is kept
    assert payloads[2]["tool_call_id"] == "call_1"
    assert UNKNOWN_TOOL_OUTCOME_TEXT in str(payloads[2]["content"])
