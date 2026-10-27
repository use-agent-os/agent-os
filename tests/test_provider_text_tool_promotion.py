"""Text-form tool calls: what the user sees must match what is executed.

A model on the OpenAI-compatible path sometimes writes its tool call into the
reply text. The provider promotes such text into synthetic tool calls and the
engine's stream guard hides it. Both read one format table, so every case
below checks the pair together: hidden text was executed, and text that was
not executed is shown as written.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from agentos.engine.tool_text_compat import (
    ProtocolTextLeakGuard,
    strip_synthetic_tool_call_suffix,
)
from agentos.provider import ChatConfig, Message
from agentos.provider.openai import OpenAIProvider, _synthesize_text_tool_events
from agentos.provider.text_tool_protocol import (
    MAX_PAYLOAD_BYTES,
    MAX_TOOL_NAME_CHARS,
    find_text_tool_regions,
    has_unpromoted_text_tool_calls,
    promote_text_tool_calls,
)
from agentos.provider.types import ToolDefinition, ToolInputSchema, ToolUseEndEvent


def _tool(name: str, properties: dict[str, object] | None = None) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=name,
        input_schema=ToolInputSchema(type="object", properties=properties or {}),
    )


TOOLS = [
    _tool("read_file", {"path": {"type": "string"}, "limit": {"type": "integer"}}),
    _tool("exec", {"cmd": {"type": "string"}}),
    _tool("write_file"),
]


@dataclass
class Outcome:
    visible: str
    executed: list[tuple[str, dict[str, object]]]


def _run(text: str, *, chunk: int | None = None) -> Outcome:
    """Stream ``text`` through the guard, then settle it as the engine does."""
    guard = ProtocolTextLeakGuard()
    size = chunk or len(text) or 1
    visible = "".join(guard.push(text[i : i + size]) for i in range(0, len(text), size))
    events = _synthesize_text_tool_events(text, TOOLS)
    ends = [event for event in events if isinstance(event, ToolUseEndEvent)]
    if ends:
        visible += guard.flush_before_tool_use(synthetic_from_text=True)
    else:
        visible += guard.flush()
    return Outcome(visible, [(end.tool_name, end.arguments) for end in ends])


def _assert_consistent(text: str, outcome: Outcome) -> None:
    # The stored reply (agent side) must equal what was streamed to the user.
    names = [name for name, _ in outcome.executed]
    assert strip_synthetic_tool_call_suffix(text, names) == outcome.visible
    # Character-by-character streaming must reach the same result.
    assert _run(text, chunk=1) == outcome


FENCED_EXAMPLE = (
    "A call to the exec tool looks like this:\n\n"
    "```xml\n"
    '<invoke name="exec">\n'
    '<parameter name="cmd">rm -rf /tmp/scratch</parameter>\n'
    "</invoke>\n"
    "```\n\n"
    "The engine runs it for you."
)

ISSUE_ROWS = [
    pytest.param(
        'Let me check.\n<tool_call>{"name":"read_file","arguments":{"path":"a.txt"}}</tool_call>',
        "Let me check.",
        [("read_file", {"path": "a.txt"})],
        id="tool_call-json",
    ),
    pytest.param(
        "<function=read_file><parameter=path>a.txt</parameter></function>",
        "",
        [("read_file", {"path": "a.txt"})],
        id="function-equals",
    ),
    pytest.param(
        "<|channel|>analysis<|message|>Need to read the file first.<|end|>"
        "<|start|>assistant<|channel|>commentary to=functions.read_file "
        '<|constrain|>json<|message|>{"path":"a.txt"}<|call|>',
        "",
        [("read_file", {"path": "a.txt"})],
        id="channel-markup",
    ),
    pytest.param(FENCED_EXAMPLE, FENCED_EXAMPLE, [], id="fenced-example"),
]


@pytest.mark.parametrize(("text", "visible", "executed"), ISSUE_ROWS)
def test_issue_rows(text: str, visible: str, executed: list[tuple[str, dict]]) -> None:
    outcome = _run(text)

    assert outcome.visible == visible
    assert outcome.executed == executed
    _assert_consistent(text, outcome)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(FENCED_EXAMPLE, id="backtick-fence"),
        pytest.param(FENCED_EXAMPLE.replace("```", "~~~~"), id="tilde-fence"),
        pytest.param(
            'Use `<tool_call>{"name":"exec","arguments":{"cmd":"ls"}}</tool_call>` to run it.',
            id="inline-code",
        ),
        pytest.param(
            '`<invoke name="exec"><parameter name="cmd">ls</parameter></invoke>`',
            id="inline-code-whole-line",
        ),
        pytest.param('Example:\n```\nexec{"cmd": "ls"}\n```', id="plain-json-in-fence"),
        pytest.param('Example:\n```\nexec{"cmd": "ls"}', id="plain-json-in-open-fence"),
        pytest.param(
            '    <invoke name="exec"><parameter name="cmd">ls</parameter></invoke>',
            id="indented-code-block",
        ),
    ],
)
def test_code_examples_are_neither_run_nor_hidden(text: str) -> None:
    outcome = _run(text)

    assert outcome.executed == []
    assert outcome.visible == text


def test_example_in_prose_mid_line_is_not_run() -> None:
    text = 'You would write <invoke name="exec"><parameter name="cmd">ls</parameter></invoke>.'

    assert _run(text) == Outcome(text, [])


def test_unknown_tool_is_not_run_and_stays_visible() -> None:
    text = 'Let me check.\n<tool_call>{"name":"delete_everything","arguments":{}}</tool_call>'

    assert _run(text) == Outcome(text, [])
    assert has_unpromoted_text_tool_calls(text, {t.name for t in TOOLS})


@pytest.mark.parametrize("name", ["functions.read_file", "Read_File", "read_file:0"])
def test_decorated_tool_name_resolves_like_a_native_call(name: str) -> None:
    text = f'<tool_call>{{"name":"{name}","arguments":{{"path":"a"}}}}</tool_call>'

    assert _run(text) == Outcome("", [("read_file", {"path": "a"})])
    assert not has_unpromoted_text_tool_calls(text, {t.name for t in TOOLS})


def test_one_unknown_tool_blocks_the_whole_reply() -> None:
    text = (
        '<tool_call>{"name":"read_file","arguments":{"path":"a"}}</tool_call>\n'
        '<tool_call>{"name":"not_offered","arguments":{}}</tool_call>'
    )

    assert _run(text) == Outcome(text, [])


def test_oversize_payload_is_rejected_and_shown() -> None:
    content = "x" * (MAX_PAYLOAD_BYTES + 1)
    text = (
        "Writing it.\n"
        '<invoke name="write_file"><parameter name="path">big.txt</parameter>'
        f'<parameter name="content">{content}</parameter></invoke>'
    )

    outcome = _run(text, chunk=4096)

    assert outcome.executed == []
    assert outcome.visible == text


def test_payload_cap_counts_utf8_bytes() -> None:
    content = "é" * (MAX_PAYLOAD_BYTES // 2)  # under the cap in chars, over it in bytes
    text = f"<function=write_file><parameter=content>{content}</parameter></function>"

    assert promote_text_tool_calls(text, {"write_file"}) is None


def test_overlong_tool_name_is_rejected() -> None:
    name = "t" * (MAX_TOOL_NAME_CHARS + 1)
    text = f'<tool_call>{{"name":"{name}","arguments":{{}}}}</tool_call>'

    assert promote_text_tool_calls(text, {name}) is None
    assert promote_text_tool_calls(text.replace(name, name[:-1]), {name[:-1]}) is not None


def test_mixed_prose_and_call_keeps_the_prose() -> None:
    text = (
        "I'll look at the config first.\n\n"
        '<tool_call>{"name":"read_file","arguments":{"path":"config.toml"}}</tool_call>\n\n'
        "Then I will summarise what changed."
    )

    outcome = _run(text)

    assert outcome.executed == [("read_file", {"path": "config.toml"})]
    assert outcome.visible == (
        "I'll look at the config first.\n\nThen I will summarise what changed."
    )
    _assert_consistent(text, outcome)


def test_prose_between_calls_means_explaining_not_calling() -> None:
    text = (
        '<tool_call>{"name":"read_file","arguments":{"path":"a"}}</tool_call>\n'
        "and then, after that one,\n"
        '<tool_call>{"name":"exec","arguments":{"cmd":"ls"}}</tool_call>'
    )

    assert _run(text) == Outcome(text, [])


def test_several_calls_in_one_block_all_run_in_order() -> None:
    text = (
        "Two reads.\n"
        '<invoke name="read_file"><parameter name="path">a</parameter></invoke>\n'
        '<invoke name="read_file"><parameter name="path">b</parameter></invoke>'
    )

    outcome = _run(text)

    assert outcome.executed == [("read_file", {"path": "a"}), ("read_file", {"path": "b"})]
    assert outcome.visible == "Two reads."
    _assert_consistent(text, outcome)


def test_wrapped_function_form_with_newlines() -> None:
    text = (
        "<tool_call>\n<function=read_file>\n<parameter=path>\nsrc/app.py\n</parameter>\n"
        "<parameter=limit>\n40\n</parameter>\n</function>\n</tool_call>"
    )

    outcome = _run(text)

    # limit is declared an integer, so its text value reaches the tool as one.
    assert outcome.executed == [("read_file", {"path": "src/app.py", "limit": 40})]
    assert outcome.visible == ""


def test_channel_markup_with_recipient_before_channel() -> None:
    text = (
        "<|start|>assistant to=functions.exec<|channel|>commentary json"
        '<|message|>{"cmd": "pwd"}<|call|>'
    )

    assert _run(text) == Outcome("", [("exec", {"cmd": "pwd"})])


def test_channel_final_answer_is_shown() -> None:
    text = "<|channel|>final<|message|>The file has 3 lines.<|end|>"

    assert _run(text) == Outcome(text, [])
    assert not has_unpromoted_text_tool_calls(text, {"read_file"})


def test_tool_call_json_arguments_may_be_a_string() -> None:
    text = '<tool_call>{"name":"exec","arguments":"{\\"cmd\\": \\"ls\\"}"}</tool_call>'

    assert _run(text) == Outcome("", [("exec", {"cmd": "ls"})])


def test_truncated_call_is_shown_not_dropped() -> None:
    text = 'On it.\n<tool_call>{"name":"read_file","argum'

    assert _run(text) == Outcome(text, [])
    assert has_unpromoted_text_tool_calls(text, {"read_file"})


def test_plain_json_fallback_still_runs() -> None:
    text = 'Here is the answer.\n\nread_file{"path": "a.txt"}'

    outcome = _run(text)

    assert outcome.executed == [("read_file", {"path": "a.txt"})]
    assert strip_synthetic_tool_call_suffix(text, ["read_file"]) == "Here is the answer."


def test_plain_json_fallback_does_not_run_beside_unpromoted_markup() -> None:
    # The markup stays visible because it did not run; a plain-JSON call in
    # the same reply must not be promoted around it.
    text = (
        '<invoke name="not_offered"><parameter name="a">1</parameter></invoke>\n\n'
        'read_file{"path": "a.txt"}'
    )

    assert _run(text) == Outcome(text, [])


def test_false_opener_is_released_without_waiting_for_the_end() -> None:
    guard = ProtocolTextLeakGuard()

    assert guard.push("Intro.\n<tool_callback> is a ") == "Intro.\n<tool_callback> is a "
    assert guard.push("different thing.") == "different thing."


def test_native_tool_use_releases_held_markup() -> None:
    # A native tool call means nothing promoted the text markup, so it shows.
    guard = ProtocolTextLeakGuard()
    text = '<invoke name="exec"><parameter name="cmd">ls</parameter></invoke>'

    assert guard.push(text) == ""
    assert guard.flush_before_tool_use(synthetic_from_text=False) == text


def test_regions_skip_fenced_code_but_see_calls_after_it() -> None:
    text = (
        '```\n<invoke name="exec"></invoke>\n```\n'
        '<invoke name="read_file"><parameter name="path">a</parameter></invoke>'
    )

    (region,) = find_text_tool_regions(text)

    assert region.calls[0].name == "read_file"
    assert _run(text).executed == [("read_file", {"path": "a"})]


def test_has_unpromoted_text_tool_calls() -> None:
    allowed = {"read_file"}
    call = '<tool_call>{"name":"read_file","arguments":{"path":"a"}}</tool_call>'

    assert not has_unpromoted_text_tool_calls("Just prose.", allowed)
    assert not has_unpromoted_text_tool_calls(call, allowed)
    assert has_unpromoted_text_tool_calls(call, {"exec"})
    assert not has_unpromoted_text_tool_calls(f"```\n{call}\n```", {"exec"})


def _stream_through_provider(monkeypatch: pytest.MonkeyPatch, text: str) -> list[ToolUseEndEvent]:
    """Send ``text`` as streamed content deltas through the real provider."""
    deltas = [text[i : i + 7] for i in range(0, len(text), 7)]
    chunks = [{"choices": [{"delta": {"content": d}, "finish_reason": None}]} for d in deltas]
    chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    body = b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        )
    )
    real_client = httpx.AsyncClient

    def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("agentos.provider.openai.httpx.AsyncClient", patched)
    provider = OpenAIProvider(api_key="k", model="m", base_url="http://127.0.0.1:9/v1")

    async def run() -> list[object]:
        messages = [Message(role="user", content="go")]
        return [ev async for ev in provider.chat(messages, tools=TOOLS, config=ChatConfig())]

    return [ev for ev in asyncio.run(run()) if isinstance(ev, ToolUseEndEvent)]


def test_provider_stream_promotes_a_tool_call_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    text = 'Let me check.\n<tool_call>{"name":"read_file","arguments":{"path":"a.txt"}}</tool_call>'

    (end,) = _stream_through_provider(monkeypatch, text)

    assert (end.tool_name, end.arguments, end.synthetic_from_text) == (
        "read_file",
        {"path": "a.txt"},
        True,
    )


def test_provider_stream_never_runs_a_fenced_example(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _stream_through_provider(monkeypatch, FENCED_EXAMPLE) == []
