from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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


def test_git_diff_argv_unstaged_without_path() -> None:
    assert git._git_diff_argv({}) == ("git", "diff")
    assert git._git_diff_argv({"staged": False, "path": None}) == ("git", "diff")


def test_git_diff_argv_staged_without_path() -> None:
    assert git._git_diff_argv({"staged": True}) == ("git", "diff", "--cached")
    assert git._git_diff_argv({"staged": True, "path": None}) == ("git", "diff", "--cached")


def test_git_diff_argv_unstaged_with_path() -> None:
    assert git._git_diff_argv({"path": "src/main.py"}) == (
        "git",
        "diff",
        "--",
        "src/main.py",
    )


def test_git_diff_argv_staged_with_path() -> None:
    assert git._git_diff_argv({"staged": True, "path": "src/main.py"}) == (
        "git",
        "diff",
        "--cached",
        "--",
        "src/main.py",
    )


@pytest.mark.asyncio
async def test_git_commit_empty_files_skips_git_add(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded_commands: list[tuple[tuple[str, ...], str | None]] = []

    async def _mock_run_git(*args: str, cwd: str | None = None) -> str:
        recorded_commands.append((args, cwd))
        return "ok"

    monkeypatch.setattr(git, "_run_git", _mock_run_git)
    raw_git_commit = git.git_commit.__wrapped__.__wrapped__
    result = await raw_git_commit(message="commit only staged", files=[], workdir="/repo")
    assert result == "ok"
    assert recorded_commands == [(("commit", "-m", "commit only staged"), "/repo")]


@pytest.mark.asyncio
async def test_git_commit_omitted_files_runs_git_add_all(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded_commands: list[tuple[tuple[str, ...], str | None]] = []

    async def _mock_run_git(*args: str, cwd: str | None = None) -> str:
        recorded_commands.append((args, cwd))
        return "ok"

    monkeypatch.setattr(git, "_run_git", _mock_run_git)
    raw_git_commit = git.git_commit.__wrapped__.__wrapped__
    result = await raw_git_commit(message="stage all", files=None, workdir="/repo")
    assert result == "ok"
    assert recorded_commands == [
        (("add", "-A"), "/repo"),
        (("commit", "-m", "stage all"), "/repo"),
    ]


@pytest.mark.asyncio
async def test_git_commit_specific_files_stages_only_specified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_commands: list[tuple[tuple[str, ...], str | None]] = []

    async def _mock_run_git(*args: str, cwd: str | None = None) -> str:
        recorded_commands.append((args, cwd))
        return "ok"

    monkeypatch.setattr(git, "_run_git", _mock_run_git)
    monkeypatch.setattr(git, "_reject_foreign_git_path", lambda p: None)
    raw_git_commit = git.git_commit.__wrapped__.__wrapped__
    result = await raw_git_commit(
        message="stage some", files=["file1.py", "file2.py"], workdir="/repo"
    )
    assert result == "ok"
    assert recorded_commands == [
        (("add", "--", "file1.py", "file2.py"), "/repo"),
        (("commit", "-m", "stage some"), "/repo"),
    ]

