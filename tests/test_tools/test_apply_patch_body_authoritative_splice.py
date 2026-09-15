"""Issue #2224: the splice trusted the hunk header's ``old_count``.

``_apply_hunk`` does three passes over a hunk body. The context check and the
rebuild both count old-side lines from the *body*; the splice used the
*header's* ``old_count``. Two sources of truth for one number, and the format
lets a writer omit the count (``@@@ -2 +2 @@@`` defaults to 1) or simply
miscount it -- nothing validates it against the body.

When they disagreed the splice replaced a different span than the body
described, and silently: every body line is context-matched against the file
before the splice runs, so the hunk looks applied and ``apply_patch`` reports
success. An undercount writes the tail back a second time; an overcount deletes
lines the hunk never mentioned.

The body is the only authority available, because each of its old-side lines
was verified against the file. These tests assert on final file content rather
than on internals, so they hold whichever way the splice is computed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from agentos.tools.builtin import patch as patch_tool
from agentos.tools.types import ToolContext, current_tool_context

FIVE_LINES = "l1\nl2\nl3\nl4\nl5\n"


def _original_async(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    return fn.__wrapped__.__wrapped__  # type: ignore[attr-defined, no-any-return]


async def _apply(workspace: Path, patch_text: str) -> str:
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    try:
        return await _original_async(patch_tool.apply_patch)(patch_text)
    finally:
        current_tool_context.reset(token)


def _patch(header: str, body: str) -> str:
    return f"""*** Begin Patch
*** Update File: test.txt
{header}
{body}*** End Patch"""


# ── the three reported shapes ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_omitted_count_does_not_duplicate_the_tail(tmp_path: Path) -> None:
    """``@@@ -2 +2 @@@`` defaults old_count to 1 while the body consumes 3, so
    the splice dropped one line and wrote the other two back after the hunk."""
    target = tmp_path / "test.txt"
    target.write_text(FIVE_LINES, encoding="utf-8")

    await _apply(tmp_path, _patch("@@@ -2 +2 @@@", " l2\n-l3\n+l3x\n l4\n"))

    assert target.read_text(encoding="utf-8") == "l1\nl2\nl3x\nl4\nl5\n"


@pytest.mark.asyncio
async def test_an_undercounted_header_does_not_duplicate_the_tail(tmp_path: Path) -> None:
    target = tmp_path / "test.txt"
    target.write_text(FIVE_LINES, encoding="utf-8")

    await _apply(tmp_path, _patch("@@@ -2,2 +2,2 @@@", " l2\n-l3\n+l3x\n l4\n"))

    assert target.read_text(encoding="utf-8") == "l1\nl2\nl3x\nl4\nl5\n"


@pytest.mark.asyncio
async def test_an_overcounted_header_does_not_delete_unmentioned_lines(
    tmp_path: Path,
) -> None:
    """The destructive direction: the body spells three old lines, the header
    claims four, and ``l5`` -- never mentioned -- was deleted."""
    target = tmp_path / "test.txt"
    target.write_text(FIVE_LINES, encoding="utf-8")

    await _apply(tmp_path, _patch("@@@ -2,4 +2,4 @@@", " l2\n-l3\n+l3x\n l4\n"))

    content = target.read_text(encoding="utf-8")
    assert content == "l1\nl2\nl3x\nl4\nl5\n"
    assert "l5" in content, "a line the hunk never mentions must survive"


@pytest.mark.asyncio
async def test_a_wildly_overcounted_header_cannot_truncate_the_file(
    tmp_path: Path,
) -> None:
    """A count past the end of the file used to take everything after the hunk
    with it, because the slice simply ran off the end."""
    target = tmp_path / "test.txt"
    target.write_text(FIVE_LINES, encoding="utf-8")

    await _apply(tmp_path, _patch("@@@ -2,99 +2,99 @@@", " l2\n-l3\n+l3x\n l4\n"))

    assert target.read_text(encoding="utf-8") == "l1\nl2\nl3x\nl4\nl5\n"


# ── the correct-header path is undisturbed ──────────────────────────────────


@pytest.mark.asyncio
async def test_a_correct_header_still_applies(tmp_path: Path) -> None:
    target = tmp_path / "test.txt"
    target.write_text(FIVE_LINES, encoding="utf-8")

    await _apply(tmp_path, _patch("@@@ -2,3 +2,3 @@@", " l2\n-l3\n+l3x\n l4\n"))

    assert target.read_text(encoding="utf-8") == "l1\nl2\nl3x\nl4\nl5\n"


@pytest.mark.asyncio
async def test_a_pure_insertion_consumes_only_its_context(tmp_path: Path) -> None:
    """No ``-`` lines at all: the body consumes exactly its context lines."""
    target = tmp_path / "test.txt"
    target.write_text(FIVE_LINES, encoding="utf-8")

    await _apply(tmp_path, _patch("@@@ -2,1 +2,2 @@@", " l2\n+inserted\n"))

    assert target.read_text(encoding="utf-8") == "l1\nl2\ninserted\nl3\nl4\nl5\n"


@pytest.mark.asyncio
async def test_a_pure_deletion_removes_only_what_it_names(tmp_path: Path) -> None:
    target = tmp_path / "test.txt"
    target.write_text(FIVE_LINES, encoding="utf-8")

    await _apply(tmp_path, _patch("@@@ -3,1 +3,0 @@@", "-l3\n"))

    assert target.read_text(encoding="utf-8") == "l1\nl2\nl4\nl5\n"


@pytest.mark.asyncio
async def test_a_hunk_at_the_end_of_the_file(tmp_path: Path) -> None:
    """There is no tail to duplicate here, so this isolates the splice's end
    index from the slice-past-the-end behaviour that hides it."""
    target = tmp_path / "test.txt"
    target.write_text(FIVE_LINES, encoding="utf-8")

    await _apply(tmp_path, _patch("@@@ -5 +5 @@@", "-l5\n+l5x\n"))

    assert target.read_text(encoding="utf-8") == "l1\nl2\nl3\nl4\nl5x\n"


@pytest.mark.asyncio
async def test_a_prepend_hunk_still_lands_at_the_top(tmp_path: Path) -> None:
    """``@@@ -0,0 +1,N @@@`` has old_count 0 and a body that consumes nothing;
    the clamp that makes it work must survive this change."""
    target = tmp_path / "test.txt"
    target.write_text("line1\nline2\n", encoding="utf-8")

    await _apply(tmp_path, _patch("@@@ -0,0 +1,1 @@@", "+header\n"))

    assert target.read_text(encoding="utf-8") == "header\nline1\nline2\n"


# ── multiple hunks, where a bad splice compounds ────────────────────────────


@pytest.mark.asyncio
async def test_a_bad_count_in_one_hunk_does_not_corrupt_a_later_one(
    tmp_path: Path,
) -> None:
    """Hunks are applied bottom-up, so a wrong span in one shifts everything
    the next one is matched against. One miscount used to poison the file."""
    target = tmp_path / "test.txt"
    target.write_text("l1\nl2\nl3\nl4\nl5\nl6\n", encoding="utf-8")

    await _apply(
        tmp_path,
        """*** Begin Patch
*** Update File: test.txt
@@@ -2 +2 @@@
 l2
-l3
+l3x
 l4
@@@ -5,9 +5,9 @@@
-l5
+l5x
*** End Patch""",
    )

    assert target.read_text(encoding="utf-8") == "l1\nl2\nl3x\nl4\nl5x\nl6\n"


@pytest.mark.asyncio
async def test_two_correct_hunks_still_apply_together(tmp_path: Path) -> None:
    target = tmp_path / "test.txt"
    target.write_text("l1\nl2\nl3\nl4\nl5\nl6\n", encoding="utf-8")

    await _apply(
        tmp_path,
        """*** Begin Patch
*** Update File: test.txt
@@@ -2,1 +2,1 @@@
-l2
+l2x
@@@ -5,1 +5,1 @@@
-l5
+l5x
*** End Patch""",
    )

    assert target.read_text(encoding="utf-8") == "l1\nl2x\nl3\nl4\nl5x\nl6\n"


# ── the count keeps its other job ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_trailing_blank_is_still_treated_as_a_separator(tmp_path: Path) -> None:
    """``old_count`` still drives ``_trim_trailing_separators``: it tells a
    blank *context* line apart from a blank that merely separates the hunk
    from the next marker. Making the splice ignore the count must not make the
    trimming ignore it too."""
    target = tmp_path / "test.txt"
    target.write_text("l1\nl2\nl3\n", encoding="utf-8")

    await _apply(
        tmp_path,
        """*** Begin Patch
*** Update File: test.txt
@@@ -2,1 +2,1 @@@
-l2
+l2x

*** End Patch""",
    )

    assert target.read_text(encoding="utf-8") == "l1\nl2x\nl3\n"


@pytest.mark.asyncio
async def test_a_blank_context_line_inside_a_hunk_is_kept(tmp_path: Path) -> None:
    """The other side of that: a blank the count *does* account for is real
    content and must still match and survive."""
    target = tmp_path / "test.txt"
    target.write_text("l1\n\nl3\n", encoding="utf-8")

    await _apply(
        tmp_path,
        """*** Begin Patch
*** Update File: test.txt
@@@ -1,3 +1,3 @@@
 l1

-l3
+l3x
*** End Patch""",
    )

    assert target.read_text(encoding="utf-8") == "l1\n\nl3x\n"


# ── a genuinely wrong hunk must still be rejected ───────────────────────────


@pytest.mark.asyncio
async def test_context_that_does_not_match_is_still_an_error(tmp_path: Path) -> None:
    """Trusting the body for the span must not mean trusting it for content:
    the verification pass is what earns the body its authority."""
    target = tmp_path / "test.txt"
    target.write_text(FIVE_LINES, encoding="utf-8")

    with pytest.raises(ValueError, match="Context mismatch"):
        await _apply(tmp_path, _patch("@@@ -2,3 +2,3 @@@", " WRONG\n-l3\n+l3x\n l4\n"))

    assert target.read_text(encoding="utf-8") == FIVE_LINES, "a rejected patch writes nothing"


@pytest.mark.asyncio
async def test_a_hunk_running_past_the_end_is_still_an_error(tmp_path: Path) -> None:
    target = tmp_path / "test.txt"
    target.write_text("l1\nl2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="exceeds file length"):
        await _apply(tmp_path, _patch("@@@ -1,4 +1,4 @@@", " l1\n l2\n l3\n-l4\n+l4x\n"))

    assert target.read_text(encoding="utf-8") == "l1\nl2\n"


# ── line endings are not collateral ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_crlf_endings_survive_a_miscounted_hunk(tmp_path: Path) -> None:
    """The splice moves whole lines, so the tail must come back with the
    endings it had -- a CRLF file must not be silently rewritten to LF."""
    target = tmp_path / "test.txt"
    target.write_bytes(b"l1\r\nl2\r\nl3\r\nl4\r\nl5\r\n")

    await _apply(tmp_path, _patch("@@@ -2 +2 @@@", " l2\n-l3\n+l3x\n l4\n"))

    assert target.read_bytes() == b"l1\r\nl2\r\nl3x\r\nl4\r\nl5\r\n"
