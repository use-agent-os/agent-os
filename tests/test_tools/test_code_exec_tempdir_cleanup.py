"""Regression test for code_exec tempdir leak on denied sandbox calls.

Before the fix, every denied call to ``code_exec.execute_code`` left a
``/tmp/agentos_exec_*`` directory on disk because early returns inside
the sandbox block (DenialResult from gate_action, escalation denial,
subprocess timeout, exception) bypassed the ``finally`` clause attached
to the outer non-sandbox path.
"""

from __future__ import annotations

import glob
import shutil

import pytest

from agentos.tools.builtin import code_exec
from agentos.tools.types import current_tool_context


@pytest.fixture(autouse=True)
def _reset_tool_context() -> None:
    """Reset global tool context before and after each test to prevent pollution."""
    current_tool_context.set(None)
    yield
    current_tool_context.set(None)


def _cleanup_agentos_tempdirs() -> list[str]:
    """Remove any pre-existing agentos_exec tempdirs and return what was there."""
    existing = glob.glob("/tmp/agentos_exec_*")
    for d in existing:
        shutil.rmtree(d, ignore_errors=True)
    return existing


def test_denied_code_exec_call_does_not_leak_tempdir(monkeypatch: pytest.MonkeyPatch) -> None:
    """A denied sandbox call must clean up its ephemeral workdir.

    Each call with no runtime configured creates ``/tmp/agentos_exec_<rand>``
    and must remove it before returning, even on the deny path.
    """
    _cleanup_agentos_tempdirs()
    current_tool_context.set(None)

    async def run() -> None:
        result = await code_exec.execute_code("print('x')")
        # Runtime unconfigured → denial JSON.  We don't care about the content,
        # only that the tempdir is gone.
        assert "denied" in result or "unconfigured" in result

    import asyncio

    asyncio.run(run())

    leaked = glob.glob("/tmp/agentos_exec_*")
    assert leaked == [], f"leaked tempdirs after denied call: {leaked}"


def test_multiple_denied_calls_do_not_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """Five denied calls in a row must leave zero tempdirs behind."""
    _cleanup_agentos_tempdirs()
    current_tool_context.set(None)

    import asyncio

    async def run() -> None:
        for _ in range(5):
            result = await code_exec.execute_code("print('x')")
            assert "denied" in result or "unconfigured" in result

    asyncio.run(run())

    leaked = glob.glob("/tmp/agentos_exec_*")
    assert leaked == [], f"leaked {len(leaked)} tempdirs after 5 denied calls: {leaked}"


def test_workspace_path_does_not_create_tempdir(monkeypatch: pytest.MonkeyPatch) -> None:
    """When a workspace is configured, no ``/tmp/agentos_exec_*`` tempdir is created."""
    _cleanup_agentos_tempdirs()
    # Use a real workspace via ToolContext so we hit the "workspace is not None" branch.
    # Use a real workspace via ToolContext so we hit the "workspace is not None" branch.
    from agentos.tools.types import ToolContext

    ctx = ToolContext(workspace_dir="/tmp/agentos_test_workspace")
    current_tool_context.set(ctx)

    import asyncio

    async def run() -> None:
        result = await code_exec.execute_code("print('x')")
        # Even if denied, the workspace path is reused — no tempdir created.
        assert "denied" in result or "unconfigured" in result

    asyncio.run(run())

    leaked = glob.glob("/tmp/agentos_exec_*")
    assert leaked == [], f"workspace-path call still created tempdir: {leaked}"
