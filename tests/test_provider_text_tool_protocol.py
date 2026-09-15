"""Text-encoded tool calls must be executed, not just hidden (issue #1514).

`engine.tool_text_compat` recognises several wrapper variants and scrubs them
from what the user sees. `_synthesize_text_tool_events` only recognised the
literal `<minimax:tool_call>` wrapper, so for every other variant the markup
was hidden and the action was silently never performed — nothing looked wrong.
The two fixtures below are the ones already pinned in
tests/test_core_lightweight_contracts.py as text the user must not see.
"""

from __future__ import annotations

import pytest

from agentos.engine.tool_text_compat import strip_protocol_text_leak
from agentos.provider.openai import _synthesize_text_tool_events
from agentos.provider.types import ToolDefinition, ToolInputSchema, ToolUseEndEvent

TVOE_TEXT = (
    "Let me write the dashboard now.\n\n"
    '<tvoe_calls><invoke name="write_file">'
    '<parameter name="path">index.html</parameter>'
    '<parameter name="content"><!DOCTYPE html><html><body>app</body></html>'
    "</parameter></invoke></tvoe_calls>"
)

DSML_TEXT = (
    "Let me create the printable daily record sheet as well:\n\n"
    '<｜DSML｜tool_calls><｜DSML｜invoke name="create_xlsx">'
    '<｜DSML｜parameter name="name" string="true">'
    "bean-sprout-daily-record-sheet.xlsx"
    "</｜DSML｜parameter>"
    '<｜DSML｜parameter name="sheets" string="false">'
    '[{"name":"Record Sheet","rows":[["Day","Height"]]}]'
    "</｜DSML｜parameter></｜DSML｜invoke></｜DSML｜tool_calls>"
)

BARE_INVOKE_TEXT = (
    "Writing it out.\n\n"
    '<invoke name="write_file">'
    '<parameter name="path">notes.md</parameter>'
    '<parameter name="content">hello</parameter>'
    "</invoke>"
)

MINIMAX_TEXT = (
    "<minimax:tool_call>"
    '<invoke name="write_file">'
    '<parameter name="path">index.html</parameter>'
    '<parameter name="content">hi</parameter>'
    "</invoke>"
)


def _tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=name,
        input_schema=ToolInputSchema(type="object", properties={}),
    )


TOOLS = [_tool("write_file"), _tool("create_xlsx")]


def _end_events(text: str) -> list[ToolUseEndEvent]:
    return [
        event
        for event in _synthesize_text_tool_events(text, TOOLS)
        if isinstance(event, ToolUseEndEvent)
    ]


@pytest.mark.parametrize("text", [TVOE_TEXT, DSML_TEXT, BARE_INVOKE_TEXT, MINIMAX_TEXT])
def test_hidden_protocol_text_is_also_executed(text: str) -> None:
    # The leak suppressor already treats each of these as tool-call protocol.
    assert strip_protocol_text_leak(text) != text

    assert _end_events(text), "protocol was hidden from the user but never executed"


def test_tvoe_wrapper_yields_the_real_arguments() -> None:
    (end,) = _end_events(TVOE_TEXT)

    assert end.tool_name == "write_file"
    assert end.synthetic_from_text is True
    assert end.arguments["path"] == "index.html"
    assert end.arguments["content"] == "<!DOCTYPE html><html><body>app</body></html>"


def test_bare_invoke_without_a_wrapper_yields_the_real_arguments() -> None:
    (end,) = _end_events(BARE_INVOKE_TEXT)

    assert end.tool_name == "write_file"
    assert end.arguments == {"path": "notes.md", "content": "hello"}


def test_dsml_string_attribute_decides_the_argument_type() -> None:
    (end,) = _end_events(DSML_TEXT)

    assert end.tool_name == "create_xlsx"
    # string="true" stays a literal string...
    assert end.arguments["name"] == "bean-sprout-daily-record-sheet.xlsx"
    # ...and string="false" is JSON that must reach the tool as a real list.
    assert end.arguments["sheets"] == [{"name": "Record Sheet", "rows": [["Day", "Height"]]}]


def test_start_and_end_events_are_paired() -> None:
    events = _synthesize_text_tool_events(TVOE_TEXT, TOOLS)

    assert len(events) == 2
    assert events[0].tool_use_id == events[1].tool_use_id


def test_unknown_tool_names_are_not_synthesized() -> None:
    assert _synthesize_text_tool_events(TVOE_TEXT, [_tool("create_xlsx")]) == []


def test_ordinary_prose_is_untouched() -> None:
    assert _synthesize_text_tool_events("Here is a summary of the file.", TOOLS) == []


def test_a_disallowed_invoke_does_not_swallow_a_plain_json_call() -> None:
    # Falling back on "did any XML parse?" rather than "was anything
    # synthesized?" dropped a genuine trailing plain-JSON call whenever the
    # reply also quoted an <invoke> for a tool this turn never offered.
    text = (
        'For example: <invoke name="other_tool">'
        '<parameter name="a">1</parameter></invoke>\n\n'
        'write_file{"path": "a.txt", "content": "hi"}'
    )

    (end,) = _end_events(text)

    assert end.tool_name == "write_file"
    assert end.arguments == {"path": "a.txt", "content": "hi"}


def test_parameter_crlf_newlines_are_stripped_cleanly() -> None:
    text = (
        '<invoke name="write_file">\r\n'
        '<parameter name="path">\r\n'
        "hello.txt\r\n"
        "</parameter>\r\n"
        '<parameter name="content">\r\n'
        "line 1\r\nline 2\r\n"
        "</parameter>\r\n"
        "</invoke>"
    )

    (end,) = _end_events(text)

    assert end.tool_name == "write_file"
    assert end.arguments["path"] == "hello.txt"
    assert end.arguments["content"] == "line 1\r\nline 2"


def test_dsml_crlf_json_parameter_decodes_properly() -> None:
    text = (
        '<｜DSML｜invoke name="create_xlsx">\r\n'
        '<｜DSML｜parameter name="name" string="true">\r\n'
        "report.xlsx\r\n"
        "</｜DSML｜parameter>\r\n"
        '<｜DSML｜parameter name="sheets" string="false">\r\n'
        '[{"name":"Summary","rows":[["A","B"]]}]\r\n'
        "</｜DSML｜parameter>\r\n"
        "</｜DSML｜invoke>"
    )

    (end,) = _end_events(text)

    assert end.tool_name == "create_xlsx"
    assert end.arguments["name"] == "report.xlsx"
    assert end.arguments["sheets"] == [{"name": "Summary", "rows": [["A", "B"]]}]
