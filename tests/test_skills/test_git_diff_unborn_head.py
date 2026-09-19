"""The bundled ``git-diff`` script in a repository with no commit yet (#2882).

Before the first commit HEAD is an unborn branch, and ``git diff HEAD`` exits
128. Dropping the revision is not a substitute -- bare ``git diff`` loses what
is staged, bare ``git diff --cached`` loses edits made after staging -- so the
script diffs against the empty tree, which is what ``git diff HEAD`` would
show if the repository's history were empty. These drive the real script in a
child process against a real repository.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "src" / "agentos" / "skills" / "bundled" / "git-diff" / "scripts" / "git_diff.py"

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git is not installed",
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=test",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.autocrlf=false",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _run(repo: Path, mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--mode", mode, "--cwd", str(repo)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


@pytest.fixture
def unborn(tmp_path: Path) -> Path:
    """A fresh repository: one staged line, plus an edit made after staging."""
    _git(tmp_path, "init", "-q")
    target = tmp_path / "a.txt"
    target.write_bytes(b"staged line\n")
    _git(tmp_path, "add", "a.txt")
    target.write_bytes(b"staged line\nedited after staging\n")
    return tmp_path


def test_worktree_mode_shows_staged_content_and_later_edits(unborn: Path) -> None:
    """Bare ``git diff`` here would show only the later edit; ``git diff
    --cached`` only the staged line. ``git diff HEAD`` means both."""
    result = _run(unborn, "worktree")

    assert result.returncode == 0, result.stderr
    assert "+staged line" in result.stdout
    assert "+edited after staging" in result.stdout


def test_cached_mode_shows_the_staged_content(unborn: Path) -> None:
    result = _run(unborn, "cached")

    assert result.returncode == 0, result.stderr
    assert "+staged line" in result.stdout
    assert "edited after staging" not in result.stdout


def test_default_mode_reports_the_staged_change(unborn: Path) -> None:
    result = _run(unborn, "cached_fallback_worktree")

    assert result.returncode == 0, result.stderr
    assert "+staged line" in result.stdout


def test_default_mode_falls_back_to_the_worktree_when_nothing_is_staged(
    tmp_path: Path,
) -> None:
    """Staged, then unstaged again: the index is empty, the file is not."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "b.txt").write_bytes(b"only in the worktree\n")
    _git(tmp_path, "add", "b.txt")
    _git(tmp_path, "rm", "-q", "--cached", "b.txt")

    result = _run(tmp_path, "cached_fallback_worktree")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "NO_DIFF"  # untracked files are not diffed


def test_a_repository_with_a_commit_still_diffs_against_head(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "c.txt").write_bytes(b"committed\n")
    _git(tmp_path, "add", "c.txt")
    _git(tmp_path, "commit", "-q", "-m", "first")
    (tmp_path / "c.txt").write_bytes(b"committed\nchanged\n")

    result = _run(tmp_path, "worktree")

    assert result.returncode == 0, result.stderr
    assert "+changed" in result.stdout
    assert "+committed" not in result.stdout


def test_outside_a_repository_git_reports_its_own_error(tmp_path: Path) -> None:
    result = _run(tmp_path, "worktree")

    assert result.returncode != 0
    assert "not a git repository" in result.stderr.lower()
