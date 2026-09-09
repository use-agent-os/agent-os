"""Regression tests for the ``_resolve_workspace`` relative-cwd bug.

``gate_action`` resolves the caller's ``cwd`` via ``_resolve_workspace``,
which used to accept only an already-absolute path and silently discard
anything relative — falling back to ``ctx.workspace_dir`` (the workspace
root) instead of joining the relative path onto it.

That resolved workspace feeds ``action_fingerprint`` (via
``SandboxRequest.cwd``), and ``gate_execution`` consults the *previous*
denial's fingerprint through ``post_denial_guard`` on every call —
regardless of whether the new call even requires approval. So two calls
that differ only by which relative subdirectory they target, and whose
tool-specific ``argv_factory`` doesn't otherwise encode the directory
(as `git.py`'s tools deliberately don't, to keep ``git status`` in two
repos from looking like different intents by path alone), used to
collapse onto the *same* fingerprint. Denying one would then silently
auto-deny the other as a "repeated" request it never was.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos.sandbox.config import SandboxSettings
from agentos.sandbox.integration import configure_runtime, gate_action, reset_runtime
from agentos.sandbox.policy import LevelHints
from agentos.sandbox.types import DenialReason, DenialResult
from agentos.tools.types import CallerKind, ToolContext, current_tool_context


class _FakeApprovalQueue:
    """Minimal ``_ApprovalQueueLike`` fake: replays a fixed approve/deny
    sequence, one entry per ``wait`` call."""

    def __init__(self, decisions: list[bool]) -> None:
        self._decisions = decisions
        self.calls = 0

    def request(self, namespace: str = "exec", params: dict | None = None) -> str:
        return f"approval-{self.calls}"

    async def wait(self, approval_id: str, timeout: float | None = None) -> bool:
        decision = self._decisions[self.calls]
        self.calls += 1
        return decision

    def resolve(self, approval_id: str, approved: bool) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset():
    reset_runtime()
    yield
    reset_runtime()


def _configure_graded_runtime(workspace: Path, queue: _FakeApprovalQueue) -> None:
    configure_runtime(
        SandboxSettings(
            sandbox=True, security_grading=True, backend="noop", allow_legacy_mode=True
        ),
        workspace=workspace,
        approval_queue=queue,
    )


@pytest.mark.asyncio
async def test_denial_in_one_relative_subdir_does_not_block_a_different_one(
    tmp_path: Path,
) -> None:
    (tmp_path / "repoA").mkdir()
    (tmp_path / "repoB").mkdir()
    token = current_tool_context.set(
        ToolContext(caller_kind=CallerKind.CLI, session_key="s1", workspace_dir=str(tmp_path))
    )
    try:
        # First call is denied by the human; second (unrelated) call is
        # approved — proving it actually reached the approval gate instead
        # of being blind-blocked by the post-denial guard.
        queue = _FakeApprovalQueue([False, True])
        _configure_graded_runtime(tmp_path, queue)
        hints = LevelHints(trusted_source=False)  # forces require_approval=True

        first, _, _ = await gate_action(
            action_kind="test.action",
            argv=("git", "status"),
            cwd=Path("repoA"),
            hints=hints,
            session_id="s1",
        )
        assert isinstance(first, DenialResult)
        assert first.reason == DenialReason.HUMAN_REJECTED

        second, _, _ = await gate_action(
            action_kind="test.action",
            argv=("git", "status"),
            cwd=Path("repoB"),
            hints=hints,
            session_id="s1",
        )
        assert not isinstance(second, DenialResult), (
            f"repoB was blocked as a repeat of repoA's denial: {second}"
        )
        # The queue was actually consulted a second time — not short-circuited.
        assert queue.calls == 2
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_repeating_the_same_relative_subdir_still_triggers_the_guard(
    tmp_path: Path,
) -> None:
    """Sanity check the other direction: the post-denial guard must still
    fire for a genuine blind retry in the *same* directory."""
    (tmp_path / "repoA").mkdir()
    token = current_tool_context.set(
        ToolContext(caller_kind=CallerKind.CLI, session_key="s1", workspace_dir=str(tmp_path))
    )
    try:
        queue = _FakeApprovalQueue([False, True])
        _configure_graded_runtime(tmp_path, queue)
        hints = LevelHints(trusted_source=False)

        first, _, _ = await gate_action(
            action_kind="test.action",
            argv=("git", "status"),
            cwd=Path("repoA"),
            hints=hints,
            session_id="s1",
        )
        assert isinstance(first, DenialResult)

        second, _, _ = await gate_action(
            action_kind="test.action",
            argv=("git", "status"),
            cwd=Path("repoA"),
            hints=hints,
            session_id="s1",
        )
        assert isinstance(second, DenialResult)
        assert second.reason == DenialReason.REPEATED_SAME_INTENT
        # Blocked by the guard before ever re-consulting the queue.
        assert queue.calls == 1
    finally:
        current_tool_context.reset(token)
