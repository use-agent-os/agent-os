"""``_plain_inline`` strips ``*italic*`` the way it strips ``_italic_`` (#2964).

Table headers and row labels go through ``_plain_inline`` before they are
wrapped in ``<b>``. It stripped ``**bold**``, ``__bold__``, ``~~strike~~``,
`` `code` `` and ``_italic_`` -- but not ``*italic*``, so ``| *Metric* |``
reached the reader as ``<b>*Metric*</b>``. The asterisk pattern is the one
``_render_inline`` already uses; its lookarounds keep it off ``**bold**``.
"""

from __future__ import annotations

import pytest

from agentos.channels._telegram_formatting import _plain_inline, render_telegram_html

# ── the report ─────────────────────────────────────────────────────────────


def test_the_reported_table_renders_clean_labels() -> None:
    markdown = (
        "| *Metric* | *Value* |\n"
        "| --- | --- |\n"
        "| *Latency* | 12ms |\n"
        "| ***Throughput*** | 500 rps |\n"
    )

    assert render_telegram_html(markdown) == (
        "<b>Metric — Value</b>\n<b>Latency:</b> 12ms\n<b>Throughput:</b> 500 rps"
    )


def test_both_italic_spellings_strip_the_same_way() -> None:
    assert _plain_inline("*heading*") == "heading"
    assert _plain_inline("_heading_") == "heading"


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("*em*", "em"),
        ("***both***", "both"),
        ("*alpha* and _beta_", "alpha and beta"),
        ("*a* and **b** and ***c***", "a and b and c"),
        ("`code` and *em*", "code and em"),
        ("*multi word label*", "multi word label"),
        ("*a* *b*", "a b"),
    ],
)
def test_a_table_label_strips_asterisk_italics(label: str, expected: str) -> None:
    assert _plain_inline(label) == expected


# ── what must not be touched ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "label",
    [
        "* bullet",  # an opening asterisk followed by a space is not emphasis
        "a * b * c",  # neither is a spaced-out operator
        "5 * 3",
        "*unterminated",
        "trailing*",
    ],
)
def test_a_lone_or_spaced_asterisk_is_left_alone(label: str) -> None:
    assert _plain_inline(label) == label


def test_a_url_containing_asterisks_is_still_parked() -> None:
    """Same protection the other markers have: the link target is not text."""
    assert _plain_inline("[t](https://x.test/a*b*c)") == "t (https://x.test/a*b*c)"


def test_the_other_markers_still_strip() -> None:
    assert _plain_inline("**bold** and __also__ and ~~gone~~") == "bold and also and gone"
    assert _plain_inline("call __init__ method") == "call __init__ method"
    assert _plain_inline("some_helper_name") == "some_helper_name"


def test_the_strip_agrees_with_the_render_pass() -> None:
    """Whatever ``_render_inline`` would italicise, the label path removes --
    including the case both share: a bare ``*3*`` between digits."""
    assert render_telegram_html("*em*") == "<i>em</i>"
    assert _plain_inline("*em*") == "em"
    assert render_telegram_html("2*3*4") == "2<i>3</i>4"
    assert _plain_inline("2*3*4") == "234"
