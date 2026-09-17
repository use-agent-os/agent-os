"""Prove glob_search handles patterns with leading slashes and root patterns.

Fixes #2689: ``pathlib.Path.glob`` raises ``NotImplementedError`` when given
patterns with leading slashes (e.g. ``/**/*.py``, ``/*.py``). ``glob_search``
normalizes the pattern relative to the search base instead of raising.
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
    (ws / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
    (ws / "src" / "util.py").write_text("print('util')\n", encoding="utf-8")
    (ws / "README.md").write_text("# Test\n", encoding="utf-8")
    return ws


@pytest.mark.asyncio
async def test_glob_search_handles_leading_slash_recursive(workspace: Path) -> None:
    with _tool_context(workspace):
        out = await fs.glob_search("/**/*.py", path=str(workspace))
    assert "app.py" in out
    assert "util.py" in out


@pytest.mark.asyncio
async def test_glob_search_handles_leading_slash_direct_child(workspace: Path) -> None:
    with _tool_context(workspace):
        out = await fs.glob_search("/*.md", path=str(workspace))
    assert "README.md" in out
    assert "app.py" not in out


@pytest.mark.asyncio
async def test_glob_search_handles_leading_slash_nested_dir(workspace: Path) -> None:
    with _tool_context(workspace):
        out = await fs.glob_search("/src/*.py", path=str(workspace))
    assert "app.py" in out
    assert "util.py" in out


@pytest.mark.asyncio
async def test_glob_search_handles_leading_backslash(workspace: Path) -> None:
    with _tool_context(workspace):
        out = await fs.glob_search("\\*.md", path=str(workspace))
    assert "README.md" in out


@pytest.mark.asyncio
async def test_glob_search_handles_root_slash_pattern(workspace: Path) -> None:
    with _tool_context(workspace):
        out = await fs.glob_search("/", path=str(workspace))
    assert "README.md" in out
    assert "src" in out


@pytest.mark.asyncio
async def test_glob_search_handles_empty_pattern(workspace: Path) -> None:
    with _tool_context(workspace):
        out = await fs.glob_search("", path=str(workspace))
    assert "README.md" in out
    assert "src" in out


@pytest.mark.asyncio
async def test_glob_search_standard_relative_pattern_positive_control(workspace: Path) -> None:
    with _tool_context(workspace):
        out = await fs.glob_search("src/*.py", path=str(workspace))
    assert "app.py" in out
    assert "util.py" in out
