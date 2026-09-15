"""Issue #2127: a fence opening a segment left the first chunk half-open.

``split_text_for_limit`` backs a cut up to before an unclosed fence so neither
half carries a half-open block. The backup only ran ``if candidate > 0`` — but
``candidate`` is the offset just after the newline preceding the fence, and
that is legitimately ``0`` when the fence opens the segment. The guard read
zero as "nothing to do" and emitted the unbalanced chunk.

Backing up to ``0`` is not the fix either: an empty head makes every caller
that splits until the tail is empty spin forever. The fence is closed on the
chunk and reopened on the next instead.

Two latent defects in the same guard are covered here too. It counted
``"```"`` occurrences, which reads a six-backtick fence as two (so an unclosed
one looks balanced) and does not see a ``~~~`` fence at all. Both leave exactly
the half-open block the guard exists to prevent.

This is the shared primitive behind Discord's, Telegram's and MS Teams'
message-length caps, so an unbalanced chunk reaches three platforms.
"""

from __future__ import annotations

import re

import pytest

from agentos.channels._util import split_text_for_limit

BT = "`" * 3
LONG_BT = "`" * 6
TILDE = "~" * 3

_FENCE_LINE = re.compile(r"(?m)^ {0,3}(`{3,}|~{3,})")


def fence_lines(text: str) -> list[str]:
    return _FENCE_LINE.findall(text)


def open_fence(chunk: str) -> str | None:
    """The marker of a fence left open at the end of *chunk*, if any.

    Counting markers and checking parity is not enough. CommonMark closes a
    fence only with the same character at the same length or longer, so a
    three-backtick line inside a six-backtick block is content, and a parity
    count would call a perfectly balanced chunk broken.
    """
    marker: str | None = None
    for run in fence_lines(chunk):
        if marker is None:
            marker = run
        elif run[0] == marker[0] and len(run) >= len(marker):
            marker = None
    return marker


def assert_balanced(chunk: str) -> None:
    left = open_fence(chunk)
    assert left is None, f"chunk carries a fence left open by {left!r}: {chunk!r}"


def body_of(text: str) -> str:
    """*text* with every fence marker and all whitespace stripped.

    The fix adds a closing marker to one chunk and an opening one to the next,
    each on its own line, so markers and newlines legitimately differ between
    input and output. Everything else must not: this reduces both sides to the
    characters the user actually wrote.
    """
    return re.sub(r"\s+", "", re.sub(r"`{3,}|~{3,}", "", text))


def split_all(segment: str, limit: int, *, measure=None) -> list[str]:
    """Drive the splitter the way its callers do, and refuse to hang.

    Every caller loops until the tail is empty. A cut that makes no progress is
    an infinite loop in production, so the bound here is part of the assertion
    rather than a convenience.
    """
    chunks: list[str] = []
    remaining = segment
    for _ in range(200):
        head, tail = split_text_for_limit(remaining, limit, measure=measure)
        assert head, "an empty head would loop forever"
        assert len(tail) < len(remaining), "the tail must shrink"
        chunks.append(head)
        if not tail:
            return chunks
        remaining = tail
    raise AssertionError("split_text_for_limit did not terminate")


# ── the reported case ───────────────────────────────────────────────────────


def test_a_fence_opening_the_segment_leaves_a_balanced_chunk() -> None:
    """The reproduction from the issue."""
    segment = BT + "a" * 100 + BT + "\nrest"

    head, _tail = split_text_for_limit(segment, 50)

    assert_balanced(head)


def test_the_chunk_still_respects_the_limit() -> None:
    """Closing the fence adds characters; the chunk must still fit, or the
    platform rejects the message and nothing has been gained."""
    segment = BT + "a" * 100 + BT + "\nrest"

    head, _tail = split_text_for_limit(segment, 50)

    assert len(head) <= 50


def test_the_tail_reopens_the_fence() -> None:
    segment = BT + "a" * 100 + BT + "\nrest"

    _head, tail = split_text_for_limit(segment, 50)

    assert tail.startswith(BT)


def test_no_body_text_is_lost_or_duplicated() -> None:
    """The synthesized markers are the only difference between input and
    output: strip them back off and the body must be exactly what went in."""
    segment = BT + "a" * 100 + BT + "\nrest"

    chunks = split_all(segment, 50)
    rejoined = "".join(chunks)

    assert body_of(rejoined) == body_of(segment)
    assert rejoined.count("a") == segment.count("a")
    assert rejoined.endswith("rest")


def test_the_language_tag_is_carried_onto_the_reopened_fence() -> None:
    """A reopened block without its tag loses syntax highlighting for the rest
    of a long listing."""
    segment = BT + "python\n" + "a" * 100 + "\n" + BT

    head, tail = split_text_for_limit(segment, 60)

    assert_balanced(head)
    assert tail.startswith(BT + "python")


def test_a_bare_fence_does_not_invent_a_language_tag() -> None:
    """A fence whose own line never ends inside the segment has no info string:
    everything after it is body text, and treating that as a tag would both
    reopen the block wrongly and make the reopener arbitrarily long."""
    segment = BT + "a" * 100

    _head, tail = split_text_for_limit(segment, 40)

    assert tail.startswith(BT + "\n")
    assert "aaa" not in tail.split("\n", 1)[0]


# ── the fence grammar ───────────────────────────────────────────────────────


def test_a_six_backtick_fence_is_one_fence_not_two() -> None:
    """Counting ``"```"`` read this as two fences, so an unclosed block looked
    balanced and no backup happened. Six backticks is the spelling used when
    the code sample itself contains a triple."""
    segment = LONG_BT + "\n" + "a" * 100 + "\n" + LONG_BT

    head, _tail = split_text_for_limit(segment, 50)

    assert_balanced(head)


def test_a_tilde_fence_is_recognised() -> None:
    """``~~~`` is the other fence CommonMark defines, and the old guard did not
    see it at all."""
    segment = TILDE + "\n" + "a" * 100 + "\n" + TILDE

    head, _tail = split_text_for_limit(segment, 50)

    assert_balanced(head)


def test_a_shorter_run_does_not_close_a_longer_fence() -> None:
    """CommonMark: a closing fence must be at least as long as its opener, so
    a ``` inside a ``````-fenced block is content, not a close."""
    segment = LONG_BT + "\n" + "a" * 40 + "\n" + BT + "\n" + "b" * 60 + "\n" + LONG_BT

    for chunk in split_all(segment, 50):
        assert_balanced(chunk)


def test_a_tilde_fence_is_not_closed_by_backticks() -> None:
    segment = TILDE + "\n" + "a" * 40 + "\n" + BT + "\n" + "b" * 60 + "\n" + TILDE

    for chunk in split_all(segment, 50):
        assert_balanced(chunk)


def test_a_backtick_fence_info_string_may_not_contain_a_backtick() -> None:
    """CommonMark's rule, and it matters here because the info string is
    copied onto every reopened chunk.

    Without it, a line like ``` ```a`b ``` — an inline code span, not a fence —
    reads as a fence whose language tag is most of the line, and that text is
    then duplicated into the output.
    """
    segment = BT + "a`b\n" + "c" * 200

    chunks = split_all(segment, 40)
    joined = "".join(chunks)

    assert joined.count("a`b") == 1
    assert joined.count("c") == 200


def test_only_the_language_is_carried_not_the_whole_info_string() -> None:
    """CommonMark takes the first word as the language and ignores the rest.
    Copying the rest onto every chunk puts an unbounded string on the front of
    each one."""
    segment = BT + "python title=example.py extra\n" + "a" * 300

    _head, tail = split_text_for_limit(segment, 60)

    assert tail.startswith(BT + "python\n")
    assert "title=" not in tail


def test_a_tilde_fence_info_string_may_contain_backticks() -> None:
    """The backtick restriction is specific to backtick fences."""
    segment = TILDE + "py`x\n" + "a" * 200

    chunks = split_all(segment, 40)

    assert "".join(chunks).count("a") == 200


def test_an_indented_fence_still_counts() -> None:
    """Up to three spaces of indent is still a fence; four would be an indented
    code block instead."""
    segment = "   " + BT + "\n" + "a" * 100 + "\n   " + BT

    head, _tail = split_text_for_limit(segment, 50)

    assert_balanced(head)


def test_backticks_inside_a_line_are_not_a_fence() -> None:
    """An inline ``code`` span is not a fence, and treating one as an opener
    would back the cut up for no reason."""
    segment = "text with `inline` and " + "a" * 100

    head, tail = split_text_for_limit(segment, 50)

    assert len(head) == 50 or head.endswith(" ")
    assert head + tail == segment


def test_a_triple_backtick_mid_line_is_a_code_span_not_a_fence() -> None:
    """``intro ```x``` `` is a code span: a fence has to start its own line.

    Nothing is synthesized here — the ordinary word-boundary cut already ends
    the chunk before the span, so the content round-trips exactly. Treating the
    span as a fence reaches the same head on this input by a different route,
    but only because the boundary happened to agree.
    """
    segment = "intro text " + BT + "a" * 400 + BT + "\nrest"

    head, tail = split_text_for_limit(segment, 100)

    assert head == "intro text "
    assert head + tail == segment


def test_a_long_inline_span_is_not_cut_through() -> None:
    """An unclosed ``code`` span is the inline sibling of a half-open fence and
    the same delivery failure.

    The word-boundary nudge does not save this: it only accepts a boundary in
    the second half of the chunk, and a span that starts early has its last
    boundary before that. The cut retreats to the span's own start.
    """
    segment = "see " + "`" + "a" * 200 + "`" + " done"

    head, tail = split_text_for_limit(segment, 100)

    assert head == "see "
    assert head + tail == segment


def test_backing_up_out_of_a_span_never_empties_the_chunk() -> None:
    """A span opening the segment has nowhere to retreat to. Backing up to 0
    would hang every caller, so the plain cut stands."""
    segment = "`" + "a" * 200 + "`"

    chunks = split_all(segment, 40)

    assert "".join(chunks) == segment


def test_a_closed_span_before_the_cut_is_left_alone() -> None:
    """Balanced spans must not drag the cut backwards."""
    segment = "a `x` b " * 30

    head, tail = split_text_for_limit(segment, 100)

    assert head + tail == segment
    assert len(head) > 50


def test_a_double_backtick_span_is_not_closed_by_a_single() -> None:
    """CommonMark closes a span only with a run of exactly the opening length,
    so the single backtick here is content and the span is still open."""
    segment = "see ``" + "a" * 60 + " ` " + "b" * 140 + "`` done"

    head, tail = split_text_for_limit(segment, 100)

    assert head == "see "
    assert head + tail == segment


# ── behaviour that must not change ──────────────────────────────────────────


def test_a_fence_after_a_newline_still_backs_up() -> None:
    """The original path: there is an earlier line to retreat to, so the chunk
    ends before the fence and no markers are synthesized."""
    segment = "intro\n" + BT + "\n" + "a" * 100 + "\n" + BT

    head, tail = split_text_for_limit(segment, 50)

    assert head == "intro\n"
    assert tail.startswith(BT)


def test_text_shorter_than_the_limit_is_returned_whole() -> None:
    assert split_text_for_limit("short", 50) == ("short", "")


def test_text_with_no_fence_splits_on_a_word_boundary() -> None:
    segment = "word " * 40

    head, tail = split_text_for_limit(segment, 50)

    assert head.endswith(" ")
    assert head + tail == segment


def test_a_balanced_fence_before_the_cut_is_left_alone() -> None:
    segment = BT + "\nx\n" + BT + "\n" + "a" * 100

    head, _tail = split_text_for_limit(segment, 60)

    assert_balanced(head)
    assert head.startswith(BT)


# ── termination, across shapes and limits ───────────────────────────────────


@pytest.mark.parametrize("limit", [8, 12, 20, 50, 97])
@pytest.mark.parametrize(
    "segment",
    [
        BT + "a" * 100,
        BT + "python\n" + "a" * 100,
        LONG_BT + "\n" + "a" * 100,
        TILDE + "\n" + "a" * 100,
        BT + "a" * 100 + BT + "\nrest",
        "intro\n" + BT + "\n" + "a" * 100,
        "a" * 100,
    ],
    ids=["bare", "tagged", "six-tick", "tilde", "closed", "after-newline", "no-fence"],
)
def test_splitting_always_terminates_and_stays_within_the_limit(segment: str, limit: int) -> None:
    """The guard that makes the fix safe: a cut that fails to shrink the tail
    hangs every caller. Driven over small limits, where a synthesized closer is
    most likely to not fit."""
    chunks = split_all(segment, limit)

    assert chunks
    for chunk in chunks:
        assert len(chunk) <= limit or chunk == chunks[-1] or len(chunk) <= limit + len(BT) + 1


def test_a_limit_too_small_for_a_closer_falls_back_rather_than_looping() -> None:
    """When even the closing marker will not fit, an unbalanced chunk is the
    right trade: the message still goes out, where a loop never would."""
    segment = BT + "a" * 40

    chunks = split_all(segment, 4)

    assert chunks
    assert "".join(chunks).count("a") == 40


# ── the measure callable is respected ───────────────────────────────────────


def test_the_synthesized_closer_is_measured_not_assumed() -> None:
    """Telegram passes a ``measure`` that renders markdown first, so the closer
    can cost far more than its four characters. Measuring with ``len`` while
    the caller measures otherwise puts the chunk back over the cap."""
    segment = BT + "a" * 200

    def double(text: str) -> int:
        return len(text) * 2

    head, tail = split_text_for_limit(segment, 60, measure=double)

    assert double(head) <= 60
    assert len(tail) < len(segment)
