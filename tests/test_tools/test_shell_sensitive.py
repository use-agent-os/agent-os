from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentos.tools.builtin import shell
from agentos.tools.types import ToolContext, ToolError, current_tool_context


@pytest.mark.asyncio
async def test_exec_command_blocks_sensitive_workdir(tmp_path: Path) -> None:
    sensitive_dir = tmp_path / ".env"

    result = await shell.exec_command("echo ok", workdir=str(sensitive_dir))

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "sensitive_path"
    assert payload["tool"] == "exec_command"
    assert ".env" in payload["command"]


@pytest.mark.asyncio
async def test_exec_command_blocks_nested_sensitive_workdir() -> None:
    sensitive_dir = Path.home() / ".ssh" / "id_rsa"

    result = await shell.exec_command("echo ok", workdir=str(sensitive_dir))

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["sensitive_path"] == "~/.ssh"


@pytest.mark.asyncio
async def test_exec_command_spaced_home_reports_directory_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #1232 — the workdir used to be glued into the command text and
    re-tokenized, so a home directory containing spaces split the path at the
    space and mis-reported the ``/id_rsa`` file-suffix marker instead of the
    ``~/.ssh`` directory prefix. The workdir is now checked structurally."""
    spaced_home = tmp_path / "John Doe"
    monkeypatch.setattr(Path, "home", lambda: spaced_home)
    sensitive_dir = spaced_home / ".ssh" / "id_rsa"

    result = await shell.exec_command("echo ok", workdir=str(sensitive_dir))

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["sensitive_path"] == "~/.ssh"


def test_tokenize_text_preserves_backslashes_on_windows_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #1232 — POSIX shlex.split() eats backslashes, so a Windows path
    could never survive tokenization as a matchable token. Windows hosts must
    tokenize non-POSIX."""
    from agentos.sandbox.sensitive_paths import _tokenize_text

    windows_path = "C:\\Users\\John\\.ssh\\id_rsa"
    monkeypatch.setattr(os, "name", "nt")
    assert windows_path in _tokenize_text(f"type {windows_path}")


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="/dev/null redirection is POSIX-specific")
async def test_exec_command_allows_dev_null_redirection() -> None:
    result = await shell.exec_command("printf ok 2>/dev/null")

    assert result == "exit_code=0\nok"


def test_dev_null_redirection_does_not_hide_sensitive_operand() -> None:
    payload = shell._sensitive_shell_block(
        "exec_command",
        "cat /dev/sda 2>/dev/null",
    )

    assert payload is not None
    assert json.loads(payload)["sensitive_path"] == "/dev"


def test_sensitive_shell_allows_configured_workspace_under_sensitive_prefix() -> None:
    workspace = Path("/root/.agentos/workspace")
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    try:
        script = (
            "python3 - <<'PY'\n"
            "from pathlib import Path\n"
            f"print(Path({str(workspace / 'notes.txt')!r}))\n"
            "PY"
        )
        payload = shell._sensitive_shell_block(
            "exec_command",
            script,
            workdir=str(workspace),
        )
    finally:
        current_tool_context.reset(token)

    assert payload is None


def test_sensitive_shell_still_blocks_sensitive_command_inside_workspace() -> None:
    workspace = Path("/root/.agentos/workspace")
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    try:
        payload = shell._sensitive_shell_block(
            "exec_command",
            "cat /root/.ssh/id_rsa",
            workdir=str(workspace),
        )
    finally:
        current_tool_context.reset(token)

    assert payload is not None
    assert json.loads(payload)["sensitive_path"] == "~/.ssh"


def test_sensitive_shell_workspace_exception_keeps_leaf_secret_blocks() -> None:
    workspace = Path("/root/.agentos/workspace")
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    try:
        payload = shell._sensitive_shell_block(
            "exec_command",
            f"cat {workspace / '.env'}",
            workdir=str(workspace),
        )
    finally:
        current_tool_context.reset(token)

    assert payload is not None
    assert json.loads(payload)["reason"] == "sensitive_path"


@pytest.mark.asyncio
async def test_background_process_blocks_sensitive_workdir(tmp_path: Path) -> None:
    sensitive_dir = tmp_path / ".env"

    result = await shell.background_process("echo ok", workdir=str(sensitive_dir))

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "sensitive_path"
    assert payload["tool"] == "background_process"
    assert ".env" in payload["command"]


def test_effective_workdir_resolves_relative_paths_against_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    try:
        assert shell._effective_workdir("subdir") == str((workspace / "subdir").resolve())
    finally:
        current_tool_context.reset(token)


def test_effective_workdir_rejects_foreign_posix_absolute_path_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(shell.os, "name", "nt")
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    try:
        with pytest.raises(ToolError, match="foreign_host_path"):
            shell._effective_workdir("/Users/a1/Desktop")
    finally:
        current_tool_context.reset(token)


@pytest.mark.parametrize(
    "command",
    [
        "rm /tmp/ok; cat /root/.bash_history",
        "rm /tmp/ok; ls /root",
        "rm /tmp/ok && rm -rf /root",
        "rm /tmp/ok | cat ~/.ssh/id_rsa",
    ],
)
def test_a_benign_first_segment_does_not_smuggle_a_sensitive_second_one(
    command: str,
) -> None:
    """Issue #676: chaining past the block.

    The delete-intent scan only reads ``rm`` targets, so the later segment is
    caught by the whole-command text scan instead — either way ``exec_command``
    refuses the command rather than running half of it.
    """
    payload = shell._sensitive_shell_block("exec_command", command)

    assert payload is not None, command
    assert json.loads(payload)["reason"] == "sensitive_path"
