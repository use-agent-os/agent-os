"""WAL-01: a recursive read of an ancestor of ``wallets/`` is refused.

The sensitive-path floor matched a path that *named* a protected file. It did
not see that ``grep -r . ~/.agentos``, ``cp -r ~/.agentos dst`` or ``tar -C ~
-cf a.tar .agentos`` -- a recursive read of the unprotected *parent* -- reads
every protected file under it. The agent never had to spell ``wallets``. These
port the audit's PoC (``grep -r``/``cp -r``/``tar`` of the state root) plus
variants and the ``execute_code`` twin, and confirm ordinary recursive work in
the workspace still runs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentos.gateway.agent_surface import AgentSurface
from agentos.tools.builtin import code_exec, shell
from agentos.tools.types import CallerKind, ToolContext, current_tool_context

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX shell semantics")


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    workspace = home / "ws"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("AGENTOS_STATE_DIR", raising=False)
    monkeypatch.setattr(shell, "get_runtime", lambda: None)
    state = home / ".agentos"
    AgentSurface().ensure_operator_file(state)
    (state / "wallets" / "unlock.key").write_text("vault-password\n")
    (state / "wallets" / "0xabc.json").write_text("{}")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_rsa").write_text("PRIVATE")
    # ordinary workspace content a recursive command legitimately scans
    (workspace / "notes.txt").write_text("todo")
    return {"home": home, "workspace": workspace, "state": state}


async def _exec(command: str, workspace: Path, *, elevated: str | None = None) -> str:
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            session_key="agent:main:wal01",
            workspace_dir=str(workspace),
            elevated=elevated,
        )
    )
    try:
        return await shell.exec_command(command, workdir=str(workspace))
    finally:
        current_tool_context.reset(token)


def _blocked(output: str) -> bool:
    try:
        return json.loads(output).get("reason") == "sensitive_path"
    except (ValueError, AttributeError):
        return False


RECURSIVE_READS = [
    "grep -r . ~/.agentos",
    "grep -r secret ~",
    "cp -r ~/.agentos /tmp/exfil",
    "cp -R ~/.agentos/ /tmp/exfil",
    "tar -C ~ -cf /tmp/a.tar .agentos",
    "tar czf /tmp/h.tgz -C ~ .",
    "find ~ -name unlock.key",
    "find ~/.agentos -type f",
    "rsync -a ~/ /tmp/backup",
    "cd ~/.agentos && grep -r . .",
    "cd ~/.agentos; tar -cf /tmp/a.tar .",
    "D=~/.agentos; cp -r $D /tmp/x",
    "A=~; cd $A && tar -cf /tmp/t.tar .agentos",
]


@pytest.mark.parametrize("elevated", ["bypass", None])
@pytest.mark.parametrize("command", RECURSIVE_READS)
async def test_recursive_read_of_an_ancestor_is_blocked(
    layout: dict[str, Path], command: str, elevated: str | None
) -> None:
    out = await _exec(command, layout["workspace"], elevated=elevated)
    assert _blocked(out), out
    assert "vault-password" not in out and "PRIVATE" not in out


async def test_elevated_full_still_lifts_it(layout: dict[str, Path]) -> None:
    out = await _exec("grep -r . ~/.agentos", layout["workspace"], elevated="full")
    assert not _blocked(out)


ALLOWED = [
    "grep -r TODO .",
    "find . -name '*.txt'",
    "ls -R .",
    "cat notes.txt",
    "ls ~/.agentos",
    "cp notes.txt notes.bak",
    "tar -cf /tmp/ws.tar notes.txt",
]


@pytest.mark.parametrize("command", ALLOWED)
async def test_ordinary_recursive_work_still_runs(layout: dict[str, Path], command: str) -> None:
    out = await _exec(command, layout["workspace"])
    assert not _blocked(out), out


CODE_READS = [
    "import shutil, os\nshutil.copytree(os.path.expanduser('~/.agentos'), '/tmp/x')",
    "import os\nfor root, _d, _f in os.walk(os.path.expanduser('~/.agentos')):\n    print(root)",
    "from pathlib import Path\nfor p in Path('~/.agentos').expanduser().rglob('*'):\n    print(p)",
]


@pytest.mark.parametrize("code", CODE_READS)
def test_execute_code_recursive_ancestor_is_blocked(layout: dict[str, Path], code: str) -> None:
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            session_key="agent:main:wal01",
            workspace_dir=str(layout["workspace"]),
        )
    )
    try:
        result = code_exec._check_code_sensitive_access(code)
    finally:
        current_tool_context.reset(token)
    assert result is not None and result[0] == "sensitive_path"


def test_execute_code_recursive_in_workspace_is_allowed(layout: dict[str, Path]) -> None:
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            session_key="agent:main:wal01",
            workspace_dir=str(layout["workspace"]),
        )
    )
    try:
        code = "from pathlib import Path\nfor p in Path('.').rglob('*.py'):\n    print(p)"
        assert code_exec._check_code_sensitive_access(code) is None
    finally:
        current_tool_context.reset(token)
