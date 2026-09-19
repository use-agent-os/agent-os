"""Tests for Update File hunk validation in apply_patch.

When an ``*** Update File: <path>`` section contains zero recognised ``@@@ `` hunks
(such as an empty update section, or an update using git unified-diff ``@@ -1,3 +1,3 @@``
headers), apply_patch previously skipped the unrecognised lines, created an
UpdateFile with hunks=[], and reported ``Applied patch: 1 file(s) modified`` while
rewriting the unchanged file to disk and leaving its content completely untouched.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from agentos.tools.builtin import patch as patch_tool
from agentos.tools.builtin.patch import UpdateFile, _plan_ops
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
async def test_update_file_with_no_hunks_raises_value_error(tmp_path: Path) -> None:
    """An Update File block without hunks must raise ValueError rather than reporting success."""
    target = tmp_path / "sample.txt"
    target.write_text("hello world\n", encoding="utf-8")

    patch_text = "*** Begin Patch\n*** Update File: sample.txt\n*** End Patch\n"

    with pytest.raises(
        ValueError, match=r"No hunks found in '\*\*\* Update File: sample\.txt' block"
    ):
        await _apply(tmp_path, patch_text)


@pytest.mark.asyncio
async def test_update_file_with_unified_diff_header_raises_value_error(tmp_path: Path) -> None:
    """An Update File block with git unified diff @@ headers instead of @@@ raises ValueError."""
    target = tmp_path / "sample.txt"
    target.write_text("old text\n", encoding="utf-8")

    patch_text = (
        "*** Begin Patch\n"
        "*** Update File: sample.txt\n"
        "@@ -1,1 +1,1 @@\n"
        "-old text\n"
        "+new text\n"
        "*** End Patch\n"
    )

    with pytest.raises(
        ValueError,
        match=(
            r"Invalid line in '\*\*\* Update File: sample\.txt' block "
            r"\(expected a '@@@ ' hunk header\)"
        ),
    ):
        await _apply(tmp_path, patch_text)


@pytest.mark.asyncio
async def test_update_file_with_invalid_line_before_hunk_raises(tmp_path: Path) -> None:
    """An invalid line before a hunk raises rather than being silently ignored."""
    target = tmp_path / "sample.txt"
    target.write_text("line 1\nline 2\n", encoding="utf-8")

    patch_text = (
        "*** Begin Patch\n"
        "*** Update File: sample.txt\n"
        "Note: modifying this line\n"
        "@@@ -1,1 +1,1 @@@\n"
        "-line 1\n"
        "+modified 1\n"
        "*** End Patch\n"
    )

    with pytest.raises(
        ValueError,
        match=(
            r"Invalid line in '\*\*\* Update File: sample\.txt' block "
            r"\(expected a '@@@ ' hunk header\)"
        ),
    ):
        await _apply(tmp_path, patch_text)


@pytest.mark.asyncio
async def test_update_file_leaves_workspace_untouched_on_error(tmp_path: Path) -> None:
    """Ensure failed patch execution leaves file content and mtime completely intact."""
    target = tmp_path / "sample.txt"
    target.write_text("hello world\n", encoding="utf-8")
    mtime_before = target.stat().st_mtime_ns

    patch_text = "*** Begin Patch\n*** Update File: sample.txt\n*** End Patch\n"

    with pytest.raises(ValueError):
        await _apply(tmp_path, patch_text)

    assert target.read_text(encoding="utf-8") == "hello world\n"
    assert target.stat().st_mtime_ns == mtime_before


@pytest.mark.asyncio
async def test_valid_update_file_with_blank_separators_applies_successfully(
    tmp_path: Path,
) -> None:
    """Positive control: valid hunks with formatting blank lines apply cleanly."""
    target = tmp_path / "sample.txt"
    target.write_text("line 1\nline 2\nline 3\n", encoding="utf-8")

    patch_text = (
        "*** Begin Patch\n"
        "*** Update File: sample.txt\n"
        "\n"
        "@@@ -1,1 +1,1 @@@\n"
        "-line 1\n"
        "+alpha\n"
        "\n"
        "@@@ -3,1 +3,1 @@@\n"
        "-line 3\n"
        "+gamma\n"
        "\n"
        "*** End Patch\n"
    )

    result = await _apply(tmp_path, patch_text)
    assert "1 file(s) modified" in result
    assert target.read_text(encoding="utf-8") == "alpha\nline 2\ngamma\n"


def test_plan_ops_rejects_empty_hunks(tmp_path: Path) -> None:
    """Defense-in-depth: _plan_ops refuses an UpdateFile with empty hunks."""
    target = tmp_path / "sample.txt"
    target.write_text("hello\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"No hunks to apply for update: sample\.txt"):
        _plan_ops([UpdateFile(path="sample.txt", hunks=[])], root=tmp_path)
