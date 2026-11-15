"""WAL-17: copies of a protected config file are on the floor like the file itself.

Every persisted config write leaves ``<config>.backup.<stamp>`` (a verbatim
copy: Uniswap API key, keyed RPC URLs, the gateway token) beside the file,
and the atomic writer stages ``.<config>.<rand>.tmp``. The floor matched the
config path only exactly, so ``cat`` of a backup -- which an agent could make
the gateway write with one allowed ``config.set`` -- read the secrets back.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentos.gateway.config_migration import make_config_backup
from agentos.sandbox.sensitive_paths import (
    is_sensitive_path,
    protect_config_path,
    reset_protected_config_paths,
    sensitive_path_in_text,
)
from agentos.tools.builtin import filesystem, shell
from agentos.tools.types import CallerKind, ToolContext, current_tool_context

SECRET = "uni-DUMMY-0123456789abcdef"
CONFIG_TEXT = f'[trading]\nuniswap_api_key = "{SECRET}"\n'


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    root = home / ".agentos"
    (home / "ws").mkdir(parents=True)
    root.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(root))
    monkeypatch.setattr(shell, "get_runtime", lambda: None)
    (root / "config.toml").write_text(CONFIG_TEXT, encoding="utf-8")
    reset_protected_config_paths()
    yield root
    reset_protected_config_paths()


def _ctx(workspace: Path) -> ToolContext:
    return ToolContext(
        caller_kind=CallerKind.CLI,
        session_key="agent:main:wal17",
        workspace_dir=str(workspace),
    )


def test_backup_of_state_config_is_sensitive(state: Path) -> None:
    backup = make_config_backup(state / "config.toml")
    assert backup.read_text(encoding="utf-8") == CONFIG_TEXT
    assert is_sensitive_path(str(backup)) is not None
    assert is_sensitive_path(str(state / ".config.toml.abc123.tmp")) is not None
    # An unrelated sibling is not swept up.
    assert is_sensitive_path(str(state / "config.toml.md")) is None
    assert is_sensitive_path(str(state / "notes.txt")) is None


def test_backup_of_active_project_config_is_sensitive(state: Path, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    active = project / "agentos.toml"
    active.write_text(CONFIG_TEXT, encoding="utf-8")
    protect_config_path(active)
    backup = make_config_backup(active)
    assert is_sensitive_path(str(backup)) is not None


async def test_exec_command_refuses_to_read_a_backup(state: Path) -> None:
    backup = make_config_backup(state / "config.toml")
    workspace = state.parent / "ws"
    token = current_tool_context.set(_ctx(workspace))
    try:
        out = await shell.exec_command(f'cat "{backup}"', workdir=str(workspace))
    finally:
        current_tool_context.reset(token)
    assert SECRET not in out
    assert json.loads(out)["reason"] == "sensitive_path"


async def test_read_file_refuses_a_backup(state: Path) -> None:
    backup = make_config_backup(state / "config.toml")
    token = current_tool_context.set(_ctx(state.parent / "ws"))
    try:
        out = await filesystem.read_file(str(backup))
    finally:
        current_tool_context.reset(token)
    assert SECRET not in out
    assert json.loads(out)["reason"] == "sensitive_path"


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell globs")
def test_glob_and_relative_spellings_of_a_backup_are_refused(state: Path) -> None:
    make_config_backup(state / "config.toml")
    workspace = state.parent / "ws"
    for command in (
        "cat ~/.agentos/config.toml.backup.*",
        "cat ~/.agentos/*backup*",
        "cat ~/.agentos/config.toml.b*",
        "cd ~/.agentos && cat config.toml.backup.*",
    ):
        assert sensitive_path_in_text(command, workspace=workspace, cwd=workspace), command
