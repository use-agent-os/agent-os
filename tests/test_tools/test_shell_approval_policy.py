from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agentos.gateway.approval_queue import get_approval_queue, reset_approval_queue
from agentos.sandbox.config import SandboxSettings
from agentos.sandbox.governance import action_fingerprint
from agentos.sandbox.integration import configure_runtime, reset_runtime
from agentos.sandbox.intent_cache import get_intent_cache, reset_intent_cache
from agentos.tools.builtin import code_exec, filesystem, shell
from agentos.tools.builtin.code_exec import execute_code
from agentos.tools.builtin.shell_policy import PolicyResult
from agentos.tools.types import (
    CallerKind,
    InteractionMode,
    ToolContext,
    ToolError,
    UnsupportedSurfaceError,
    current_tool_context,
)


@pytest.fixture(autouse=True)
def reset_approval_state(monkeypatch: pytest.MonkeyPatch):
    reset_approval_queue()
    reset_intent_cache()
    reset_runtime()
    monkeypatch.setattr(shell, "_sandbox_effectively_off", lambda: True)
    elevate_token = shell._elevate_current_call.set(False)
    token = current_tool_context.set(
        ToolContext(caller_kind=CallerKind.CLI, session_key="agent:main:test")
    )
    yield
    current_tool_context.reset(token)
    shell._elevate_current_call.reset(elevate_token)
    reset_approval_queue()
    reset_intent_cache()
    reset_runtime()


@pytest.mark.asyncio
async def test_sandbox_off_forces_prompt_over_global_auto_approve() -> None:
    queue = get_approval_queue()
    queue.set_settings("auto-approve")

    result = await shell._check_exec_approval(
        "exec_command",
        "rm target.txt",
        None,
        "command requires approval",
        None,
        True,
    )

    assert result is not None
    assert result["status"] == "approval_required"
    assert len(queue.list_pending("exec")) == 1


@pytest.mark.asyncio
async def test_elevated_bypass_skips_prompt_even_when_sandbox_off() -> None:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.elevated = "bypass"

    result = await shell._check_exec_approval(
        "exec_command",
        "rm target.txt",
        None,
        "command requires approval",
        None,
        True,
    )

    assert result is None
    assert shell._elevate_current_call.get() is True


@pytest.mark.asyncio
async def test_sandbox_off_forces_prompt_over_cached_intent() -> None:
    get_intent_cache().record("rm target.txt")

    result = await shell._check_exec_approval(
        "exec_command",
        "rm target.txt",
        None,
        "command requires approval",
        None,
        True,
    )

    assert result is not None
    assert result["status"] == "approval_required"
    assert shell._elevate_current_call.get() is False


@pytest.mark.asyncio
async def test_elevated_full_remains_explicit_override_when_sandbox_off() -> None:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.elevated = "full"

    result = await shell._check_exec_approval(
        "exec_command",
        "rm target.txt",
        None,
        "command requires approval",
        None,
        True,
    )

    assert result is None
    assert shell._elevate_current_call.get() is True


def test_audit_command_preserves_long_commands_until_cap() -> None:
    command = "rm " + ("x" * 120)
    assert shell._audit_command(command) == command

    huge = "rm " + ("x" * 5000)
    audited = shell._audit_command(huge)
    assert len(audited) > 80
    assert audited.endswith("...[truncated]")


@pytest.mark.asyncio
async def test_unattended_warnlist_shell_fails_fast_without_pending_exec_approval() -> None:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    queue = get_approval_queue()

    with pytest.raises(UnsupportedSurfaceError):
        await shell._check_exec_approval(
            "exec_command",
            "rm target.txt",
            None,
            "command requires approval",
            None,
            True,
        )

    assert len(queue.list_pending("exec")) == 0


@pytest.mark.asyncio
async def test_unattended_auto_deny_preserves_policy_denial_without_pending_approval() -> None:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    queue = get_approval_queue()
    queue.set_settings("auto-deny")

    result = await shell._check_exec_approval(
        "exec_command",
        "rm target.txt",
        None,
        "command requires approval",
        None,
        True,
    )

    assert result is not None
    assert result["status"] == "approval_denied"
    assert len(queue.list_pending("exec")) == 0


@pytest.mark.asyncio
async def test_unattended_destructive_code_exec_fails_fast_without_pending_exec_approval() -> None:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    queue = get_approval_queue()

    with pytest.raises(UnsupportedSurfaceError):
        await execute_code("import os\nos.remove('target.txt')")

    assert len(queue.list_pending("exec")) == 0


@pytest.mark.asyncio
async def test_unattended_bypass_allows_warnlist_shell_without_pending_approval() -> None:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    queue = get_approval_queue()

    result = await shell._check_exec_approval(
        "exec_command",
        "rm target.txt",
        None,
        "command requires approval",
        None,
        True,
    )

    assert result is None
    assert shell._elevate_current_call.get() is True
    assert len(queue.list_pending("exec")) == 0


@pytest.mark.asyncio
async def test_unattended_bypass_allows_destructive_code_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(tmp_path)
    target = tmp_path / "target.txt"
    target.write_text("delete me", encoding="utf-8")
    configure_runtime(
        SandboxSettings(sandbox=False, security_grading=False, allow_legacy_mode=True),
        workspace=tmp_path,
    )
    monkeypatch.setattr(
        code_exec,
        "_resolve_python_bin",
        lambda *, sandbox_enabled: sys.executable,
    )

    result = await execute_code("import os\nos.remove('target.txt')")
    payload = json.loads(result)

    assert payload["exit_code"] == 0
    assert not target.exists()
    assert len(get_approval_queue().list_pending("exec")) == 0


@pytest.mark.asyncio
async def test_approved_destructive_code_exec_uses_host_grant_when_sandbox_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.workspace_dir = str(tmp_path)
    target = tmp_path / "target.txt"
    target.write_text("delete me", encoding="utf-8")
    configure_runtime(
        SandboxSettings(
            sandbox=True,
            security_grading=True,
            backend="noop",
            allow_legacy_mode=True,
        ),
        workspace=tmp_path,
    )
    monkeypatch.setattr(
        code_exec,
        "_resolve_python_bin",
        lambda *, sandbox_enabled: sys.executable,
    )

    async def fail_sandbox(*args: object, **kwargs: object) -> object:
        raise AssertionError("sandbox backend should not run after approval")

    monkeypatch.setattr(code_exec, "run_under_backend", fail_sandbox)

    code = "import os\nos.remove('target.txt')"
    pending = json.loads(await execute_code(code))
    assert pending["status"] == "approval_required"
    approval_id = str(pending["approval_id"])
    get_approval_queue().resolve(approval_id, approved=True)

    result = await execute_code(code, approval_id=approval_id)
    payload = json.loads(result)

    assert payload["exit_code"] == 0
    assert not target.exists()


@pytest.mark.asyncio
async def test_approved_background_process_uses_host_grant_when_sandbox_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.workspace_dir = str(tmp_path)
    target = tmp_path / "target.txt"
    target.write_text("delete me", encoding="utf-8")
    configure_runtime(
        SandboxSettings(
            sandbox=True,
            security_grading=True,
            backend="noop",
            allow_legacy_mode=True,
        ),
        workspace=tmp_path,
    )

    async def fail_sandbox(*args: object, **kwargs: object) -> object:
        raise AssertionError("sandbox background backend should not run after approval")

    monkeypatch.setattr(shell, "_spawn_sandboxed_background_process", fail_sandbox)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: PolicyResult(
            allowed=True,
            reason=f"command requires approval: {command}",
            needs_approval=True,
        ),
    )

    import os

    cmd = "del target.txt" if os.name == "nt" else "rm target.txt"
    pending = json.loads(await shell.background_process(cmd, workdir=str(tmp_path)))
    assert pending["status"] == "approval_required"
    approval_id = str(pending["approval_id"])
    get_approval_queue().resolve(approval_id, approved=True)

    result = await shell.background_process(
        cmd,
        workdir=str(tmp_path),
        timeout=5,
        approval_id=approval_id,
    )

    assert "status: running" in result
    session_id = result.splitlines()[0].split("=", 1)[1]
    session = shell._bg_sessions[session_id]
    assert session.collector_task is not None
    await session.collector_task
    assert not target.exists()


@pytest.mark.asyncio
async def test_unattended_bypass_allows_outside_workspace_write(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)

    write_file = filesystem.write_file.__wrapped__.__wrapped__  # type: ignore[attr-defined]
    result = await write_file(str(outside), "ok")

    assert result.startswith("Written 2 bytes to ")
    assert outside.read_text(encoding="utf-8") == "ok"
    assert len(get_approval_queue().list_pending("exec")) == 0


@pytest.mark.asyncio
async def test_workspace_lockdown_blocks_outside_workspace_write_even_with_bypass(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.workspace_lockdown = True  # type: ignore[attr-defined]

    write_file = filesystem.write_file.__wrapped__.__wrapped__  # type: ignore[attr-defined]
    with pytest.raises(ToolError, match="workspace lockdown"):
        await write_file(str(outside), "ok")

    assert not outside.exists()
    assert len(get_approval_queue().list_pending("exec")) == 0


@pytest.mark.asyncio
async def test_workspace_lockdown_allows_configured_scratch_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    target = scratch / "debug.py"
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.scratch_dir = str(scratch)  # type: ignore[attr-defined]
    ctx.workspace_lockdown = True  # type: ignore[attr-defined]

    write_file = filesystem.write_file.__wrapped__.__wrapped__  # type: ignore[attr-defined]
    result = await write_file(str(target), "print('ok')")

    assert result.startswith("Written 11 bytes to ")
    assert target.read_text(encoding="utf-8") == "print('ok')"


@pytest.mark.asyncio
async def test_workspace_write_deny_globs_block_file_write(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "blocked" / "generated.txt"
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.workspace_write_deny_globs = ["blocked/**"]  # type: ignore[attr-defined]

    write_file = filesystem.write_file.__wrapped__.__wrapped__  # type: ignore[attr-defined]
    with pytest.raises(ToolError, match="workspace write deny policy"):
        await write_file(str(target), "nope")

    assert not target.exists()
    assert len(get_approval_queue().list_pending("exec")) == 0


def test_tool_definitions_include_scratch_guidance_when_configured(tmp_path: Path) -> None:
    from agentos.tools.registry import get_default_registry

    scratch = tmp_path / "scratch"
    ctx = ToolContext(scratch_dir=str(scratch))

    tools = get_default_registry().to_tool_definitions(ctx)
    descriptions = {tool.name: tool.description for tool in tools}

    assert str(scratch) in descriptions["exec_command"]
    assert str(scratch) in descriptions["write_file"]


@pytest.mark.asyncio
async def test_workspace_lockdown_blocks_obvious_outside_shell_redirection(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.workspace_lockdown = True  # type: ignore[attr-defined]

    result = await shell._check_exec_approval(
        "exec_command",
        f"echo ok > {outside}",
        str(workspace),
        "command requires approval",
        None,
        False,
    )

    assert result is not None
    assert result["status"] == "blocked"
    assert result["reason"] == "workspace_lockdown"


@pytest.mark.parametrize(
    "command",
    [
        "echo malicious>/etc/passwd",
        "echo malicious>>/etc/passwd",
        "cmd 2>/etc/leak.txt",
        "cmd 1>>/etc/leak.txt",
        "cmd 10>/etc/leak.txt",
        ">&/etc/both.txt",
        "cmd &>/etc/both.txt",
        "cmd &>>/etc/both.txt",
        "cmd >|/etc/clobber.txt",
        "cat<input>/etc/out.txt",
        "cmd 2>&1 >/etc/out.txt",
    ],
)
def test_shell_write_targets_detects_redirection_without_whitespace(command: str) -> None:
    assert [target for target in shell._shell_write_targets(command) if target.startswith("/etc/")]


@pytest.mark.parametrize(
    "command",
    [
        "cmd 2>&1",
        "cmd >&2",
        "cmd 2>&-",
        "cmd 2>&1 | grep x",
        "ls -la",
        "grep -R needle src",
    ],
)
def test_shell_write_targets_ignores_descriptor_duplication(command: str) -> None:
    assert shell._shell_write_targets(command) == []


@pytest.mark.parametrize(
    "command",
    [
        "echo x|tee /etc/passwd",
        "echo x|tee -a /etc/passwd",
        "echo x;tee /etc/passwd",
        "echo x | tee /etc/passwd",
        "tee --append /etc/passwd",
        "tee --output-error=warn /etc/passwd",
        "tee -a -i /etc/passwd",
        "echo x | /usr/bin/tee /etc/passwd",
    ],
)
def test_shell_write_targets_detects_tee_without_whitespace_or_short_options(
    command: str,
) -> None:
    assert "/etc/passwd" in shell._shell_write_targets(command)


@pytest.mark.parametrize(
    "command",
    [
        "mytee /etc/passwd",
        "notee /etc/passwd",
        "committee /etc/passwd",
        "echo tee",
    ],
)
def test_shell_write_targets_ignores_words_ending_in_tee(command: str) -> None:
    assert shell._shell_write_targets(command) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "template",
    [
        "echo ok>{target}",
        "echo ok>>{target}",
        "echo ok 2>{target}",
        ">&{target}",
        "echo ok &>{target}",
        "cat<in>{target}",
    ],
)
async def test_workspace_lockdown_blocks_redirection_without_whitespace(
    tmp_path: Path,
    template: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.workspace_lockdown = True  # type: ignore[attr-defined]

    result = await shell._check_exec_approval(
        "exec_command",
        template.format(target=outside),
        str(workspace),
        "command requires approval",
        None,
        False,
    )

    assert result is not None
    assert result["status"] == "blocked"
    assert result["reason"] == "workspace_lockdown"
    assert result["resolved_path"] == str(outside)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "template",
    [
        "echo ok|tee {target}",
        "echo ok|tee -a {target}",
        "echo ok | tee --append {target}",
        "echo ok | tee --output-error=warn {target}",
    ],
)
async def test_workspace_lockdown_blocks_tee_without_whitespace_or_long_options(
    tmp_path: Path,
    template: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.workspace_lockdown = True  # type: ignore[attr-defined]

    result = await shell._check_exec_approval(
        "exec_command",
        template.format(target=outside),
        str(workspace),
        "command requires approval",
        None,
        False,
    )

    assert result is not None
    assert result["status"] == "blocked"
    assert result["reason"] == "workspace_lockdown"
    assert result["resolved_path"] == str(outside)


@pytest.mark.asyncio
async def test_workspace_write_deny_globs_block_tee_without_whitespace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.workspace_write_deny_globs = ["reports/*.txt"]  # type: ignore[attr-defined]

    result = await shell._check_exec_approval(
        "exec_command",
        "echo ok|tee --append reports/out.txt",
        str(workspace),
        "command requires approval",
        None,
        False,
    )

    assert result is not None
    assert result["status"] == "blocked"
    assert result["reason"] == "workspace_write_deny"
    assert result["matched_pattern"] == "reports/*.txt"


@pytest.mark.asyncio
async def test_workspace_lockdown_allows_tee_inside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.workspace_lockdown = True  # type: ignore[attr-defined]

    result = await shell._check_exec_approval(
        "exec_command",
        "echo ok|tee -a out.txt",
        str(workspace),
        "command requires approval",
        None,
        False,
    )

    assert result is None


@pytest.mark.asyncio
async def test_workspace_lockdown_allows_descriptor_duplication_inside_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.workspace_lockdown = True  # type: ignore[attr-defined]

    result = await shell._check_exec_approval(
        "exec_command",
        "echo ok>out.txt 2>&1",
        str(workspace),
        "command requires approval",
        None,
        False,
    )

    assert result is None


@pytest.mark.asyncio
async def test_workspace_write_deny_globs_block_shell_redirection(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.workspace_write_deny_globs = ["reports/*.txt"]  # type: ignore[attr-defined]

    result = await shell._check_exec_approval(
        "exec_command",
        "echo ok > reports/out.txt",
        str(workspace),
        "command requires approval",
        None,
        False,
    )

    assert result is not None
    assert result["status"] == "blocked"
    assert result["reason"] == "workspace_write_deny"


@pytest.mark.asyncio
async def test_workspace_write_deny_globs_block_redirection_without_whitespace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.workspace_write_deny_globs = ["reports/*.txt"]  # type: ignore[attr-defined]

    result = await shell._check_exec_approval(
        "exec_command",
        "echo ok>reports/out.txt",
        str(workspace),
        "command requires approval",
        None,
        False,
    )

    assert result is not None
    assert result["status"] == "blocked"
    assert result["reason"] == "workspace_write_deny"
    assert result["matched_pattern"] == "reports/*.txt"


@pytest.mark.asyncio
async def test_workspace_write_deny_globs_block_direct_shell_command(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "reports" / "out.txt"
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.workspace_write_deny_globs = ["reports/*.txt"]  # type: ignore[attr-defined]

    result = await shell.exec_command("echo ok > reports/out.txt", workdir=str(workspace))

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workspace_write_deny"
    assert not target.exists()


@pytest.mark.asyncio
async def test_bypass_still_blocks_sensitive_shell_targets() -> None:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.elevated = "bypass"

    result = await shell._check_exec_approval(
        "exec_command",
        "rm ~/.ssh/id_rsa",
        None,
        "command requires approval",
        None,
        True,
    )

    assert result is not None
    assert result["status"] == "blocked"
    assert result["reason"] == "sensitive_path"
    assert shell._elevate_current_call.get() is False


@pytest.mark.asyncio
async def test_elevated_bypass_cron_runs_a_warned_command_without_a_human() -> None:
    """The whole point of the cron opt-in: no approver, so no approval step."""
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.caller_kind = CallerKind.CRON
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.session_key = "cron:lp-check:run:1"
    ctx.elevated = "bypass"
    queue = get_approval_queue()

    result = await shell._check_exec_approval(
        "exec_command",
        "rm target.txt",
        None,
        "command requires approval",
        None,
        True,
    )

    assert result is None
    assert len(queue.list_pending("exec")) == 0


@pytest.mark.asyncio
async def test_elevated_bypass_cron_still_blocks_a_sensitive_path() -> None:
    """bypass is not full: the sensitive-path floor survives the opt-in.

    Note the scope — sensitive_target_in_command only inspects *destructive*
    intents, so this floor stops an elevated job clobbering ~/.ssh, not one
    reading it. Reads of secrets are not contained once exec_command is on.
    """
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.caller_kind = CallerKind.CRON
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.session_key = "cron:lp-check:run:1"
    ctx.elevated = "bypass"

    result = await shell._check_exec_approval(
        "exec_command",
        "rm ~/.ssh/id_rsa",
        None,
        "command requires approval",
        None,
        True,
    )

    assert result is not None
    assert result["status"] == "blocked"
    assert result["reason"] == "sensitive_path"


@pytest.mark.asyncio
async def test_unelevated_cron_still_fails_fast_on_a_warned_command() -> None:
    """Without the opt-in, a cron turn cannot silently park on an approval
    queue nobody is watching — it dies immediately instead."""
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.caller_kind = CallerKind.CRON
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.session_key = "cron:lp-check:run:1"
    queue = get_approval_queue()

    with pytest.raises(UnsupportedSurfaceError):
        await shell._check_exec_approval(
            "exec_command",
            "rm target.txt",
            None,
            "command requires approval",
            None,
            True,
        )

    assert len(queue.list_pending("exec")) == 0


@pytest.mark.asyncio
async def test_bypass_does_not_override_safe_bin_hard_denies() -> None:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.elevated = "bypass"

    with pytest.raises(ToolError, match="command blocked by policy"):
        await shell.exec_command("Clear-Disk")


@pytest.mark.asyncio
async def test_root_wipe_is_hard_blocked_at_the_exec_approval_boundary() -> None:
    """Issue #563 at the real entry point: ``rm -rf /`` must never reach the
    approval prompt, and ``/elevated bypass`` must not lift the floor."""
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.elevated = "bypass"

    for command in ("rm -rf /", "rm -rf /*", "rm /tmp/ok && rm -rf /"):
        result = await shell._check_exec_approval(
            "exec_command",
            command,
            None,
            "command requires approval",
            None,
            True,
        )

        assert result is not None, command
        assert result["status"] == "blocked"
        assert result["reason"] == "sensitive_path"
        assert result["sensitive_path"] == "/"


# ─── Issue #1510: `_sandbox_request_for` denial-ledger cwd accuracy ───────
#
# `_sandbox_request_for` is only ever called by `_record_shell_denial`, right
# after a human has rejected a warned command, to build the request that gets
# fingerprinted into the sandbox denial ledger (§8.3/§8.5).


def test_sandbox_request_for_resolves_relative_workdir_against_ctx_workspace(
    tmp_path: Path,
) -> None:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.workspace_dir = str(tmp_path)
    configure_runtime(
        SandboxSettings(
            sandbox=True, security_grading=False, backend="noop", allow_legacy_mode=True
        ),
        workspace=tmp_path,
    )

    built = shell._sandbox_request_for("exec_command", "rm -rf build", "subproject")
    assert built is not None
    request, _, _ = built
    assert request.cwd == (tmp_path / "subproject").resolve()


def test_sandbox_request_for_distinguishes_denials_in_different_relative_workdirs(
    tmp_path: Path,
) -> None:
    """Regression for #1510: before the fix, every relative ``workdir`` was
    silently dropped in favour of the workspace root, so denying the same
    command from two different subdirectories fingerprinted identically and
    collapsed into one counter in the §8.5 denial ledger."""
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.workspace_dir = str(tmp_path)
    configure_runtime(
        SandboxSettings(
            sandbox=True, security_grading=False, backend="noop", allow_legacy_mode=True
        ),
        workspace=tmp_path,
    )

    built_a = shell._sandbox_request_for("exec_command", "rm -rf build", "subproject")
    built_b = shell._sandbox_request_for("exec_command", "rm -rf build", "other")
    built_root = shell._sandbox_request_for("exec_command", "rm -rf build", None)
    assert built_a is not None
    assert built_b is not None
    assert built_root is not None

    fingerprint_a = action_fingerprint(built_a[0])
    fingerprint_b = action_fingerprint(built_b[0])
    fingerprint_root = action_fingerprint(built_root[0])
    assert len({fingerprint_a, fingerprint_b, fingerprint_root}) == 3


@pytest.mark.asyncio
async def test_exec_command_records_denial_against_resolved_workdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The call site matters too: exec_command already has the resolved
    ``cwd`` in hand from ``_effective_workdir`` a few lines earlier, so it
    should hand that to ``_record_shell_denial`` instead of the raw,
    possibly-relative ``workdir`` argument."""
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.workspace_dir = str(tmp_path)

    recorded: dict[str, object] = {}

    async def _capture_denial(tool_name: str, command: str, workdir, reason) -> None:
        recorded["workdir"] = workdir

    monkeypatch.setattr(shell, "_record_shell_denial", _capture_denial)

    pending = json.loads(await shell.exec_command("rm target.txt", workdir="subproject"))
    approval_id = str(pending["approval_id"])
    get_approval_queue().resolve(approval_id, approved=False)

    await shell.exec_command("rm target.txt", workdir="subproject", approval_id=approval_id)

    assert recorded["workdir"] == str((tmp_path / "subproject").resolve())
