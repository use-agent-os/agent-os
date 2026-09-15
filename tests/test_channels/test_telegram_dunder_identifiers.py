"""Issue #2076: Telegram's ``__bold__`` pass ate Python dunder identifiers.

``call __init__ method`` rendered as ``call <b>init</b> method``, and the table
label path was worse: ``_plain_inline`` stripped ``__`` with a bare
``str.replace``, so the reader got ``init`` with not even a tag to hint that
something had been removed. A coding assistant writes these constantly.

``__init__`` is structurally identical to an intentional single-word
``__bold__`` -- a delimiter run with whitespace either side -- so the content
between the underscores is the only thing that can tell them apart. The set is
derived from ``dir()`` over the builtin types rather than typed out, so it
tracks the interpreter instead of going stale.

The same path also never stripped ``_italic_`` from table labels, while it did
strip bold and strike: the sibling of #1931, which only reached
``_render_inline``.
"""

from __future__ import annotations

import pytest

from agentos.channels._telegram_formatting import (
    _DUNDER_NAMES,
    _plain_inline,
    render_telegram_html,
)

COMMON_DUNDERS = [
    "__init__",
    "__main__",
    "__str__",
    "__repr__",
    "__all__",
    "__name__",
    "__file__",
    "__dict__",
    "__len__",
    "__call__",
    "__enter__",
    "__exit__",
    "__eq__",
    "__hash__",
    "__slots__",
    "__doc__",
]

#: Dunders a hand-maintained list tends to miss. These are the ones that make a
#: derived set worth having: each omission reproduces the original bug.
LONG_TAIL_DUNDERS = [
    "__post_init__",
    "__init_subclass__",
    "__set_name__",
    "__aenter__",
    "__aexit__",
    "__anext__",
    "__await__",
    "__match_args__",
    "__getstate__",
    "__setstate__",
    "__subclasshook__",
    "__annotations__",
    "__qualname__",
    "__weakref__",
    "__format__",
    "__sizeof__",
    "__fspath__",
    "__index__",
    "__round__",
    "__reduce__",
]


# ── body text ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("dunder", COMMON_DUNDERS + LONG_TAIL_DUNDERS)
def test_a_dunder_renders_literally(dunder: str) -> None:
    rendered = render_telegram_html(f"call {dunder} method")

    assert rendered == f"call {dunder} method"
    assert "<b>" not in rendered


def test_the_reported_example() -> None:
    assert render_telegram_html("call __init__ method") == "call __init__ method"


def test_two_dunders_in_one_line_both_survive() -> None:
    rendered = render_telegram_html("__init__ calls __post_init__")

    assert rendered == "__init__ calls __post_init__"


def test_a_dunder_at_the_start_and_end_of_a_line() -> None:
    assert render_telegram_html("__main__") == "__main__"
    assert render_telegram_html("guard with __main__") == "guard with __main__"


# ── ordinary emphasis is untouched ──────────────────────────────────────────


def test_single_word_emphasis_still_bolds() -> None:
    """The behaviour the issue explicitly asks to keep."""
    assert render_telegram_html("__also__ emphasised") == "<b>also</b> emphasised"


def test_multi_word_emphasis_still_bolds() -> None:
    assert render_telegram_html("__very important__ note") == "<b>very important</b> note"


def test_star_bold_is_unaffected() -> None:
    assert render_telegram_html("**still bold**") == "<b>still bold</b>"


def test_italics_and_strike_are_unaffected() -> None:
    assert render_telegram_html("~~gone~~") == "<s>gone</s>"
    assert render_telegram_html("*slanted*") == "<i>slanted</i>"


def test_snake_case_still_survives() -> None:
    """Pinned beside the new rule: the single-underscore guards are what keep
    ordinary identifiers intact, and they are easy to disturb from here."""
    assert render_telegram_html("call some_helper_name now") == "call some_helper_name now"


def test_a_dunder_inside_a_code_span_is_still_code() -> None:
    """Code spans are parked before any inline pass, so this was never broken --
    asserted because it is the obvious workaround a user would reach for, and
    it must keep working."""
    assert render_telegram_html("`__init__`") == "<code>__init__</code>"


def test_a_url_containing_double_underscores_is_still_parked() -> None:
    """The href-parking fix and the dunder rule operate on the same delimiters."""
    rendered = render_telegram_html("[link](https://example.test/a__b__c)")

    assert "https://example.test/a__b__c" in rendered


# ── table labels ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("dunder", COMMON_DUNDERS + LONG_TAIL_DUNDERS)
def test_a_table_label_keeps_its_dunder(dunder: str) -> None:
    assert _plain_inline(f"call {dunder} method") == f"call {dunder} method"


def test_a_table_label_still_strips_ordinary_bold() -> None:
    assert _plain_inline("__also__") == "also"
    assert _plain_inline("**bold**") == "bold"
    assert _plain_inline("~~struck~~") == "struck"


def test_a_table_label_now_strips_underscore_italics() -> None:
    """The second half of the report: bold and strike were stripped here and
    `_italic_` was not, so one header kept delimiters its neighbours lost."""
    assert _plain_inline("_Status_") == "Status"


def test_a_table_label_leaves_snake_case_alone() -> None:
    assert _plain_inline("some_helper_name") == "some_helper_name"


def test_a_table_label_url_keeps_its_double_underscores() -> None:
    assert "a__b__c" in _plain_inline("[link](https://example.test/a__b__c)")


def test_the_reported_table_renders_both_columns_intact() -> None:
    markdown = "| _Status_ | __init__ |\n|---|---|\n| _active_ | see __main__ |\n"

    rendered = render_telegram_html(markdown)

    assert "__init__" in rendered
    assert "__main__" in rendered
    assert "_Status_" not in rendered, "underscore italics should be stripped in labels"
    assert "_active_" not in rendered


# ── the derived name set ────────────────────────────────────────────────────


def test_the_name_set_is_derived_from_the_runtime() -> None:
    """A hand-typed list goes stale; this one is built from ``dir()`` so a
    dunder added by a future Python is covered without an edit."""
    assert "init" in _DUNDER_NAMES
    assert "subclasshook" in _DUNDER_NAMES  # from dir(object), never typed out
    assert "match_args" in _DUNDER_NAMES  # from the explicit extras


def test_the_name_set_holds_inner_names_not_delimited_ones() -> None:
    assert "__init__" not in _DUNDER_NAMES


def test_ordinary_words_are_not_in_the_name_set() -> None:
    """If common English crept in, ordinary emphasis would stop bolding."""
    for word in ("also", "very", "important", "note", "status", "active"):
        assert word not in _DUNDER_NAMES
