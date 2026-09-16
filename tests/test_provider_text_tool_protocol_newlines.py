"""Issue #2238: CRLF parameter bodies kept their boundary newlines.

A model writes the opening and closing tags of a ``<parameter>`` on their own
lines, so exactly one newline at each end is markup rather than content.
``_parameter_value`` removed it with ``raw.startswith("\\n")`` and
``raw.endswith("\\n")``. Against CRLF the first test is false -- the first
character is ``\\r`` -- and the second strips the ``\\n`` but orphans the
``\\r``, so the value arrived as ``"\\r\\nline 1\\r\\nline 2\\r"``.

The cost is not cosmetic. The same path carries ``path``, ``command`` and
``code``, so a CRLF-emitting model asking to write ``a.txt`` reached the tool
with ``path="\\r\\na.txt\\r"``: a filename containing control characters.

What must *not* change is the interior. A ``content`` parameter written with
CRLF is meant to arrive with CRLF, so only the two boundary newlines are
removed and every line ending inside the value is left exactly as sent.
"""

from __future__ import annotations

import pytest

from agentos.provider.openai import _synthesize_text_tool_events
from agentos.provider.text_tool_protocol import (
    _strip_boundary_newlines,
    parse_text_tool_calls,
)
from agentos.provider.types import ToolDefinition, ToolInputSchema, ToolUseEndEvent


def _tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=name,
        input_schema=ToolInputSchema(type="object", properties={}),
    )


TOOLS = [_tool("write_file"), _tool("create_xlsx")]


def _argument(body: str, *, attributes: str = "") -> object:
    text = (
        f'<invoke name="write_file">\n<parameter name="p"{attributes}>{body}</parameter>\n</invoke>'
    )
    return parse_text_tool_calls(text)[0].arguments["p"]


def _end_events(text: str) -> list[ToolUseEndEvent]:
    return [
        event
        for event in _synthesize_text_tool_events(text, TOOLS)
        if isinstance(event, ToolUseEndEvent)
    ]


# --------------------------------------------------------------------------
# The boundary rule, in every encoding a model may emit.
# --------------------------------------------------------------------------

BOUNDARY_CASES = {
    # LF -- the only form that already worked.
    "LF both ends": ("\nline 1\nline 2\n", "line 1\nline 2"),
    "LF leading only": ("\nhello", "hello"),
    "LF trailing only": ("hello\n", "hello"),
    # CRLF -- what the issue reports.
    "CRLF both ends": ("\r\nline 1\r\nline 2\r\n", "line 1\r\nline 2"),
    "CRLF leading only": ("\r\nhello", "hello"),
    "CRLF trailing only": ("hello\r\n", "hello"),
    "CRLF single line": ("\r\nhello\r\n", "hello"),
    # Lone CR -- the same markup with classic Mac line endings.
    "CR both ends": ("\rline 1\rline 2\r", "line 1\rline 2"),
    "CR leading only": ("\rhello", "hello"),
    "CR trailing only": ("hello\r", "hello"),
    # Mixed, which is what a model splicing two sources actually produces.
    "CR open, LF close": ("\rhello\n", "hello"),
    "LF open, CR close": ("\nhello\r", "hello"),
    "LF open, CRLF close": ("\nhello\r\n", "hello"),
    "CRLF open, LF close": ("\r\nhello\n", "hello"),
    # No boundary newline at all: a single-line parameter written inline.
    "no boundary newline": ("hello", "hello"),
    # Degenerate bodies.
    "empty": ("", ""),
    "just LF": ("\n", ""),
    "just CRLF": ("\r\n", ""),
    "just CR": ("\r", ""),
}


@pytest.mark.parametrize(("body", "expected"), BOUNDARY_CASES.values(), ids=list(BOUNDARY_CASES))
def test_one_newline_is_removed_at_each_boundary(body: str, expected: str) -> None:
    assert _strip_boundary_newlines(body) == expected


@pytest.mark.parametrize(("body", "expected"), BOUNDARY_CASES.values(), ids=list(BOUNDARY_CASES))
def test_the_parsed_argument_matches_the_boundary_rule(body: str, expected: str) -> None:
    assert _argument(body) == expected


def test_a_leading_lone_cr_is_stripped_like_a_trailing_one() -> None:
    """The asymmetry worth pinning: handling a trailing bare ``\\r`` but not a
    leading one leaves a control character at the front of every value."""
    assert _strip_boundary_newlines("\rhello") == "hello"
    assert _strip_boundary_newlines("hello\r") == "hello"
    assert _strip_boundary_newlines("\rhello\r") == "hello"


# --------------------------------------------------------------------------
# Exactly one newline, and only at the boundary. The interior is the value.
# --------------------------------------------------------------------------

INTERIOR_CASES = {
    "CRLF interior preserved": ("\r\na\r\nb\r\nc\r\n", "a\r\nb\r\nc"),
    "CRLF blank line preserved": ("\r\n\r\nhello\r\n\r\n", "\r\nhello\r\n"),
    "LF blank line preserved": ("\n\nhello\n\n", "\nhello\n"),
    "CR blank line preserved": ("\r\rhello\r\r", "\rhello\r"),
    "mixed interior untouched": ("\r\na\nb\r\nc\r\n", "a\nb\r\nc"),
    "leading spaces preserved": ("\r\n    indented\r\n", "    indented"),
    "trailing spaces preserved": ("\r\nhello   \r\n", "hello   "),
    "interior tabs preserved": ("\r\na\tb\r\n", "a\tb"),
}


@pytest.mark.parametrize(("body", "expected"), INTERIOR_CASES.values(), ids=list(INTERIOR_CASES))
def test_only_the_boundary_newlines_are_removed(body: str, expected: str) -> None:
    assert _strip_boundary_newlines(body) == expected


def test_a_crlf_document_keeps_crlf_inside_the_value() -> None:
    """Normalising interior line endings would be a different bug: a model
    writing a Windows file means the CRLF it sent."""
    value = _argument("\r\nline 1\r\nline 2\r\n")

    assert value == "line 1\r\nline 2"
    assert "\r\n" in str(value)


# --------------------------------------------------------------------------
# What the issue actually reports: the value reaching the tool.
# --------------------------------------------------------------------------


def test_a_crlf_invoke_block_yields_clean_arguments() -> None:
    text = (
        '<invoke name="write_file">\r\n'
        '<parameter name="path">\r\n'
        "a.txt\r\n"
        "</parameter>\r\n"
        '<parameter name="content">\r\n'
        "line 1\r\nline 2\r\n"
        "</parameter>\r\n"
        "</invoke>"
    )

    (end,) = _end_events(text)

    assert end.tool_name == "write_file"
    assert end.arguments == {"path": "a.txt", "content": "line 1\r\nline 2"}


def test_a_cr_only_invoke_block_yields_clean_arguments() -> None:
    text = '<invoke name="write_file">\r<parameter name="path">\ra.txt\r</parameter>\r</invoke>'

    (end,) = _end_events(text)

    assert end.arguments == {"path": "a.txt"}


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_a_path_argument_never_carries_a_control_character(newline: str) -> None:
    """The concrete failure: ``write_file`` asked to create a file whose name
    begins with a carriage return."""
    text = (
        f'<invoke name="write_file">{newline}'
        f'<parameter name="path">{newline}'
        f"a.txt{newline}"
        f"</parameter>{newline}"
        "</invoke>"
    )

    (end,) = _end_events(text)

    path = end.arguments["path"]
    assert path == "a.txt"
    assert "\r" not in path
    assert "\n" not in path


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_the_line_ending_does_not_change_a_single_line_value(newline: str) -> None:
    """The same document in three encodings must produce the same arguments."""
    text = (
        f'<invoke name="write_file">{newline}'
        f'<parameter name="path">{newline}notes.md{newline}</parameter>{newline}'
        f'<parameter name="content">{newline}hello{newline}</parameter>{newline}'
        "</invoke>"
    )

    (end,) = _end_events(text)

    assert end.arguments == {"path": "notes.md", "content": "hello"}


# --------------------------------------------------------------------------
# The string="false" JSON path runs through the same stripping.
# --------------------------------------------------------------------------

JSON_CASES = {
    "LF object": ('\n{"a": 1}\n', {"a": 1}),
    "CRLF object": ('\r\n{"a": 1}\r\n', {"a": 1}),
    "CR object": ('\r{"a": 1}\r', {"a": 1}),
    "CRLF list": ("\r\n[1, 2]\r\n", [1, 2]),
    "CRLF string": ('\r\n"hi"\r\n', "hi"),
    "CRLF number": ("\r\n42\r\n", 42),
    "CRLF null": ("\r\nnull\r\n", None),
}


@pytest.mark.parametrize(("body", "expected"), JSON_CASES.values(), ids=list(JSON_CASES))
def test_a_json_parameter_decodes_under_any_line_ending(body: str, expected: object) -> None:
    assert _argument(body, attributes=' string="false"') == expected


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_a_mislabelled_json_parameter_keeps_a_clean_literal(newline: str) -> None:
    """``json.loads`` tolerates stray ``\\r`` as whitespace, so valid JSON
    decoded even before the fix. The fallback did not: a body that is not JSON
    was returned raw, carrying the boundary characters into the tool."""
    value = _argument(f"{newline}not json at all{newline}", attributes=' string="false"')

    assert value == "not json at all"


def test_a_dsml_crlf_block_decodes_both_parameter_kinds() -> None:
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


# --------------------------------------------------------------------------
# Invariants.
# --------------------------------------------------------------------------

_PAYLOADS = [
    "hello",
    "a\r\nb",
    "a\nb",
    "a\rb",
    "  indented",
    "trailing  ",
    "\ta\tb\t",
    "a\r\n\r\nb",
    '{"a": 1}',
    "",
    "line with <angle> brackets",
]


@pytest.mark.parametrize("payload", _PAYLOADS)
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_a_payload_round_trips_through_a_parameter_block(payload: str, newline: str) -> None:
    """Whatever the model meant to send comes back byte for byte, whichever
    line ending it wrapped the tags in."""
    assert _argument(f"{newline}{payload}{newline}") == payload


@pytest.mark.parametrize("payload", _PAYLOADS)
def test_at_most_one_newline_is_removed_from_each_end(payload: str) -> None:
    """Stripping twice must not eat a blank line the model meant to keep."""
    body = f"\r\n\r\n{payload}\r\n\r\n"

    assert _strip_boundary_newlines(body) == f"\r\n{payload}\r\n"


@pytest.mark.parametrize("payload", _PAYLOADS)
def test_stripping_never_lengthens_or_reorders_the_value(payload: str) -> None:
    for newline in ("\n", "\r\n", "\r"):
        body = f"{newline}{payload}{newline}"
        stripped = _strip_boundary_newlines(body)
        assert stripped in body
        assert len(body) - len(stripped) == 2 * len(newline)


def test_a_value_with_no_boundary_newlines_is_returned_unchanged() -> None:
    for payload in _PAYLOADS:
        if payload[:1] in ("\n", "\r") or payload[-1:] in ("\n", "\r"):
            continue
        assert _strip_boundary_newlines(payload) == payload
