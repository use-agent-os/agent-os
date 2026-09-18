"""``grep_search`` ``include`` globs match the path as well as the filename.

The filter used to run ``fnmatch`` against ``fp.name`` alone, so a
path-qualified glob such as ``tests/*.py`` could never match anything and the
tool answered ``No matches for '<pattern>'`` -- indistinguishable, to the
agent, from "this code does not exist". These tests pin the new contract
(the glob may name directories relative to the search base) and the old one
(a bare filename glob keeps matching at any depth).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agentos.tools.builtin import filesystem as fs
from agentos.tools.types import CallerKind, ToolContext, current_tool_context


@contextmanager
def _tool_context(workspace: Path) -> Iterator[None]:
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            channel_kind="cli",
            channel_id="cli:test",
            workspace_dir=str(workspace),
            workspace_strict=True,
        )
    )
    try:
        yield
    finally:
        current_tool_context.reset(token)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_basic.py").write_text("def test_one():\n    pass\n")
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "utils.py").write_text("def test_helper():\n    pass\n")
    (tmp_path / "src" / "pkg" / "mod.py").write_text("def test_nested():\n    pass\n")
    (tmp_path / "README.md").write_text("def test_ not code\n")
    return tmp_path


# ``<absolute path>:<lineno>: <text>`` -- split on the line number, not the
# first colon, which on Windows is the drive letter's.
_RESULT_LINE = re.compile(r"^(?P<file>.+?):(?P<lineno>\d+): ")


async def _hits(repo: Path, include: str, *, path: str | None = None) -> set[str]:
    with _tool_context(repo):
        out = await fs.grep_search("def test_", path=path or str(repo), include=include)
    if out.startswith("No matches"):
        return set()
    paths: set[str] = set()
    for line in out.splitlines():
        match = _RESULT_LINE.match(line)
        assert match is not None, line
        paths.add(Path(match["file"]).relative_to(repo).as_posix())
    return paths


@pytest.mark.asyncio
async def test_path_qualified_glob_matches_files_under_that_directory(repo: Path) -> None:
    assert await _hits(repo, "tests/*.py") == {"tests/test_basic.py"}


@pytest.mark.asyncio
async def test_bare_filename_glob_still_matches_at_any_depth(repo: Path) -> None:
    assert await _hits(repo, "*.py") == {
        "tests/test_basic.py",
        "src/utils.py",
        "src/pkg/mod.py",
    }


@pytest.mark.asyncio
async def test_bare_filename_glob_with_a_literal_prefix_still_matches_nested_files(
    repo: Path,
) -> None:
    """``test_*.py`` names a filename shape, so it must not need the directory."""
    assert await _hits(repo, "test_*.py") == {"tests/test_basic.py"}


@pytest.mark.asyncio
async def test_double_star_glob_matches_files_at_every_depth(repo: Path) -> None:
    """``**/`` spans zero or more directories, as in every other glob dialect."""
    assert await _hits(repo, "src/**/*.py") == {"src/utils.py", "src/pkg/mod.py"}


@pytest.mark.asyncio
async def test_glob_is_relative_to_the_search_path(repo: Path) -> None:
    assert await _hits(repo, "pkg/*.py", path=str(repo / "src")) == {"src/pkg/mod.py"}


@pytest.mark.asyncio
async def test_path_qualified_glob_that_matches_nothing_reports_no_matches(repo: Path) -> None:
    with _tool_context(repo):
        out = await fs.grep_search("def test_", path=str(repo), include="docs/*.py")

    assert out == "No matches for 'def test_'"


@pytest.mark.asyncio
async def test_grep_search_skips_binary_files(tmp_path: Path) -> None:
    (tmp_path / "text.txt").write_text("def test_text(): pass\n", encoding="utf-8")
    (tmp_path / "app.exe").write_bytes(b"MZ\x90\x00def test_binary")
    (tmp_path / "data.bin").write_bytes(b"\x00\x01\x02def test_raw_nul")
    (tmp_path / "doc.docx").write_bytes(b"PK\x03\x04def test_docx")

    with _tool_context(tmp_path):
        out = await fs.grep_search("def test_", path=str(tmp_path))

    assert "text.txt" in out
    assert "app.exe" not in out
    assert "data.bin" not in out
    assert "doc.docx" not in out


@pytest.mark.asyncio
async def test_grep_search_single_binary_file_returns_no_matches(tmp_path: Path) -> None:
    binary_file = tmp_path / "sample.bin"
    binary_file.write_bytes(b"\x00\x01needle\x00")

    with _tool_context(tmp_path):
        out = await fs.grep_search("needle", path=str(binary_file))

    assert out == "No matches for 'needle'"

