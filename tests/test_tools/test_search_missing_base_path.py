"""``glob_search``/``grep_search`` report a missing base path instead of "no matches".

Both tools resolved the base and searched it without checking that it exists,
so a typo'd directory answered ``No matches`` -- which is not self-correcting:
the model reads it as "the symbol is not in this codebase" and stops looking,
rather than as "you named a directory that is not there". Every other
filesystem tool raises ``FileNotFoundError`` for the same input.

These tests pin the new contract, the error's agreement with ``list_dir``, and
-- the part that is easy to get wrong -- that the check sits *after* the
workspace-strict gate, so strict mode does not become an existence oracle for
paths outside the workspace.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agentos.tools.builtin import filesystem as fs
from agentos.tools.types import (
    CallerKind,
    ToolContext,
    WorkspaceAccessError,
    current_tool_context,
)


@contextmanager
def _tool_context(workspace: Path, *, strict: bool = True) -> Iterator[None]:
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            channel_kind="cli",
            channel_id="cli:test",
            workspace_dir=str(workspace),
            workspace_strict=strict,
        )
    )
    try:
        yield
    finally:
        current_tool_context.reset(token)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "app.py").write_text("needle = 1\n", encoding="utf-8")
    return ws


# ── The base does not exist ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_glob_search_names_the_missing_directory(workspace: Path) -> None:
    with _tool_context(workspace):
        with pytest.raises(FileNotFoundError) as excinfo:
            await fs.glob_search("*.py", path=str(workspace / "scr"))
    assert "scr" in str(excinfo.value)


@pytest.mark.asyncio
async def test_grep_search_names_the_missing_directory(workspace: Path) -> None:
    with _tool_context(workspace):
        with pytest.raises(FileNotFoundError) as excinfo:
            await fs.grep_search("needle", path=str(workspace / "scr"))
    assert "scr" in str(excinfo.value)


@pytest.mark.asyncio
async def test_the_error_reports_the_path_the_caller_typed(workspace: Path) -> None:
    """A resolved absolute path the caller never wrote is not a usable hint."""
    with _tool_context(workspace):
        with pytest.raises(FileNotFoundError) as excinfo:
            await fs.grep_search("needle", path="src/nope")
    assert "src/nope" in str(excinfo.value)


@pytest.mark.asyncio
async def test_grep_search_checks_the_base_before_applying_include(
    workspace: Path,
) -> None:
    """The include filter must not turn a missing directory back into no-matches."""
    with _tool_context(workspace):
        with pytest.raises(FileNotFoundError):
            await fs.grep_search("needle", path=str(workspace / "gone"), include="*.py")


@pytest.mark.asyncio
async def test_a_missing_file_base_is_reported_too(workspace: Path) -> None:
    """``grep_search`` accepts a file as its base, so a missing file counts."""
    with _tool_context(workspace):
        with pytest.raises(FileNotFoundError):
            await fs.grep_search("needle", path=str(workspace / "src" / "gone.py"))


@pytest.mark.asyncio
async def test_a_deeply_nested_missing_path_is_reported(workspace: Path) -> None:
    with _tool_context(workspace):
        with pytest.raises(FileNotFoundError):
            await fs.glob_search("*.py", path=str(workspace / "a" / "b" / "c"))


@pytest.mark.asyncio
async def test_a_symlink_to_a_removed_target_is_reported(workspace: Path) -> None:
    """``exists()`` follows the link, so a dangling link is a missing path."""
    target = workspace / "real"
    target.mkdir()
    link = workspace / "link"
    link.symlink_to(target, target_is_directory=True)
    target.rmdir()
    with _tool_context(workspace):
        with pytest.raises(FileNotFoundError):
            await fs.grep_search("needle", path=str(link))


@pytest.mark.asyncio
async def test_the_error_agrees_with_list_dir_on_the_same_input(
    workspace: Path,
) -> None:
    """"The same error the other tools raise" -- pinned, not assumed."""
    missing = str(workspace / "scr")
    with _tool_context(workspace):
        with pytest.raises(FileNotFoundError) as from_list_dir:
            await fs.list_dir(missing)
        with pytest.raises(FileNotFoundError) as from_grep:
            await fs.grep_search("needle", path=missing)
        with pytest.raises(FileNotFoundError) as from_glob:
            await fs.glob_search("*.py", path=missing)
    prefix = "Path not found:"
    assert str(from_list_dir.value).startswith(prefix)
    assert str(from_grep.value).startswith(prefix)
    assert str(from_glob.value).startswith(prefix)


# ── Ordering: the gate wins, so this is not an existence oracle ──────────────


@pytest.mark.asyncio
async def test_a_missing_path_outside_the_workspace_is_refused_not_reported(
    workspace: Path, tmp_path: Path
) -> None:
    """Strict mode must not learn to answer "does this outside path exist?".

    ``_gate_workspace_strict_read``'s own docstring requires existence checks
    to sit behind it. If this check ran first, the two outside paths below
    would return different errors and that difference is the oracle.
    """
    outside_missing = str(tmp_path / "elsewhere" / "gone")
    with _tool_context(workspace):
        with pytest.raises(WorkspaceAccessError):
            await fs.grep_search("needle", path=outside_missing)
        with pytest.raises(WorkspaceAccessError):
            await fs.glob_search("*.py", path=outside_missing)


@pytest.mark.asyncio
async def test_outside_paths_are_indistinguishable_whether_they_exist_or_not(
    workspace: Path, tmp_path: Path
) -> None:
    """The existing and the missing outside path must fail the same way."""
    present = tmp_path / "elsewhere"
    present.mkdir()
    missing = tmp_path / "elsewhere-gone"
    with _tool_context(workspace):
        with pytest.raises(WorkspaceAccessError) as for_present:
            await fs.grep_search("needle", path=str(present))
        with pytest.raises(WorkspaceAccessError) as for_missing:
            await fs.grep_search("needle", path=str(missing))
    assert type(for_present.value) is type(for_missing.value)


# ── The direction the issue did not ask about: no over-correction ────────────


@pytest.mark.asyncio
async def test_an_existing_directory_with_no_matches_is_still_not_an_error(
    workspace: Path,
) -> None:
    """"No matches" stays the answer when the path is real and empty of hits."""
    with _tool_context(workspace):
        out = await fs.grep_search("definitely-absent-symbol", path=str(workspace))
    assert "No matches" in out


@pytest.mark.asyncio
async def test_glob_search_with_no_hits_in_a_real_directory_still_returns_text(
    workspace: Path,
) -> None:
    with _tool_context(workspace):
        out = await fs.glob_search("*.rs", path=str(workspace / "src"))
    assert "No files matched" in out


@pytest.mark.asyncio
async def test_the_default_base_is_never_reported_missing(workspace: Path) -> None:
    """``path=None`` resolves to the workspace root, which exists."""
    with _tool_context(workspace):
        assert "app.py" in await fs.glob_search("**/*.py")
        assert "needle" in await fs.grep_search("needle")


@pytest.mark.asyncio
async def test_an_existing_file_base_still_searches_that_file(workspace: Path) -> None:
    """A file base is legitimate for grep_search and must not become an error."""
    with _tool_context(workspace):
        out = await fs.grep_search("needle", path=str(workspace / "src" / "app.py"))
    assert "app.py:1:" in out
