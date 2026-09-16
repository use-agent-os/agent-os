"""``git_diff``'s description has to match the argv it actually runs (issue #2321).

The tool advertised "staged + unstaged changes", but the handler appends
``--cached`` only when ``staged=true`` and ``git diff`` on its own reports the
working tree against the index. So the default mode never showed the staged
half, and a model that trusted the description read a diff it believed was
complete — staged work simply missing, with nothing in the output saying so.

These cover the description and both modes' argv together: the description is
only true for as long as the argv stays the way it is.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos.sandbox.config import SandboxSettings
from agentos.sandbox.integration import configure_runtime, reset_runtime
from agentos.tools.builtin import git
from agentos.tools.registry import get_default_registry
from agentos.tools.types import CallerKind, ToolContext, current_tool_context


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "workspace").mkdir()
    return tmp_path / "workspace"


@pytest.fixture
def git_calls(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Record the argv ``git_diff`` hands to git, without running git."""
    calls: list[tuple[str, ...]] = []

    async def fake_run_git(*args: str, cwd: str | None = None) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(git, "_run_git", fake_run_git)
    reset_runtime()
    configure_runtime(
        SandboxSettings(sandbox=True, backend="noop", security_grading=False),
        workspace=workspace,
    )
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            session_key="agent:main:test",
            workspace_dir=str(workspace),
        )
    )
    yield calls
    current_tool_context.reset(token)
    reset_runtime()


def _description() -> str:
    registered = get_default_registry().get("git_diff")
    assert registered is not None
    return registered.spec.description


def test_git_diff_description_names_the_default_mode() -> None:
    """The model picks the mode from this string; it has to name the default."""
    description = _description()

    assert "Unstaged changes by default" in description
    assert "staged=true" in description


def test_git_diff_description_no_longer_claims_one_call_shows_both() -> None:
    """Regression for #2321: neither mode returns staged and unstaged together."""
    description = _description()

    assert "staged + unstaged" not in description
    assert "Neither mode shows both." in description


def test_git_diff_description_names_the_parameter_not_a_cli_flag() -> None:
    """``--cached`` is not callable from the tool surface -- ``staged`` is.

    The schema exposes a boolean; a description that names the git flag sends
    the model looking for an argument that does not exist.
    """
    description = _description()

    assert "--cached" not in description


@pytest.mark.asyncio
async def test_git_diff_default_runs_plain_git_diff(git_calls: list[tuple[str, ...]]) -> None:
    await git.git_diff()

    assert git_calls == [("diff",)]


@pytest.mark.asyncio
async def test_git_diff_staged_runs_only_the_cached_diff(git_calls: list[tuple[str, ...]]) -> None:
    await git.git_diff(staged=True)

    assert git_calls == [("diff", "--cached")]


@pytest.mark.asyncio
async def test_git_diff_keeps_each_mode_when_scoped_to_a_path(
    git_calls: list[tuple[str, ...]],
) -> None:
    """A path filter must not quietly change which half of the tree is read."""
    await git.git_diff(path="a.txt")
    await git.git_diff(path="a.txt", staged=True)

    assert git_calls == [("diff", "--", "a.txt"), ("diff", "--cached", "--", "a.txt")]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({}, ("git", "diff")),
        ({"staged": False}, ("git", "diff")),
        ({"staged": True}, ("git", "diff", "--cached")),
        ({"path": "a.txt"}, ("git", "diff", "--", "a.txt")),
        ({"path": "a.txt", "staged": True}, ("git", "diff", "--cached", "--", "a.txt")),
    ],
)
def test_sandbox_preflight_argv_matches_the_documented_modes(
    arguments: dict[str, object], expected: tuple[str, ...]
) -> None:
    """The sandbox judges the call from this argv, so it documents the modes too.

    Green before and after the description fix -- it is here so the two
    descriptions of the same behaviour, the prose and the argv, cannot drift
    apart again without a test going red.
    """
    assert git._git_diff_argv(arguments) == expected
