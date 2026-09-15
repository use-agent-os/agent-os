"""``git_diff`` must return the staged + unstaged view its description promises.

Bare ``git diff`` compares the working tree against the *index*, so staged work
is absent from it entirely — and on a fully staged tree it returns an empty
string with exit 0, which the model reads as "nothing to review" (#1963). These
tests pin the combined view, the ``staged=True`` view that must stay narrow, the
first-commit repository where ``HEAD`` does not resolve yet, and the sandbox
fingerprint that has to keep describing what actually runs (#614).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentos.sandbox.config import SandboxSettings
from agentos.sandbox.integration import configure_runtime, reset_runtime
from agentos.tools.builtin import git
from agentos.tools.types import ToolContext, current_tool_context


def _git(repo: Path, *args: str) -> None:
    """Run git with the ambient user/system config neutralised.

    A maintainer's global ``commit.gpgsign`` or commit template would otherwise
    decide whether this test can make a commit, and the env is copied rather
    than replaced because git on Windows needs the ambient ``SYSTEMROOT``.
    """
    missing = str(repo.parent / "no-such-gitconfig")
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
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


@pytest.fixture
def empty_repo(tmp_path: Path) -> Iterator[Path]:
    """A repository with no commit yet — ``HEAD`` does not resolve.

    The sandbox runtime is configured with ``sandbox=False`` so the
    ``@sandboxed`` gate runs the handler inline instead of refusing
    fail-closed, mirroring ``test_web_fetch_download_cap``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    configure_runtime(
        SandboxSettings(sandbox=False, security_grading=False, allow_legacy_mode=True),
        workspace=repo,
    )
    token = current_tool_context.set(ToolContext(workspace_dir=str(repo)))
    try:
        yield repo
    finally:
        current_tool_context.reset(token)
        reset_runtime()


@pytest.fixture
def repo(empty_repo: Path) -> Path:
    """One commit, then a staged edit, a staged new file and an unstaged edit."""
    (empty_repo / "tracked.txt").write_text("one\n", encoding="utf-8", newline="\n")
    _git(empty_repo, "add", "-A")
    _git(empty_repo, "commit", "-qm", "init")

    (empty_repo / "tracked.txt").write_text("one\nstaged\n", encoding="utf-8", newline="\n")
    (empty_repo / "added.txt").write_text("brand new\n", encoding="utf-8", newline="\n")
    _git(empty_repo, "add", "-A")
    (empty_repo / "tracked.txt").write_text(
        "one\nstaged\nunstaged\n", encoding="utf-8", newline="\n"
    )
    return empty_repo


async def test_default_diff_includes_a_staged_hunk(repo: Path) -> None:
    """The line that only exists in the index must reach the model."""
    out = await git.git_diff()

    assert "+staged" in out


async def test_default_diff_includes_a_staged_new_file(repo: Path) -> None:
    """A staged addition is the sharpest case: the whole file is missing, not a hunk."""
    out = await git.git_diff()

    assert "added.txt" in out
    assert "+brand new" in out


async def test_default_diff_still_includes_an_unstaged_hunk(repo: Path) -> None:
    """Guard for the direction the report did not cover — passes either way by design."""
    out = await git.git_diff()

    assert "+unstaged" in out


async def test_fully_staged_tree_is_not_reported_as_empty(repo: Path) -> None:
    """``git add -A`` is what ``git_commit`` itself runs; the diff must survive it."""
    _git(repo, "add", "-A")

    out = await git.git_diff()

    assert out.strip() != ""
    assert "+staged" in out
    assert "+unstaged" in out
    assert "added.txt" in out


async def test_staged_only_view_excludes_the_unstaged_edit(repo: Path) -> None:
    """``staged=True`` stays the narrow index-vs-HEAD view it has always been."""
    out = await git.git_diff(staged=True)

    assert "+staged" in out
    assert "+unstaged" not in out


async def test_path_filter_still_scopes_the_diff(repo: Path) -> None:
    """The revision is inserted before ``--``, so pathspec filtering is unaffected."""
    out = await git.git_diff(path="added.txt")

    assert "added.txt" in out
    assert "tracked.txt" not in out


async def test_repository_without_a_commit_still_diffs(empty_repo: Path) -> None:
    """``git diff HEAD`` exits 128 before the first commit; fall back, do not crash."""
    (empty_repo / "first.txt").write_text("hello\n", encoding="utf-8", newline="\n")
    _git(empty_repo, "add", "-A")

    out = await git.git_diff()

    assert "first.txt" in out
    assert "+hello" in out


async def test_diff_revision_resolves_head_only_when_a_commit_exists(
    empty_repo: Path,
) -> None:
    """Unit-level pin on the branch the fallback hangs off."""
    assert await git._diff_revision(str(empty_repo)) is None

    (empty_repo / "first.txt").write_text("hello\n", encoding="utf-8", newline="\n")
    _git(empty_repo, "add", "-A")
    _git(empty_repo, "commit", "-qm", "init")

    assert await git._diff_revision(str(empty_repo)) == "HEAD"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, ("git", "diff", "HEAD")),
        ({"staged": True}, ("git", "diff", "--cached", "HEAD")),
        ({"path": "pkg/mod.py"}, ("git", "diff", "HEAD", "--", "pkg/mod.py")),
        (
            {"staged": True, "path": "pkg/mod.py"},
            ("git", "diff", "--cached", "HEAD", "--", "pkg/mod.py"),
        ),
    ],
)
async def test_fingerprint_argv_matches_the_executed_argv(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, object],
    expected: tuple[str, ...],
) -> None:
    """The approval surface must name the command that runs (#614).

    Asserted against the argv git is actually handed, not against a second copy
    of the expectation, so the fingerprint cannot drift away from the body again.
    """
    seen: list[tuple[str, ...]] = []

    class _Proc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, None]:
            return (b"", None)

    async def fake_exec(*argv: str, **kwargs: object) -> _Proc:
        seen.append(tuple(argv))
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await git.git_diff(**kwargs)  # type: ignore[arg-type]

    # The first call is the ``rev-parse`` probe, which the stub reports as
    # successful; the diff itself is the last one.
    assert seen[-1] == expected
    assert git._git_diff_argv(kwargs) == expected
