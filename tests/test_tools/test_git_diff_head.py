"""Regression tests for #1963: ``git_diff`` must report staged work by default.

The tool describes itself as "staged + unstaged changes", but its body ran a
bare ``git diff`` (working tree against the index), so a fully staged tree
came back as an empty string and a staged new file never appeared at all.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from agentos.tools.builtin import git

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True, check=False).returncode != 0,
    reason="git is not installed",
)


def _git(cwd: Path, *args: str) -> str:
    """Run git with the ambient user/system config neutralised.

    The env is copied rather than replaced because git on Windows needs the
    ambient ``SYSTEMROOT``; the config overrides keep a maintainer's global
    ``commit.gpgsign`` or ``init.defaultBranch`` out of the fixture.
    """
    missing = str(cwd.parent / "no-such-gitconfig")
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": missing,
            "GIT_CONFIG_SYSTEM": missing,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )
    return proc.stdout


def _git_diff_impl() -> Callable[..., Awaitable[str]]:
    """Unwrap ``@tool`` and ``@sandboxed`` so the body runs without a runtime."""
    return git.git_diff.__wrapped__.__wrapped__  # type: ignore[attr-defined]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with one commit, one staged edit, one staged new file
    and one unstaged edit layered on top of the staged one."""
    _git(tmp_path, "init", "-q", "-b", "main", ".")
    (tmp_path / "a.txt").write_text("one\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "init")
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    (tmp_path / "b.txt").write_text("new\n")
    _git(tmp_path, "add", "a.txt", "b.txt")
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n")
    return tmp_path


async def test_default_diff_includes_staged_and_unstaged_changes(repo: Path) -> None:
    out = await _git_diff_impl()(workdir=str(repo))

    assert "+two" in out, "the staged hunk of a.txt is missing"
    assert "+three" in out, "the unstaged hunk of a.txt is missing"
    assert "b/b.txt" in out and "+new" in out, "the staged new file is missing"


async def test_default_diff_on_fully_staged_tree_is_not_empty(repo: Path) -> None:
    _git(repo, "add", "-A")

    out = await _git_diff_impl()(workdir=str(repo))

    assert "+two" in out and "+three" in out
    assert "b/b.txt" in out


async def test_staged_diff_shows_only_the_index(repo: Path) -> None:
    out = await _git_diff_impl()(workdir=str(repo), staged=True)

    assert "+two" in out
    assert "b/b.txt" in out
    assert "+three" not in out, "an unstaged hunk leaked into the staged view"


async def test_path_filter_still_applies(repo: Path) -> None:
    out = await _git_diff_impl()(workdir=str(repo), path="b.txt")

    assert "b/b.txt" in out
    assert "a.txt" not in out


async def test_unborn_repository_still_diffs(tmp_path: Path) -> None:
    """``git diff HEAD`` fails with exit 128 before the first commit; the
    tool must fall back rather than raise."""
    _git(tmp_path, "init", "-q", "-b", "main", ".")
    (tmp_path / "a.txt").write_text("one\n")
    _git(tmp_path, "add", "a.txt")
    (tmp_path / "a.txt").write_text("one\ntwo\n")

    combined = await _git_diff_impl()(workdir=str(tmp_path))
    staged = await _git_diff_impl()(workdir=str(tmp_path), staged=True)

    assert "+one" in combined and "+two" in combined
    assert "+one" in staged and "+two" not in staged


def test_git_diff_argv_fingerprints_the_head_diff() -> None:
    assert git._git_diff_argv({}) == ("git", "diff", "HEAD")
    assert git._git_diff_argv({"staged": True}) == ("git", "diff", "--cached", "HEAD")
    assert git._git_diff_argv({"staged": True, "path": "x"}) == (
        "git",
        "diff",
        "--cached",
        "HEAD",
        "--",
        "x",
    )
