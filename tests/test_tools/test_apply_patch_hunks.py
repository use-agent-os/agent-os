"""Hunk-splice behaviour of ``apply_patch``, including prepend hunks.

``_parse_hunk_header`` accepts ``old_start == 0`` — it even has a dedicated
default for ``old_count`` in that case — so ``@@@ -0,0 +1,N @@@`` is a
supported way to prepend to a file. ``_apply_hunk`` converted that to
``pos = -1`` and spliced ``result[:-1] + new + result[-1:]``, quietly writing
the new lines *before the last line* instead of the first. These tests assert
on the final file content, which is what protects the reverse-sort ordering
in ``_apply_update`` too.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from agentos.tools.builtin import patch as patch_tool
from agentos.tools.types import ToolContext, current_tool_context


def _original_async(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    return fn.__wrapped__.__wrapped__  # type: ignore[attr-defined, no-any-return]


async def _apply(workspace: Path, patch_text: str) -> str:
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    try:
        return await _original_async(patch_tool.apply_patch)(patch_text)
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_prepend_hunk_inserts_at_the_top_of_the_file(tmp_path: Path) -> None:
    target = tmp_path / "test.txt"
    target.write_text("line1\nline2\nline3\n", encoding="utf-8")

    result = await _apply(
        tmp_path,
        """*** Begin Patch
*** Update File: test.txt
@@@ -0,0 +1,1 @@@
+header
*** End Patch""",
    )

    assert "1 file(s) modified" in result
    assert target.read_text(encoding="utf-8") == "header\nline1\nline2\nline3\n"


@pytest.mark.asyncio
async def test_prepend_hunk_with_multiple_lines(tmp_path: Path) -> None:
    target = tmp_path / "test.txt"
    target.write_text("body\n", encoding="utf-8")

    await _apply(
        tmp_path,
        """*** Begin Patch
*** Update File: test.txt
@@@ -0,0 +1,2 @@@
+first
+second
*** End Patch""",
    )

    assert target.read_text(encoding="utf-8") == "first\nsecond\nbody\n"


@pytest.mark.asyncio
async def test_prepend_hunk_into_single_line_file(tmp_path: Path) -> None:
    """The one-line file is where the ``-1`` splice looked most like success."""
    target = tmp_path / "test.txt"
    target.write_text("only\n", encoding="utf-8")

    await _apply(
        tmp_path,
        """*** Begin Patch
*** Update File: test.txt
@@@ -0,0 +1,1 @@@
+header
*** End Patch""",
    )

    assert target.read_text(encoding="utf-8") == "header\nonly\n"


@pytest.mark.asyncio
async def test_prepend_hunk_alongside_a_later_hunk_in_the_same_file(tmp_path: Path) -> None:
    """Hunks apply in reverse ``old_start`` order — the prepend must land last."""
    target = tmp_path / "test.txt"
    target.write_text("line1\nline2\nline3\n", encoding="utf-8")

    await _apply(
        tmp_path,
        """*** Begin Patch
*** Update File: test.txt
@@@ -0,0 +1,1 @@@
+header
@@@ -3,1 +4,1 @@@
-line3
+LINE3
*** End Patch""",
    )

    assert target.read_text(encoding="utf-8") == "header\nline1\nline2\nLINE3\n"


@pytest.mark.asyncio
async def test_prepend_hunk_on_an_empty_file(tmp_path: Path) -> None:
    target = tmp_path / "test.txt"
    target.write_text("", encoding="utf-8")

    await _apply(
        tmp_path,
        """*** Begin Patch
*** Update File: test.txt
@@@ -0,0 +1,1 @@@
+header
*** End Patch""",
    )

    assert target.read_text(encoding="utf-8") == "header\n"


@pytest.mark.asyncio
async def test_ordinary_hunk_positions_are_unchanged(tmp_path: Path) -> None:
    """The clamp must not shift any hunk that already had a 1-indexed start."""
    target = tmp_path / "test.txt"
    target.write_text("line1\nline2\nline3\n", encoding="utf-8")

    await _apply(
        tmp_path,
        """*** Begin Patch
*** Update File: test.txt
@@@ -2,1 +2,1 @@@
-line2
+LINE2
*** End Patch""",
    )

    assert target.read_text(encoding="utf-8") == "line1\nLINE2\nline3\n"


@pytest.mark.asyncio
async def test_insert_after_first_line_still_uses_one_indexed_start(tmp_path: Path) -> None:
    target = tmp_path / "test.txt"
    target.write_text("line1\nline2\n", encoding="utf-8")

    await _apply(
        tmp_path,
        """*** Begin Patch
*** Update File: test.txt
@@@ -1,1 +1,2 @@@
 line1
+inserted
*** End Patch""",
    )

    assert target.read_text(encoding="utf-8") == "line1\ninserted\nline2\n"


def test_apply_hunk_clamps_a_zero_start_to_position_zero() -> None:
    hunk = patch_tool.Hunk(old_start=0, old_count=0, new_start=1, new_count=1)
    hunk.lines = ["+header"]

    assert patch_tool._apply_hunk(["line1\n", "line2\n"], hunk) == [
        "header\n",
        "line1\n",
        "line2\n",
    ]


def test_apply_hunk_zero_start_with_explicit_old_count_replaces_the_first_line() -> None:
    """``-0,1`` names one existing line from position 0 — the first one."""
    hunk = patch_tool.Hunk(old_start=0, old_count=1, new_start=1, new_count=1)
    hunk.lines = ["-line1", "+LINE1"]

    assert patch_tool._apply_hunk(["line1\n", "line2\n"], hunk) == ["LINE1\n", "line2\n"]


# ---------------------------------------------------------------------------
# Blank context lines (#1577)
#
# In unified diff format an empty context line is legitimately written as a
# bare ``""`` rather than ``" "`` -- editors, terminals, CI log pipelines and
# most model output strip the trailing space. ``_apply_hunk`` used to skip
# such lines outright, so every later line was checked against the wrong file
# line (a spurious "Context mismatch") and, had verification passed, the
# blank would have been dropped from the output.
# ---------------------------------------------------------------------------


def _hunk(old_start: int, old_count: int, new_count: int, lines: list[str]) -> patch_tool.Hunk:
    hunk = patch_tool.Hunk(
        old_start=old_start, old_count=old_count, new_start=old_start, new_count=new_count
    )
    hunk.lines = lines
    return hunk


def test_bare_empty_hunk_line_is_a_blank_context_line() -> None:
    hunk = _hunk(1, 3, 3, [" foo", "", "-bar", "+baz"])

    assert patch_tool._apply_hunk(["foo\n", "\n", "bar\n"], hunk) == ["foo\n", "\n", "baz\n"]


def test_space_prefixed_blank_context_line_still_works() -> None:
    hunk = _hunk(1, 3, 3, [" foo", " ", "-bar", "+baz"])

    assert patch_tool._apply_hunk(["foo\n", "\n", "bar\n"], hunk) == ["foo\n", "\n", "baz\n"]


def test_consecutive_blank_context_lines_are_all_kept() -> None:
    hunk = _hunk(1, 4, 4, [" foo", "", "", "-bar", "+baz"])

    assert patch_tool._apply_hunk(["foo\n", "\n", "\n", "bar\n"], hunk) == [
        "foo\n",
        "\n",
        "\n",
        "baz\n",
    ]


def test_blank_context_line_as_the_first_hunk_line() -> None:
    hunk = _hunk(1, 2, 2, ["", "-bar", "+baz"])

    assert patch_tool._apply_hunk(["\n", "bar\n"], hunk) == ["\n", "baz\n"]


def test_blank_context_line_as_the_last_hunk_line() -> None:
    hunk = _hunk(1, 2, 2, ["-bar", "+baz", ""])

    assert patch_tool._apply_hunk(["bar\n", "\n", "tail\n"], hunk) == ["baz\n", "\n", "tail\n"]


def test_blank_context_line_must_match_a_blank_file_line() -> None:
    hunk = _hunk(1, 3, 3, [" foo", "", "-bar", "+baz"])

    with pytest.raises(ValueError, match=r"line 2: expected '', got 'not blank'"):
        patch_tool._apply_hunk(["foo\n", "not blank\n", "bar\n"], hunk)


def test_added_and_deleted_blank_lines_keep_their_prefix_semantics() -> None:
    hunk = _hunk(1, 3, 3, [" foo", "-", "+", "+added", "-bar"])

    assert patch_tool._apply_hunk(["foo\n", "\n", "bar\n"], hunk) == ["foo\n", "\n", "added\n"]


def test_blank_context_line_reuses_the_original_file_line() -> None:
    """A blank context line is copied through, not re-synthesised."""
    hunk = _hunk(1, 2, 3, [" foo", "", "+"])
    original_blank = "\n"

    out = patch_tool._apply_hunk(["foo\n", original_blank], hunk)

    assert out == ["foo\n", "\n", "\n"]
    assert out[1] is original_blank


@pytest.mark.asyncio
async def test_issue_repro_blank_context_line_without_leading_space(tmp_path: Path) -> None:
    target = tmp_path / "test.txt"
    target.write_text("foo\n\nbar\n", encoding="utf-8")

    result = await _apply(
        tmp_path,
        "*** Begin Patch\n"
        "*** Update File: test.txt\n"
        "@@@ -1,3 +1,3 @@@\n"
        " foo\n"
        "\n"
        "-bar\n"
        "+baz\n"
        "*** End Patch",
    )

    assert "1 file(s) modified" in result
    assert target.read_text(encoding="utf-8") == "foo\n\nbaz\n"


@pytest.mark.asyncio
async def test_blank_line_before_end_marker_is_not_hunk_context(tmp_path: Path) -> None:
    """A blank separator after the hunk body must not be read as context."""
    target = tmp_path / "test.txt"
    target.write_text("foo\nbar\n", encoding="utf-8")

    await _apply(
        tmp_path,
        "*** Begin Patch\n"
        "*** Update File: test.txt\n"
        "@@@ -1,2 +1,2 @@@\n"
        " foo\n"
        "-bar\n"
        "+baz\n"
        "\n"
        "*** End Patch",
    )

    assert target.read_text(encoding="utf-8") == "foo\nbaz\n"


@pytest.mark.asyncio
async def test_blank_line_between_hunks_and_files_is_not_hunk_context(tmp_path: Path) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    second.write_text("x\n", encoding="utf-8")

    await _apply(
        tmp_path,
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@@ -1,1 +1,1 @@@\n"
        "-one\n"
        "+ONE\n"
        "\n"
        "@@@ -4,1 +4,1 @@@\n"
        "-four\n"
        "+FOUR\n"
        "\n"
        "*** Update File: b.txt\n"
        "@@@ -1,1 +1,1 @@@\n"
        "-x\n"
        "+y\n"
        "*** End Patch",
    )

    assert first.read_text(encoding="utf-8") == "ONE\ntwo\nthree\nFOUR\n"
    assert second.read_text(encoding="utf-8") == "y\n"


@pytest.mark.asyncio
async def test_trailing_blank_context_counted_by_the_header_is_kept(tmp_path: Path) -> None:
    """A trailing ``""`` the header accounts for is real context, not a separator."""
    target = tmp_path / "test.txt"
    target.write_text("bar\n\ntail\n", encoding="utf-8")

    await _apply(
        tmp_path,
        "*** Begin Patch\n"
        "*** Update File: test.txt\n"
        "@@@ -1,2 +1,2 @@@\n"
        "-bar\n"
        "+baz\n"
        "\n"
        "*** End Patch",
    )

    assert target.read_text(encoding="utf-8") == "baz\n\ntail\n"


def _parsed_hunk_lines(body: str) -> list[str]:
    ops = patch_tool._parse_patch(f"*** Begin Patch\n*** Update File: t.txt\n{body}\n*** End Patch")
    (op,) = ops
    assert isinstance(op, patch_tool.UpdateFile)
    (hunk,) = op.hunks
    return hunk.lines


def test_separator_after_an_omitted_count_header_is_trimmed() -> None:
    """``-3 +3`` means one old line; the trailing blank is not a second one."""
    assert _parsed_hunk_lines("@@@ -3 +3 @@@\n-old\n+new\n") == ["-old", "+new"]


def test_separator_after_a_prepend_hunk_is_trimmed() -> None:
    assert _parsed_hunk_lines("@@@ -0,0 +1,2 @@@\n+one\n+two\n") == ["+one", "+two"]


def test_counted_trailing_blank_is_kept_and_only_the_separator_is_trimmed() -> None:
    assert _parsed_hunk_lines("@@@ -1,2 +1,2 @@@\n-bar\n+baz\n\n\n") == ["-bar", "+baz", ""]


def test_addition_hunk_with_start_exceeding_file_length_raises_error() -> None:
    hunk = patch_tool.Hunk(old_start=50, old_count=0, new_start=50, new_count=1)
    hunk.lines = ["+line at 50"]

    with pytest.raises(ValueError, match=r"Hunk start line 50 exceeds file length \(3 lines\)"):
        patch_tool._apply_hunk(["line1\n", "line2\n", "line3\n"], hunk)


def test_addition_hunk_with_start_exceeding_empty_file_raises_error() -> None:
    hunk = patch_tool.Hunk(old_start=2, old_count=0, new_start=2, new_count=1)
    hunk.lines = ["+line at 2"]

    with pytest.raises(ValueError, match=r"Hunk start line 2 exceeds file length \(0 lines\)"):
        patch_tool._apply_hunk([], hunk)


def test_addition_hunk_at_exact_eof_is_allowed() -> None:
    hunk = patch_tool.Hunk(old_start=4, old_count=0, new_start=4, new_count=1)
    hunk.lines = ["+line4"]

    assert patch_tool._apply_hunk(["line1\n", "line2\n", "line3\n"], hunk) == [
        "line1\n",
        "line2\n",
        "line3\n",
        "line4\n",
    ]


def test_addition_hunk_at_line_zero_and_one_on_empty_file_is_allowed() -> None:
    hunk0 = patch_tool.Hunk(old_start=0, old_count=0, new_start=1, new_count=1, lines=["+first"])
    assert patch_tool._apply_hunk([], hunk0) == ["first\n"]

    hunk1 = patch_tool.Hunk(old_start=1, old_count=0, new_start=1, new_count=1, lines=["+first"])
    assert patch_tool._apply_hunk([], hunk1) == ["first\n"]
