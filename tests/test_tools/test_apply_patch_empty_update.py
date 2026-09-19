"""An ``*** Update File:`` block with no hunks must be refused, not "applied" (#2837).

``_parse_patch`` skipped every line under ``*** Update File:`` that did not open
with ``@@@ ``, so a block holding a standard unified-diff header (``@@ -1,1
+1,1 @@``), a note, or nothing at all became ``UpdateFile(hunks=[])``. The
plan then counted it as modified, the commit rewrote the file unchanged
(touching its mtime and firing the workspace-write listeners), and the tool
reported ``Applied patch: 1 file(s) modified``. A false success is never
retried. The block now gets the same treatment as ``*** Add File:``: a bare
blank is tolerated, anything else outside a hunk is an error, and no hunks is
an error.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from agentos.tools.builtin import patch as patch_tool
from agentos.tools.builtin.patch import UpdateFile, _parse_patch
from agentos.tools.types import ToolContext, current_tool_context


def _original_async(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    return fn.__wrapped__.__wrapped__  # type: ignore[attr-defined, no-any-return]


async def _apply(workspace: Path, patch_text: str) -> str:
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    try:
        return await _original_async(patch_tool.apply_patch)(patch_text)
    finally:
        current_tool_context.reset(token)


def _update(body: str, path: str = "app.py") -> str:
    return f"*** Begin Patch\n*** Update File: {path}\n{body}*** End Patch\n"


# ── the report ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_unified_diff_header_is_refused_and_the_file_is_untouched(
    tmp_path: Path,
) -> None:
    target = tmp_path / "app.py"
    target.write_text("print('old')\n", encoding="utf-8")
    mtime_before = target.stat().st_mtime_ns

    with pytest.raises(ValueError) as excinfo:
        await _apply(tmp_path, _update("@@ -1,1 +1,1 @@\n-print('old')\n+print('new')\n"))

    message = str(excinfo.value)
    assert "Invalid line in '*** Update File: app.py' block" in message
    assert "expected a '@@@ ' hunk header" in message
    assert "'@@ -1,1 +1,1 @@'" in message
    assert target.read_text(encoding="utf-8") == "print('old')\n"
    assert target.stat().st_mtime_ns == mtime_before


def test_a_unified_diff_header_gets_the_hint_that_names_it() -> None:
    """The two-@ header is the shape models write most; say what it is."""
    with pytest.raises(ValueError, match=r"unified-diff header; hunks here open with '@@@'"):
        _parse_patch(_update("@@ -1,1 +1,1 @@\n-a\n+b\n"))


def test_a_note_outside_a_hunk_does_not_get_the_unified_diff_hint() -> None:
    with pytest.raises(ValueError) as excinfo:
        _parse_patch(_update("# bump the version\n"))

    assert "'# bump the version'" in str(excinfo.value)
    assert "unified-diff" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_an_empty_update_block_is_refused(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x\n", encoding="utf-8")

    with pytest.raises(
        ValueError, match=r"No hunks found in '\*\*\* Update File: app\.py' block"
    ) as excinfo:
        await _apply(tmp_path, _update(""))

    assert "@@@ -old_start,count +new_start,count @@@" in str(excinfo.value)


def test_an_update_block_of_only_blank_lines_is_refused() -> None:
    with pytest.raises(ValueError, match=r"No hunks found"):
        _parse_patch(_update("\n\n   \n"))


def test_the_message_names_the_block_not_the_patch() -> None:
    """With several files in one patch the model needs to know which block."""
    text = (
        "*** Begin Patch\n"
        "*** Update File: first.py\n"
        "@@@ -1,1 +1,1 @@@\n-a\n+b\n"
        "*** Update File: second.py\n"
        "*** Delete File: third.py\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match=r"'\*\*\* Update File: second\.py' block"):
        _parse_patch(text)


# ── nothing else in the patch is touched ───────────────────────────────────


@pytest.mark.asyncio
async def test_a_bad_update_block_fails_the_whole_patch_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parsing precedes planning, so an earlier valid op is not half-applied
    and no write listener fires for a patch that did nothing."""
    (tmp_path / "app.py").write_text("x\n", encoding="utf-8")
    recorded: list[object] = []
    monkeypatch.setattr(
        patch_tool, "_record_workspace_file_writes", lambda *a, **k: recorded.append(a)
    )
    text = (
        "*** Begin Patch\n*** Add File: new.txt\n+hello\n*** Update File: app.py\n*** End Patch\n"
    )

    with pytest.raises(ValueError, match=r"No hunks found"):
        await _apply(tmp_path, text)

    assert not (tmp_path / "new.txt").exists()
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "x\n"
    assert recorded == []


# ── what still parses ──────────────────────────────────────────────────────


def test_bare_blank_lines_before_the_first_hunk_are_formatting() -> None:
    ops = _parse_patch(_update("\n\n@@@ -1,1 +1,1 @@@\n-a\n+b\n"))

    assert isinstance(ops[0], UpdateFile)
    assert len(ops[0].hunks) == 1
    assert ops[0].hunks[0].lines == ["-a", "+b"]


@pytest.mark.asyncio
async def test_a_well_formed_update_still_applies(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("print('old')\n", encoding="utf-8")

    result = await _apply(tmp_path, _update("@@@ -1,1 +1,1 @@@\n-print('old')\n+print('new')\n"))

    assert result == "Applied patch: 1 file(s) modified"
    assert target.read_text(encoding="utf-8") == "print('new')\n"


def test_blank_lines_after_a_hunk_still_belong_to_that_hunk() -> None:
    """The outer blank allowance must not steal a hunk's trailing separator
    handling from ``_trim_trailing_separators``."""
    ops = _parse_patch(_update("@@@ -1,1 +1,1 @@@\n-a\n+b\n\n\n"))

    assert isinstance(ops[0], UpdateFile)
    assert ops[0].hunks[0].lines == ["-a", "+b"]


def test_a_malformed_triple_at_header_keeps_its_own_error() -> None:
    with pytest.raises(ValueError, match=r"Invalid hunk header"):
        _parse_patch(_update("@@@ not a header @@@\n-a\n+b\n"))


def test_add_and_delete_blocks_are_unchanged() -> None:
    ops = _parse_patch(
        "*** Begin Patch\n*** Add File: a.txt\n+x\n*** Delete File: b.txt\n*** End Patch\n"
    )

    assert [type(op).__name__ for op in ops] == ["AddFile", "DeleteFile"]
