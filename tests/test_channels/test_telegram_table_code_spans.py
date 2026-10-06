"""Issue #3586: _plain_inline stripped emphasis out of a code span.

``_plain_inline`` produces the plain text of a table label. It parked links
and backslash escapes, then removed every backtick and ran the marker passes
-- so by the time those ran there was nothing left to say the text was code,
and a matched pair of markers *inside* the span was stripped out of it. A
cell reading ``2**8`` reached the reader as ``28``, in a row whose value
column says 256.

``_render_inline`` has never had this: ``_replace_code_spans`` runs first and
parks every span before any marker pass sees it, and its own comment states
the rule -- "code spans were parked first, so they come back last and a
restored code span is never rescanned". This is that rule applied to the
sibling function, with the span rendered as its literal content rather than
as ``<code>``.

Previous ``_plain_inline`` defects, for contrast, are both about markers in
prose: #1931 (a dunder eaten) and #2964 (asterisk italics not stripped).
"""

from __future__ import annotations

import pytest

from agentos.channels._telegram_formatting import _plain_inline, render_telegram_html

# ── the issue's reproduction ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "content",
    ["2**8", "a*b*c", "~~x~~", "__all__", "a**b**c", "_x_", "*a* and **b**", "x~~y~~z"],
)
def test_a_code_span_keeps_its_content_verbatim(content: str) -> None:
    assert _plain_inline(f"`{content}`") == content


def test_the_table_cell_from_the_issue() -> None:
    table = "| Setting | Value |\n| - | - |\n| `2**8` | 256 |"

    assert "<b>2**8:</b> 256" in render_telegram_html(table)


def test_a_code_span_beside_real_emphasis_in_the_same_label() -> None:
    """The markers outside the span are still stripped; the ones inside are not."""
    assert _plain_inline("**bold** and `2**8`") == "bold and 2**8"


def test_a_multi_backtick_span_is_protected_too() -> None:
    assert _plain_inline("``a `b` 2**8``") == "a `b` 2**8"


# ── what must not change ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("**Bold**", "Bold"),
        ("*Metric*", "Metric"),
        ("_Metric_", "Metric"),
        ("~~Gone~~", "Gone"),
        ("__init__", "__init__"),
        ("__bold__", "bold"),
        ("snake_case_name", "snake_case_name"),
        ("plain", "plain"),
        ("", ""),
    ],
)
def test_prose_labels_are_stripped_exactly_as_before(label: str, expected: str) -> None:
    assert _plain_inline(label) == expected


def test_a_link_still_becomes_text_and_destination() -> None:
    assert _plain_inline("[docs](https://x.test/a_b__c)") == "docs (https://x.test/a_b__c)"


def test_a_link_inside_a_code_span_is_not_unwrapped() -> None:
    """Code spans are parked before links, so a link written inside one is
    content, not a link."""
    assert _plain_inline("`[docs](https://x.test)`") == "[docs](https://x.test)"


def test_an_escaped_backtick_does_not_open_a_span() -> None:
    assert _plain_inline(r"\`not code\` **bold**") == "`not code` bold"


def test_an_unclosed_backtick_still_opens_nothing() -> None:
    """No span, so the markers after it are stripped as prose and the stray
    delimiter is dropped — unchanged from before."""
    assert _plain_inline("`unclosed **bold**") == "unclosed bold"


def test_the_html_path_is_unchanged() -> None:
    """``_render_inline`` still renders a span as ``<code>``; only the
    plain-text caller asked for something else."""
    assert render_telegram_html("`2**8`") == "<code>2**8</code>"
    assert render_telegram_html("`<b>&`") == "<code>&lt;b&gt;&amp;</code>"


def test_a_table_with_ordinary_labels_renders_as_before() -> None:
    table = "| Metric | Value |\n| - | - |\n| **Revenue** | 1.2M |"

    assert render_telegram_html(table) == (
        "<b>Metric — Value</b>\n<b>Revenue:</b> 1.2M"
    )
