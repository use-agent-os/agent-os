from __future__ import annotations

import pytest

from agentos.tools.fuzzy_match import (
    STRATEGIES,
    AmbiguousMatchError,
    EscapeDriftError,
    FuzzyMatchError,
    find_closest_lines,
    fuzzy_find_and_replace,
)


def test_exact_match_wins_before_any_fallback() -> None:
    result = fuzzy_find_and_replace("a\nfoo\nb\n", "foo", "bar")

    assert result.strategy == "exact"
    assert result.updated == "a\nbar\nb\n"
    assert result.match_count == 1


def test_escape_normalized_matches_unexpanded_newline_literal() -> None:
    result = fuzzy_find_and_replace("a = 1\nb = 2\n", "a = 1\\nb = 2", "c = 3")

    assert result.strategy == "escape_normalized"
    assert result.updated == "c = 3\n"


def test_escape_normalized_refuses_when_new_text_keeps_the_same_literals() -> None:
    with pytest.raises(EscapeDriftError) as excinfo:
        fuzzy_find_and_replace("a = 1\nb = 2\n", "a = 1\\nb = 2", "a = 1\\nb = 3")

    assert excinfo.value.literals == ("\\n",)


def test_exact_match_on_real_escape_literals_is_not_refused() -> None:
    result = fuzzy_find_and_replace('msg = "a\\nb"\n', 'msg = "a\\nb"', 'msg = "a\\nc"')

    assert result.strategy == "exact"
    assert result.updated == 'msg = "a\\nc"\n'


def test_unicode_normalized_matches_smart_quotes() -> None:
    result = fuzzy_find_and_replace('x = \u201chello\u201d\n', 'x = "hello"', 'x = "bye"')

    assert result.strategy == "unicode_normalized"
    assert result.updated == 'x = "bye"\n'


def test_unicode_outside_the_replaced_span_is_preserved() -> None:
    content = "note = \u201ckeep\u201d\nx = \u201chit\u201d\n"

    result = fuzzy_find_and_replace(content, 'x = "hit"', 'x = "done"')

    # The normalization exists to find the text, never to rewrite the file.
    assert result.updated == "note = \u201ckeep\u201d\nx = \"done\"\n"


def test_trimmed_boundary_matches_across_an_indent_change() -> None:
    result = fuzzy_find_and_replace("if x:\n\treturn 1\n", "    return 1", "    return 9")

    assert result.strategy == "trimmed_boundary"
    # The file's own tab survives; the model's four spaces do not leak in.
    assert result.updated == "if x:\n\treturn 9\n"


def test_indent_agnostic_reindents_replacement_to_the_matched_region() -> None:
    content = "class A:\n    def run(self):\n        value = 1\n        return value\n"

    result = fuzzy_find_and_replace(
        content,
        "    value = 1\n    return value\n",
        "    value = 2\n    return value * 2\n",
    )

    assert result.strategy == "indent_agnostic"
    assert result.updated == (
        "class A:\n    def run(self):\n        value = 2\n        return value * 2\n"
    )


def test_reindent_preserves_relative_nesting_inside_the_replacement() -> None:
    content = "class A:\n    def m(self):\n        if x:\n            return 1\n"

    result = fuzzy_find_and_replace(
        content,
        "if x:\n    return 1\n",
        "if x:\n    log()\n    return 2\n",
    )

    assert result.updated == (
        "class A:\n    def m(self):\n        if x:\n            log()\n            return 2\n"
    )


def test_line_trimmed_matches_trailing_whitespace_drift() -> None:
    result = fuzzy_find_and_replace("alpha   \nbeta\n", "alpha\nbeta\n", "gamma\n")

    assert result.strategy == "line_trimmed"
    assert result.updated == "gamma\n"


def test_whitespace_collapsed_matches_repeated_inner_spaces() -> None:
    result = fuzzy_find_and_replace(
        "def f():\n        return  1\n",
        "    return   1",
        "    return 2",
    )

    assert result.strategy == "whitespace_collapsed"
    assert result.updated == "def f():\n        return 2\n"


def test_block_anchor_matches_when_only_the_middle_drifted() -> None:
    content = "def run():\n    a = 1\n    b = 2\n    return a + b\n"

    result = fuzzy_find_and_replace(
        content,
        "def run():\n    a = 111\n    b = 222\n    return a + b\n",
        "def run():\n    return 0\n",
    )

    assert result.strategy == "block_anchor"
    assert result.updated == "def run():\n    return 0\n"


def test_context_similarity_matches_a_single_near_miss() -> None:
    content = "def calculate_total(items):\n    return 0\n"

    result = fuzzy_find_and_replace(
        content,
        "def calculate_totals(item):\n    return 0\n",
        "def total(items):\n    return 1\n",
    )

    assert result.strategy == "context_similarity"
    assert result.updated == "def total(items):\n    return 1\n"


def test_context_similarity_refuses_when_two_blocks_are_comparable() -> None:
    content = "def f():\n    x = 1\n    return x\n\ndef g():\n    x = 1\n    return x\n"

    # Two candidates score alike; choosing either one would be a guess.
    with pytest.raises(FuzzyMatchError):
        fuzzy_find_and_replace(content, "def h():\n    x = 1\n    return x\n", "pass\n")


def test_ambiguous_match_reports_every_line() -> None:
    with pytest.raises(AmbiguousMatchError) as excinfo:
        fuzzy_find_and_replace("foo\nfoo\n", "foo", "bar")

    assert excinfo.value.match_count == 2
    assert excinfo.value.lines == (1, 2)
    assert excinfo.value.strategy == "exact"


def test_replace_all_accepts_multiple_matches() -> None:
    result = fuzzy_find_and_replace("foo\nfoo\n", "foo", "bar", replace_all=True)

    assert result.match_count == 2
    assert result.updated == "bar\nbar\n"


def test_missing_text_raises_with_a_closest_line_hint() -> None:
    content = "def calculate_total(items):\n    return 0\n"

    with pytest.raises(FuzzyMatchError) as excinfo:
        fuzzy_find_and_replace(content, "def calculate_grand_totals(a, b, c, d):\n", "x")

    assert "line 1" in excinfo.value.hint


def test_unrelated_text_produces_no_misleading_hint() -> None:
    with pytest.raises(FuzzyMatchError) as excinfo:
        fuzzy_find_and_replace("alpha\nbeta\n", "zzz_nothing_like_this", "x")

    assert excinfo.value.hint == ""


def test_whitespace_only_pattern_is_refused_by_line_strategies() -> None:
    # Every line normalizes to nothing, so a line strategy would match anywhere.
    with pytest.raises(FuzzyMatchError):
        fuzzy_find_and_replace("a\n    \nb\n", "\t\n\t\n", "x")


def test_empty_old_text_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        fuzzy_find_and_replace("anything", "", "x")


def test_applying_the_result_again_finds_nothing() -> None:
    first = fuzzy_find_and_replace("a\nfoo\n", "foo", "bar")

    with pytest.raises(FuzzyMatchError):
        fuzzy_find_and_replace(first.updated, "foo", "bar")


def test_restricting_the_chain_disables_the_fallbacks() -> None:
    content = "class A:\n\tvalue = 1\n"

    # The tab-vs-spaces drift is exactly what trimmed_boundary would absorb.
    assert fuzzy_find_and_replace(content, "    value = 1", "    value = 2").strategy != "exact"
    with pytest.raises(FuzzyMatchError):
        fuzzy_find_and_replace(content, "    value = 1", "    value = 2", strategies=("exact",))


def test_every_declared_strategy_is_runnable() -> None:
    # Guards against a name in STRATEGIES with no implementation behind it,
    # which would silently skip that rung of the chain.
    for strategy in STRATEGIES:
        with pytest.raises(FuzzyMatchError):
            fuzzy_find_and_replace("a\nb\n", "no_such_text_anywhere", "x", strategies=(strategy,))


def test_find_closest_lines_reports_line_numbers_and_scores() -> None:
    content = "alpha\nbeta gamma delta\nomega\n"

    hint = find_closest_lines(content, "beta gamma delta epsilon")

    assert "line 2" in hint
    assert "%" in hint


def test_trimmed_boundary_keeps_the_block_in_its_body_on_trailing_whitespace_drift() -> None:
    """The span used to start after the line's indent and stop before its
    newline, so the shared re-indent read a target indent of "" and dedented
    every line after the first, then new_text's own newline was a net
    insertion. The result was a `return` dedented out of its `def` (#2050)."""
    content = "class A:\n    def foo(self):\n        return 1\n"

    result = fuzzy_find_and_replace(
        content,
        "    def foo(self):\n        return 1   \n",
        "    def foo(self):\n        return 2\n",
    )

    assert result.strategy == "trimmed_boundary"
    assert result.updated == "class A:\n    def foo(self):\n        return 2\n"


def test_trimmed_boundary_span_covers_the_whole_lines_it_replaces() -> None:
    content = "class A:\n    def foo(self):\n        return 1\n"

    result = fuzzy_find_and_replace(
        content, "    def foo(self):\n        return 1 \n", "    pass\n"
    )

    assert result.strategy == "trimmed_boundary"
    assert result.spans == ((9, len(content)),)
    assert result.updated == "class A:\n    pass\n"


def test_trimmed_boundary_absorbs_trailing_whitespace_the_file_has() -> None:
    # The file line carries trailing spaces the model did not reproduce.
    content = "if x:\n    return 1  \nprint()\n"

    result = fuzzy_find_and_replace(content, "    return 1\n", "    return 9\n")

    assert result.strategy == "trimmed_boundary"
    assert result.updated == "if x:\n    return 9\nprint()\n"


def test_trimmed_boundary_leading_newline_does_not_consume_the_line_above() -> None:
    content = "x = 1\n    a = 2\n    b = 3\n"

    # The model's old_text opens on a blank-ish line the file does not have.
    result = fuzzy_find_and_replace(content, "  \n    a = 2\n    b = 3\n", "    c = 4\n")

    assert result.strategy == "trimmed_boundary"
    assert result.updated == "x = 1\n    c = 4\n"


def test_trimmed_boundary_mid_line_match_stays_mid_line() -> None:
    content = "x = foo(1)\n"

    result = fuzzy_find_and_replace(content, " 1 ", "2")

    assert result.strategy == "trimmed_boundary"
    assert result.updated == "x = foo(2)\n"


def test_trimmed_boundary_without_leading_drift_keeps_new_text_indent_as_written() -> None:
    # Only the trailing scrap drifted; the model's new_text already carries
    # the file's indentation on every line, so nothing may be shifted.
    content = "class A:\n    def foo(self):\n        return 1\n"

    result = fuzzy_find_and_replace(
        content,
        "def foo(self):\n        return 1 \n",
        "def foo(self):\n        return 2\n",
    )

    assert result.strategy == "trimmed_boundary"
    assert result.updated == "class A:\n    def foo(self):\n        return 2\n"


def test_trimmed_boundary_replace_all_on_adjacent_lines_does_not_merge_matches() -> None:
    content = "    a\n    a\n"

    # new_text is written against old_text's two-space belief; each hit lands
    # at the file's four and the span growth must not fuse the two lines.
    result = fuzzy_find_and_replace(content, "  a \n", "  b\n", replace_all=True)

    assert result.strategy == "trimmed_boundary"
    assert result.match_count == 2
    assert result.updated == "    b\n    b\n"
