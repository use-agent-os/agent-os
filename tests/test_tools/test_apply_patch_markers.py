"""Which ``*** Begin Patch`` / ``*** End Patch`` lines delimit the patch body.

Both markers were located by independent scans from index 0, so the end marker
could resolve to a line *before* the begin marker (an ``*** End Patch`` quoted
in a preamble) or to a line *inside* the body (a hunk context line for a file
that documents the patch format). Either way the body slice came back short,
the operations after the cut were dropped, and ``apply_patch`` still reported
success -- the failure mode these tests exist to keep out.
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
async def test_an_end_marker_quoted_in_a_preamble_does_not_drop_the_patch(
    tmp_path: Path,
) -> None:
    """The reported case: the model echoes a transcript before its own patch."""
    result = await _apply(
        tmp_path,
        "Here is the block you sent me:\n"
        "*** End Patch\n"
        "and here is the edit:\n"
        "*** Begin Patch\n"
        "*** Add File: sample.txt\n"
        "+hello world\n"
        "*** End Patch\n",
    )

    assert "1 file(s) added" in result
    assert (tmp_path / "sample.txt").read_text(encoding="utf-8") == "hello world"


@pytest.mark.asyncio
async def test_a_hunk_may_edit_a_line_that_reads_like_the_end_marker(
    tmp_path: Path,
) -> None:
    """The other direction: the marker text is the *content* being patched.

    A context line is written ``" *** End Patch"``, which `.strip()` cannot
    tell from the marker, so the body was cut here and the rest of the hunk --
    the actual edit -- never reached the parser.
    """
    target = tmp_path / "format.md"
    target.write_text("intro\n*** End Patch\nold tail\n", encoding="utf-8")

    result = await _apply(
        tmp_path,
        "*** Begin Patch\n"
        "*** Update File: format.md\n"
        "@@@ -1,3 +1,3 @@@\n"
        " intro\n"
        " *** End Patch\n"
        "-old tail\n"
        "+new tail\n"
        "*** End Patch\n",
    )

    assert "1 file(s) modified" in result
    assert target.read_text(encoding="utf-8") == "intro\n*** End Patch\nnew tail\n"


@pytest.mark.asyncio
async def test_added_content_may_contain_the_end_marker(tmp_path: Path) -> None:
    """``*** Add File`` content is ``+``-prefixed, so it is never the marker."""
    result = await _apply(
        tmp_path,
        "*** Begin Patch\n"
        "*** Add File: doc.md\n"
        "+A patch block ends with\n"
        "+*** End Patch\n"
        "+and nothing follows.\n"
        "*** End Patch\n",
    )

    assert "1 file(s) added" in result
    assert (tmp_path / "doc.md").read_text(encoding="utf-8") == (
        "A patch block ends with\n*** End Patch\nand nothing follows."
    )


@pytest.mark.asyncio
async def test_a_begin_marker_quoted_in_a_preamble_still_parses(tmp_path: Path) -> None:
    """Mirror of the reported case. Passes either way by design.

    A stray *begin* marker was already harmless -- the extra prose it pulls
    into the body is skipped as an unknown directive -- and this pins that the
    new end-marker search did not make it harmful.
    """
    result = await _apply(
        tmp_path,
        "*** Begin Patch\n"
        "(that was the format, here is the patch)\n"
        "*** Begin Patch\n"
        "*** Add File: sample.txt\n"
        "+hello\n"
        "*** End Patch\n",
    )

    assert "1 file(s) added" in result
    assert (tmp_path / "sample.txt").read_text(encoding="utf-8") == "hello"


def test_the_span_is_the_pair_that_delimits_the_body() -> None:
    """Straight at the helper: which two lines were chosen."""
    lines = [
        "*** End Patch",
        "*** Begin Patch",
        "*** Add File: sample.txt",
        "+hello",
        "*** End Patch",
    ]

    assert patch_tool._marker_span(lines) == (1, 4)


def test_a_flush_end_marker_closes_an_indented_begin_marker() -> None:
    """Passes either way by design.

    Only a marker indented *past* the begin marker is read as content, so a
    block whose opening line drifted right still closes on a flush marker.
    """
    lines = ["  *** Begin Patch", "*** Add File: sample.txt", "+hello", "*** End Patch"]

    assert patch_tool._marker_span(lines) == (0, 3)


def test_an_end_marker_only_in_the_preamble_is_reported_as_missing() -> None:
    """Loud, not silent: no end marker follows the begin marker.

    The message is the one this parser has always raised for a missing end
    marker; callers that match on it keep working.
    """
    with pytest.raises(ValueError, match=r"Missing '\*\*\* End Patch' marker"):
        patch_tool._parse_patch(
            "*** End Patch\n*** Begin Patch\n*** Add File: sample.txt\n+hello\n"
        )


def test_a_missing_begin_marker_is_still_reported() -> None:
    """Passes either way by design -- guards the branch the fix reordered."""
    with pytest.raises(ValueError, match=r"Missing '\*\*\* Begin Patch' marker"):
        patch_tool._parse_patch("*** Add File: sample.txt\n+hello\n*** End Patch\n")
