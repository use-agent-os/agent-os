"""The trading ledger is on the agent's sensitive-path floor (WAL-05).

It holds the parked orders the operator approves by id and the approved
mandates and triggers the engine runs unattended; an agent shell that rewrote
it once redirected an approved send to itself. The engine now also seals those
rows (``tests/test_trading/test_ledger_integrity.py``); this is the first line.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentos.sandbox.sensitive_paths import (
    is_sensitive_path,
    runtime_sensitive_prefixes,
    sensitive_path_in_text,
)
from agentos.tools.builtin import shell
from agentos.tools.types import CallerKind, ToolContext, current_tool_context
from agentos.trading.ledger import LEDGER_SIDECARS, default_ledger_path


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "agentos-state"
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(root))
    (root / "state").mkdir(parents=True)
    return root


def test_the_ledger_and_its_sidecars_are_sensitive(state: Path) -> None:
    ledger = default_ledger_path()
    assert Path(ledger).parent == state / "state"
    for path in (str(ledger), *(str(ledger) + suffix for suffix in LEDGER_SIDECARS)):
        assert is_sensitive_path(path) is not None, path
    prefixes = {os.path.normcase(p) for p in runtime_sensitive_prefixes()}
    assert os.path.normcase(str(ledger)) in prefixes


def test_backups_and_copies_of_the_ledger_are_sensitive(state: Path, tmp_path: Path) -> None:
    snapshot = state / "state" / "snapshots" / "pre-upgrade-20261115T000000Z"
    assert is_sensitive_path(str(snapshot / "state" / "trading.sqlite")) is not None
    assert is_sensitive_path(str(snapshot / "home" / "config.toml")) is not None
    assert is_sensitive_path(str(snapshot)) is not None
    copy = tmp_path / "elsewhere" / "trading.sqlite"
    assert is_sensitive_path(str(copy)) is not None
    assert is_sensitive_path(str(copy) + "-wal") is not None


def test_the_rest_of_the_state_dir_is_not_swept_in(state: Path) -> None:
    # approval_queue.sqlite is on the floor for its own reason (the skill
    # signing approvals, WAL-10), not as part of the ledger.
    for name in ("safety_log.jsonl", "history"):
        assert is_sensitive_path(str(state / "state" / name)) is None, name
    assert is_sensitive_path(str(state / "state" / "trading.sqlite.notes")) is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell semantics")
def test_shell_spellings_of_the_ledger_are_caught(state: Path, tmp_path: Path) -> None:
    ledger = default_ledger_path()
    ledger.write_bytes(b"")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    commands = [
        f'sqlite3 "{ledger}" "UPDATE orders SET recipient=\'0xee\' WHERE 1"',
        f'sqlite3 "{ledger}" ".dump"',
        'sqlite3 "$AGENTOS_STATE_DIR/state/trading.sqlite" "DELETE FROM daily_spend"',
        f"cat {state}/state/trading.sq*",
        f"cd {state}/state && sqlite3 trading.sqlite .dump",
    ]
    for command in commands:
        assert sensitive_path_in_text(command, workspace=workspace, cwd=workspace), command


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell semantics")
async def test_the_agent_shell_tool_refuses_the_ledger(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shell, "get_runtime", lambda: None)
    ledger = default_ledger_path()
    ledger.write_bytes(b"SQLite format 3\x00")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            session_key="agent:main:wal05",
            workspace_dir=str(workspace),
        )
    )
    try:
        output = await shell.exec_command(f'cat "{ledger}"', workdir=str(workspace))
    finally:
        current_tool_context.reset(token)
    assert json.loads(output).get("reason") == "sensitive_path"
