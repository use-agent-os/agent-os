"""Detector and streaming gate for tool calls written out as text."""

from __future__ import annotations

import pytest

from agentos.engine.text_tool_call_detect import (
    TextToolCallStreamGate,
    detect_text_tool_call,
    offered_tool_names,
    text_tool_call_nudge,
    text_tool_call_to_retry,
)
from agentos.provider import ToolDefinition, ToolInputSchema

TOOLS = frozenset({"read_file", "exec_command"})

FENCE = "```"


def _fenced(body: str, lang: str = "json") -> str:
    return f"{FENCE}{lang}\n{body}\n{FENCE}"


CALL_JSON = '{"name": "read_file", "arguments": {"path": "notes.txt"}}'

POSITIVE_CASES = [
    pytest.param(_fenced(CALL_JSON), "read_file", "json_call", id="fenced-json"),
    pytest.param(
        "Let me open the notes first.\n\n" + _fenced(CALL_JSON) + "\n",
        "read_file",
        "json_call",
        id="fenced-json-after-intent",
    ),
    pytest.param(_fenced(CALL_JSON, lang=""), "read_file", "json_call", id="fence-no-language"),
    pytest.param(
        '{"tool": "exec_command", "args": {"cmd": "ls"}}', "exec_command", "json_call", id="bare"
    ),
    pytest.param(
        '{"type": "function", "function": {"name": "read_file", '
        '"arguments": "{\\"path\\": \\"a.txt\\"}"}}',
        "read_file",
        "json_call",
        id="function-envelope-string-arguments",
    ),
    pytest.param(
        '[{"name": "read_file", "arguments": {"path": "a"}}, '
        '{"name": "exec_command", "arguments": {"cmd": "ls"}}]',
        "read_file",
        "json_call",
        id="list-of-calls",
    ),
    pytest.param(
        '{"tool_calls": [{"name": "read_file", "arguments": {"path": "a"}}]}',
        "read_file",
        "json_call",
        id="tool-calls-wrapper",
    ),
    pytest.param(
        "Thought: I need the file contents.\nAction: read_file\n"
        'Action Input: {"path": "notes.txt"}',
        "read_file",
        "react",
        id="react",
    ),
    pytest.param(
        "Action: `exec_command`\nAction Input: ls -la\nObservation:",
        "exec_command",
        "react",
        id="react-empty-observation",
    ),
    pytest.param(
        "I'll call `read_file` with:\n" + '{"path": "notes.txt"}',
        "read_file",
        "announced_args",
        id="announced-bare-arguments",
    ),
    pytest.param(
        "Let me use exec_command:\n" + _fenced('{"cmd": "pytest -q"}'),
        "exec_command",
        "announced_args",
        id="announced-fenced-arguments",
    ),
    pytest.param(
        'read_file({"path": "notes.txt"})', "read_file", "named_args", id="name-paren-json"
    ),
    pytest.param(
        _fenced('exec_command {"cmd": "ls"}', lang="tool_code"),
        "exec_command",
        "named_args",
        id="fenced-name-json",
    ),
    pytest.param(
        "<tool_call>\n" + CALL_JSON + "\n</tool_call>",
        "read_file",
        "markup",
        id="tool-call-markup",
    ),
    pytest.param(
        '<invoke name="read_file">\n<parameter name="path">a</parameter>\n</invoke>',
        "read_file",
        "markup",
        id="invoke-markup",
    ),
]

NEGATIVE_CASES = [
    pytest.param("", id="empty"),
    pytest.param("The read_file tool reads a file from the workspace.", id="prose-mention"),
    pytest.param(
        "To read a file, you can send this request:\n" + _fenced(CALL_JSON),
        id="explains-syntax-you-can",
    ),
    pytest.param("For example:\n" + CALL_JSON, id="explains-for-example"),
    pytest.param(
        "The call format looks like this:\n\n" + _fenced(CALL_JSON),
        id="explains-format",
    ),
    pytest.param("Here is the output:\n" + _fenced(CALL_JSON), id="presents-output"),
    pytest.param(
        _fenced(CALL_JSON) + "\nThat is the whole request; the tool answers with the text.",
        id="prose-after-payload",
    ),
    pytest.param(
        _fenced('{"name": "drop_database", "arguments": {}}'),
        id="tool-not-offered",
    ),
    pytest.param(
        _fenced(
            '{"name": "read_file", "description": "Read a file", '
            '"parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}'
        ),
        id="tool-schema",
    ),
    pytest.param(
        _fenced('{"name": "read_file", "arguments": {"type": "object", "properties": {}}}'),
        id="arguments-are-a-schema",
    ),
    pytest.param(_fenced('{"path": "notes.txt", "size": 120}'), id="plain-json-data"),
    pytest.param(
        "I'll read the file:\n" + '{"path": "notes.txt"}',
        id="announcement-without-tool-name",
    ),
    pytest.param(
        "Let me explain how read_file is used:\n" + '{"path": "notes.txt"}',
        id="announcement-explaining",
    ),
    pytest.param(_fenced('read_file({"path": "a"})', lang="python"), id="python-code"),
    pytest.param(
        'Action: read_file\nAction Input: {"path": "a"}\nObservation: hello\nFinal Answer: hello',
        id="react-worked-example",
    ),
    pytest.param(
        _fenced('Action: read_file\nAction Input: {"path": "a"}', lang="text"),
        id="react-inside-text-fence",
    ),
    pytest.param(
        "Action: delete_everything\nAction Input: {}",
        id="react-unknown-tool",
    ),
    pytest.param(
        '{"name": "read_file", "arguments": {"path": "a"}, "status": "done", "output": "hi"}',
        id="call-record-with-extra-keys",
    ),
    pytest.param("Done. I read notes.txt and it says hello.", id="ordinary-answer"),
]


@pytest.mark.parametrize(("text", "tool", "shape"), POSITIVE_CASES)
def test_detector_flags_written_out_calls(text: str, tool: str, shape: str) -> None:
    match = detect_text_tool_call(text, TOOLS)
    assert match is not None
    assert match.tool_name == tool
    assert match.shape == shape


@pytest.mark.parametrize("text", NEGATIVE_CASES)
def test_detector_leaves_ordinary_text_alone(text: str) -> None:
    assert detect_text_tool_call(text, TOOLS) is None


def test_detector_needs_offered_tools() -> None:
    assert detect_text_tool_call(_fenced(CALL_JSON), frozenset()) is None


def test_offered_tool_names_reads_definitions() -> None:
    tools = [
        ToolDefinition(name="read_file", description="Read.", input_schema=ToolInputSchema()),
        ToolDefinition(name=" ", description="Blank.", input_schema=ToolInputSchema()),
    ]
    assert offered_tool_names(tools) == frozenset({"read_file"})
    assert offered_tool_names(None) == frozenset()


def test_nudge_names_the_tool() -> None:
    match = detect_text_tool_call(_fenced(CALL_JSON), TOOLS)
    assert match is not None
    nudge = text_tool_call_nudge(match)
    assert "`read_file`" in nudge
    assert "tool-calling interface" in nudge


# ── streaming gate ──────────────────────────────────────────────────────────


def _stream(gate: TextToolCallStreamGate, text: str, step: int = 1) -> str:
    return "".join(gate.push(text[i : i + step]) for i in range(0, len(text), step))


@pytest.mark.parametrize("step", [1, 3, 1000])
@pytest.mark.parametrize(("text", "tool", "shape"), POSITIVE_CASES)
def test_gate_holds_every_detected_payload(text: str, tool: str, shape: str, step: int) -> None:
    gate = TextToolCallStreamGate(TOOLS)
    shown = _stream(gate, text, step)
    match = detect_text_tool_call(text, TOOLS)
    assert match is not None
    assert gate.hold_start is not None
    assert gate.hold_start <= match.start
    assert shown == text[: gate.hold_start]
    assert text_tool_call_to_retry(
        text=text,
        gate=gate,
        produced_tool_calls=False,
        stream_complete=True,
        stop_reason="end_turn",
    )


@pytest.mark.parametrize("step", [1, 4])
def test_gate_passes_plain_prose_through_without_waiting_for_the_end(step: int) -> None:
    gate = TextToolCallStreamGate(TOOLS)
    text = "The answer is 42.\nThe tests pass and the build is green.\nNothing else to do"
    shown = _stream(gate, text, step)
    assert shown == text
    assert gate.hold_start is None


def test_gate_releases_json_once_prose_follows_it() -> None:
    gate = TextToolCallStreamGate(TOOLS)
    head = "Config:\n" + _fenced('{"debug": true}') + "\n"
    shown = _stream(gate, head)
    assert shown == "Config:\n"  # fenced JSON is held while it is the last thing
    shown += _stream(gate, "It turns debug on.")
    assert shown == head + "It turns debug on."
    assert gate.hold_start is None


def test_gate_releases_announcement_followed_by_prose() -> None:
    gate = TextToolCallStreamGate(TOOLS)
    text = "Let me check read_file output.\nIt printed three lines."
    assert _stream(gate, text) == text


def test_gate_streams_inside_a_foreign_code_fence() -> None:
    gate = TextToolCallStreamGate(TOOLS)
    text = _fenced('def f():\n    return {"name": "read_file"}\n', lang="python") + "\n"
    assert _stream(gate, text) == text


def test_gate_release_and_discard() -> None:
    gate = TextToolCallStreamGate(TOOLS)
    text = "Reading it now.\n" + _fenced(CALL_JSON)
    shown = _stream(gate, text)
    assert shown == "Reading it now.\n"
    assert gate.held_text == _fenced(CALL_JSON)
    assert gate.release() == _fenced(CALL_JSON)
    assert gate.hold_start is None
    assert gate.push(" more") == " more"  # released gates stop holding

    dropped = TextToolCallStreamGate(TOOLS)
    _stream(dropped, text)
    assert dropped.discard() == _fenced(CALL_JSON)
    assert dropped.hold_start is None


def test_gate_without_tools_is_a_pass_through() -> None:
    gate = TextToolCallStreamGate(frozenset())
    assert _stream(gate, _fenced(CALL_JSON)) == _fenced(CALL_JSON)


# ── retry decision ──────────────────────────────────────────────────────────


def _held_gate(text: str) -> TextToolCallStreamGate:
    gate = TextToolCallStreamGate(TOOLS)
    _stream(gate, text)
    return gate


@pytest.mark.parametrize("stop", ["end_turn", "stop", "stop_sequence", "tool_use", None])
def test_retry_decision_accepts_normal_stops(stop: str | None) -> None:
    text = _fenced(CALL_JSON)
    match = text_tool_call_to_retry(
        text=text,
        gate=_held_gate(text),
        produced_tool_calls=False,
        stream_complete=True,
        stop_reason=stop,
    )
    assert match is not None and match.tool_name == "read_file"


@pytest.mark.parametrize(
    ("produced_tool_calls", "stream_complete", "stop"),
    [
        (True, True, "end_turn"),  # a call came through (native or promoted)
        (False, False, "end_turn"),  # the stream broke off
        (False, True, "max_tokens"),
        (False, True, "refusal"),
        (False, True, "pause_turn"),
    ],
)
def test_retry_decision_rejects_other_outcomes(
    produced_tool_calls: bool, stream_complete: bool, stop: str
) -> None:
    text = _fenced(CALL_JSON)
    assert (
        text_tool_call_to_retry(
            text=text,
            gate=_held_gate(text),
            produced_tool_calls=produced_tool_calls,
            stream_complete=stream_complete,
            stop_reason=stop,
        )
        is None
    )


def test_retry_decision_skips_a_call_already_on_screen() -> None:
    text = _fenced(CALL_JSON)
    gate = _held_gate(text)
    gate.release()
    assert (
        text_tool_call_to_retry(
            text=text,
            gate=gate,
            produced_tool_calls=False,
            stream_complete=True,
            stop_reason="end_turn",
        )
        is None
    )


def test_gate_stops_holding_an_oversized_payload() -> None:
    from agentos.engine.text_tool_call_detect import MAX_HOLD_CHARS

    gate = TextToolCallStreamGate(TOOLS)
    head = '{"name": "read_file", "arguments": {"path": "'
    assert gate.push(head) == ""
    filler = "x" * (MAX_HOLD_CHARS + 10)
    assert gate.push(filler) == head + filler
    assert gate.hold_start is None


def test_gate_handles_a_long_reply_in_small_deltas() -> None:
    line = "Let me check this. The read_file tool says the build is green.\n"
    text = line * 2000 + "All done."
    gate = TextToolCallStreamGate(TOOLS)
    assert _stream(gate, text, step=5) == text
    assert gate.hold_start is None
