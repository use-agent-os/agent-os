from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.tools.builtin import git
from agentos.tools.types import ToolContext, ToolError, current_tool_context


def test_git_effective_workdir_resolves_context_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    try:
        assert git._effective_workdir(None) == str(workspace.resolve())
    finally:
        current_tool_context.reset(token)


def test_git_effective_workdir_joins_relative_path_onto_workspace(
    tmp_path: Path,
) -> None:
    """A relative workdir resolves against the workspace, not the process CWD.

    Regression for #1566: returning the raw string made ``_run_git`` resolve it
    against the process CWD, so ``git_status(workdir="sub")`` inspected
    ``$PWD/sub`` — possibly a different repository — whenever the gateway ran
    from anywhere but the workspace.
    """
    workspace = tmp_path / "workspace"
    subproject = workspace / "my-subproject"
    subproject.mkdir(parents=True)

    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    try:
        assert git._effective_workdir("my-subproject") == str(subproject.resolve())
    finally:
        current_tool_context.reset(token)


def test_git_effective_workdir_keeps_absolute_path_outside_workspace(
    tmp_path: Path,
) -> None:
    """An absolute workdir is resolved as-is, not re-anchored to the workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()

    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    try:
        assert git._effective_workdir(str(other)) == str(other.resolve())
    finally:
        current_tool_context.reset(token)


def test_git_effective_workdir_dotdot_stays_inside_resolved_workspace(
    tmp_path: Path,
) -> None:
    """Lexical traversal is resolved, keeping the containment check honest."""
    workspace = tmp_path / "workspace"
    (workspace / "nested").mkdir(parents=True)

    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    try:
        assert git._effective_workdir("nested/../nested") == str((workspace / "nested").resolve())
    finally:
        current_tool_context.reset(token)


def test_git_effective_workdir_rejects_foreign_posix_absolute_path_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(git, "os", SimpleNamespace(name="nt"), raising=False)
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    try:
        with pytest.raises(ToolError, match="foreign_host_path"):
            git._effective_workdir("/Users/a1/Desktop/repo")
    finally:
        current_tool_context.reset(token)


def test_git_rejects_foreign_diff_path_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(git, "os", SimpleNamespace(name="nt"), raising=False)

    with pytest.raises(ToolError, match="foreign_host_path"):
        git._reject_foreign_git_path("/Users/a1/Desktop/repo/file.py")


def test_git_rejects_foreign_commit_file_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(git, "os", SimpleNamespace(name="nt"), raising=False)

    with pytest.raises(ToolError, match="foreign_host_path"):
        git._reject_foreign_git_path("/Users/a1/Desktop/repo/file.py")


def test_git_diff_argv_default_without_path() -> None:
    """The default fingerprint carries ``HEAD`` now that the body diffs against it.

    Updated with #1963: the body stopped running bare ``git diff``, and #614's
    rule is that this tuple mirrors the body. The ``path: None`` spelling stays
    pinned because that is the input that used to stringify to a literal
    ``"None"``.
    """
    assert git._git_diff_argv({}) == ("git", "diff", "HEAD")
    assert git._git_diff_argv({"staged": False, "path": None}) == ("git", "diff", "HEAD")


def test_git_diff_argv_staged_without_path() -> None:
    assert git._git_diff_argv({"staged": True}) == ("git", "diff", "--cached", "HEAD")
    assert git._git_diff_argv({"staged": True, "path": None}) == (
        "git",
        "diff",
        "--cached",
        "HEAD",
    )


def test_git_diff_argv_default_with_path() -> None:
    assert git._git_diff_argv({"path": "src/main.py"}) == (
        "git",
        "diff",
        "HEAD",
        "--",
        "src/main.py",
    )


def test_git_diff_argv_staged_with_path() -> None:
    assert git._git_diff_argv({"staged": True, "path": "src/main.py"}) == (
        "git",
        "diff",
        "--cached",
        "HEAD",
        "--",
        "src/main.py",
    )


def test_git_diff_argv_never_emits_the_invalid_unstaged_flag() -> None:
    """#614's actual defect: ``--unstaged`` is not a git flag, and never was."""
    for args in ({}, {"staged": False}, {"staged": True}, {"path": "x"}):
        assert "--unstaged" not in git._git_diff_argv(args)
        assert "None" not in git._git_diff_argv(args)


def _git_commit_impl() -> Any:
    """``git_commit`` with the ``@tool``/``@sandboxed`` wrappers peeled off.

    The staging decision under test lives in the plain coroutine; the sandbox
    wrapper denies fail-closed when no runtime is configured.
    """
    return git.git_commit.__wrapped__.__wrapped__  # type: ignore[attr-defined]


class _RecordingRunGit:
    """Stand-in for ``git._run_git`` that records argv instead of running git."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, *args: str, cwd: str | None = None) -> str:
        self.calls.append(args)
        return ""


async def test_git_commit_with_empty_files_stages_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``files=[]`` is an explicit "stage nothing", not "stage everything".

    Regression for the ``if files:`` fall-through that ran ``git add -A`` and
    swept untracked files the caller never named into the commit.
    """
    recorder = _RecordingRunGit()
    monkeypatch.setattr(git, "_run_git", recorder)

    await _git_commit_impl()(message="commit staged", files=[])

    assert recorder.calls == [("commit", "-m", "commit staged")]


async def test_git_commit_without_files_stages_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting ``files`` keeps the documented ``git add -A`` behavior."""
    recorder = _RecordingRunGit()
    monkeypatch.setattr(git, "_run_git", recorder)

    await _git_commit_impl()(message="commit all")

    assert recorder.calls == [("add", "-A"), ("commit", "-m", "commit all")]


async def test_git_commit_with_named_files_stages_only_those(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _RecordingRunGit()
    monkeypatch.setattr(git, "_run_git", recorder)

    await _git_commit_impl()(message="commit one", files=["file1.txt"])

    assert recorder.calls == [
        ("add", "--", "file1.txt"),
        ("commit", "-m", "commit one"),
    ]
