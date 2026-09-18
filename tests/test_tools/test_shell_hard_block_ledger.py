"""Shell-layer hard blocks must reach the sandbox denial ledger (issue #1513).

Only a denied *approval* used to be recorded. The four unconditional security
blocks — denylisted binary, sensitive path, workspace lockdown, workspace write
deny — returned or raised without touching the ledger, so the most severe
violations were the ones missing from the audit trail and from the §8.5
threshold that pauses a runaway agent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentos.sandbox.config import SandboxSettings
from agentos.sandbox.integration import configure_runtime, get_runtime, reset_runtime
from agentos.sandbox.types import DenialReason
from agentos.tools.builtin import shell
from agentos.tools.types import CallerKind, ToolContext, ToolError, current_tool_context

SESSION_KEY = "agent:main:test"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "workspace").mkdir()
    return tmp_path / "workspace"


@pytest.fixture
def ctx(workspace: Path):
    reset_runtime()
    configure_runtime(
        SandboxSettings(sandbox=True, backend="noop", security_grading=False),
        workspace=workspace,
    )
    context = ToolContext(
        caller_kind=CallerKind.CLI,
        session_key=SESSION_KEY,
        workspace_dir=str(workspace),
    )
    token = current_tool_context.set(context)
    yield context
    current_tool_context.reset(token)
    reset_runtime()


async def _denials(tool_name: str, command: str, workdir: str | None = None) -> int:
    """Per-fingerprint denial count — the audit signal a hard block writes."""
    from agentos.sandbox.integration import action_fingerprint

    runtime = get_runtime()
    assert runtime is not None
    built = shell._sandbox_request_for(tool_name, command, workdir)
    assert built is not None
    return await runtime.ledger.count(SESSION_KEY, action_fingerprint(built[0]))


@pytest.mark.asyncio
async def test_denylisted_binary_is_recorded(ctx: ToolContext) -> None:
    with pytest.raises(ToolError):
        await shell.exec_command("Format-Volume -DriveLetter C")

    assert await _denials("exec_command", "Format-Volume -DriveLetter C") == 1


@pytest.mark.asyncio
async def test_sensitive_path_block_is_recorded(ctx: ToolContext) -> None:
    raw = await shell.exec_command("cat ~/.ssh/id_rsa")

    assert json.loads(raw)["status"] == "blocked"
    assert await _denials("exec_command", "cat ~/.ssh/id_rsa") == 1


@pytest.mark.asyncio
async def test_workspace_lockdown_block_is_recorded(ctx: ToolContext, tmp_path: Path) -> None:
    ctx.workspace_lockdown = True
    outside = tmp_path / "outside.txt"

    command = f'echo hi > "{outside}"'
    payload = json.loads(await shell.exec_command(command))

    assert payload["reason"] == "workspace_lockdown"
    assert await _denials("exec_command", command) == 1


@pytest.mark.asyncio
async def test_workspace_write_deny_block_is_recorded(ctx: ToolContext, workspace: Path) -> None:
    ctx.workspace_write_deny_globs = ["*.env"]

    command = 'echo hi > "%s"' % (workspace / "secrets.env")
    payload = json.loads(await shell.exec_command(command))

    assert payload["status"] == "blocked"
    assert await _denials("exec_command", command) == 1


@pytest.mark.asyncio
async def test_background_process_denylist_is_recorded(ctx: ToolContext) -> None:
    with pytest.raises(ToolError):
        await shell.background_process("Format-Volume -DriveLetter C")

    assert await _denials("background_process", "Format-Volume -DriveLetter C") == 1


@pytest.mark.asyncio
async def test_background_process_sensitive_path_is_recorded(ctx: ToolContext) -> None:
    raw = await shell.background_process("cat ~/.ssh/id_rsa")

    assert json.loads(raw)["status"] == "blocked"
    assert await _denials("background_process", "cat ~/.ssh/id_rsa") == 1


@pytest.mark.asyncio
async def test_allowed_command_records_nothing(ctx: ToolContext) -> None:
    await shell.exec_command("echo ok")

    assert await _denials("exec_command", "echo ok") == 0


@pytest.mark.asyncio
async def test_hard_blocks_do_not_pause_the_session(ctx: ToolContext, workspace: Path) -> None:
    """Audit records must not trip the §8.5 pause, which is permanent and global.

    The default threshold is three and the sensitive-path check is a text scan,
    so counting these would let three ordinary refusals — a `grep` for a key
    name is enough — deny every @sandboxed tool for the life of the session.
    """
    from agentos.tools.builtin import filesystem

    for command in ("cat ~/.ssh/id_rsa", "grep -rn id_rsa .", "ls -la ~/.ssh/id_rsa"):
        assert json.loads(await shell.exec_command(command))["status"] == "blocked"

    runtime = get_runtime()
    assert runtime is not None
    assert await runtime.ledger.is_paused(SESSION_KEY) is False
    assert await runtime.ledger.threshold_reached(SESSION_KEY) is False

    # An ordinary shell command and an unrelated sandboxed tool both still run.
    assert "denied" not in await shell.exec_command("echo ok")
    written = await filesystem.write_file(str(workspace / "a.txt"), "hi")
    assert "threshold_exceeded" not in written


@pytest.mark.asyncio
async def test_hard_blocks_do_not_clobber_the_repeat_guard(ctx: ToolContext) -> None:
    """§8.4 reads back last_denial; an audit record must leave it untouched."""
    runtime = get_runtime()
    assert runtime is not None
    await runtime.ledger.record_denial(SESSION_KEY, "gate-fingerprint", DenialReason.HUMAN_REJECTED)

    await shell.exec_command("cat ~/.ssh/id_rsa")

    assert await runtime.ledger.last_denial(SESSION_KEY) == (
        "gate-fingerprint",
        DenialReason.HUMAN_REJECTED,
    )
