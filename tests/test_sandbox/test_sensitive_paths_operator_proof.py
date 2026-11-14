"""The operator's proof stays out of an agent shell's reach, however it is spelled.

Two holes, both reproduced against the real ``exec_command`` body:

* the wallets prefix was the literal ``~/.agentos/wallets``, while the vault
  and ``operator.secret`` follow ``AGENTOS_STATE_DIR`` (``/var/lib/agentos``
  in the container image, whose ``$HOME`` is elsewhere);
* the scan read the command text before the shell expanded globs and
  variables or applied ``cd``.

This floor is defence in depth: the gateway also refuses the operator file
from an agent's connection (``tests/test_gateway/test_agent_surface.py``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentos.gateway.agent_surface import AgentSurface
from agentos.sandbox.sensitive_paths import (
    is_sensitive_path,
    runtime_sensitive_prefixes,
    sensitive_path_in_text,
)
from agentos.tools.builtin import shell
from agentos.tools.types import CallerKind, ToolContext, current_tool_context

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX shell semantics")


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    workspace = home / "ws"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AGENTOS_STATE_DIR", raising=False)
    monkeypatch.setattr(shell, "get_runtime", lambda: None)
    return {"home": home, "workspace": workspace, "tmp": tmp_path}


async def _exec(command: str, workspace: Path, *, elevated: str | None, **kwargs) -> str:
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            session_key="agent:main:v316",
            workspace_dir=str(workspace),
            elevated=elevated,
        )
    )
    try:
        return await shell.exec_command(command, workdir=str(workspace), **kwargs)
    finally:
        current_tool_context.reset(token)


def _blocked(output: str) -> bool:
    try:
        return json.loads(output).get("reason") == "sensitive_path"
    except (ValueError, AttributeError):
        return False


class TestStateDirOutsideHome:
    """The container layout: AGENTOS_STATE_DIR=/var/lib/agentos, HOME elsewhere."""

    @pytest.fixture
    def state(self, layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> Path:
        state = layout["tmp"] / "var-lib-agentos"
        monkeypatch.setenv("AGENTOS_STATE_DIR", str(state))
        AgentSurface().ensure_operator_file()
        (state / "wallets" / "unlock.key").write_text("vault-password\n")
        (state / "wallets" / "0xabc.json").write_text("{}")
        return state

    def test_real_wallets_dir_is_a_sensitive_prefix(self, state: Path) -> None:
        wallets = str(state / "wallets")
        assert wallets in runtime_sensitive_prefixes()
        for name in ("operator.secret", "unlock.key", "0xabc.json"):
            assert is_sensitive_path(str(state / "wallets" / name)) is not None

    @pytest.mark.parametrize("elevated", ["bypass", None])
    @pytest.mark.parametrize(
        "command",
        [
            "cat {state}/wallets/operator.secret",
            "cat {state}/wallets/*",
            "cat {state}/wallets/0xabc.json",
            "cat {state}/wal*/op*",
            "cat $AGENTOS_STATE_DIR/wallets/operator.secret",
        ],
    )
    async def test_agent_shell_cannot_read_it(
        self, state: Path, layout: dict[str, Path], command: str, elevated: str | None
    ) -> None:
        out = await _exec(command.format(state=state), layout["workspace"], elevated=elevated)
        assert _blocked(out), out
        assert "vault-password" not in out


class TestShellSpellings:
    """The audit's spellings of ``~/.agentos/wallets``, default layout."""

    @pytest.fixture
    def secret(self, layout: dict[str, Path]) -> str:
        path = AgentSurface().ensure_operator_file(layout["home"] / ".agentos")
        (path.parent / "unlock.key").write_text("vault-password\n")
        return path.read_text().strip()

    @pytest.mark.parametrize("elevated", ["bypass", None])
    @pytest.mark.parametrize(
        "command",
        [
            "cat ~/.agentos/wal*/operator.secret",
            "cat ~/.agentos/wallet?/unlock.k?y",
            "cat ~/.agentos/[w]allets/operator.secret",
            "cat ~/.agentos/**/operator.secret",
            "cd ~/.agentos && cd wallets && cat operator.secret",
            "cd ~/.agentos; cat wallets/unlock.key",
            "D=wallets; cat ~/.agentos/$D/operator.secret",
            "D=wallets; cat ~/.agentos/${D}/*",
            'cat ~/.agentos/"$(echo wallets)"/operator.secret',
            "cat ~/.agentos/`printf wallets`/unlock.key",
        ],
    )
    async def test_blocked(
        self, secret: str, layout: dict[str, Path], command: str, elevated: str | None
    ) -> None:
        out = await _exec(command, layout["workspace"], elevated=elevated)
        assert _blocked(out), out
        assert secret not in out and "vault-password" not in out

    @pytest.mark.parametrize("elevated", ["bypass", None])
    async def test_a_path_in_a_model_chosen_env_value_is_blocked(
        self, secret: str, layout: dict[str, Path], elevated: str | None
    ) -> None:
        path = str(layout["home"] / ".agentos" / "wallets" / "operator.secret")
        out = await _exec('cat "$F"', layout["workspace"], elevated=elevated, env={"F": path})
        assert _blocked(out), out
        assert secret not in out

    async def test_a_workdir_inside_the_agentos_home_is_resolved(
        self, secret: str, layout: dict[str, Path]
    ) -> None:
        token = current_tool_context.set(
            ToolContext(
                caller_kind=CallerKind.CLI,
                session_key="agent:main:v316",
                workspace_dir=str(layout["workspace"]),
                elevated="bypass",
            )
        )
        try:
            out = await shell.exec_command(
                "cat wallets/unlock.k*", workdir=str(layout["home"] / ".agentos")
            )
        finally:
            current_tool_context.reset(token)
        assert _blocked(out), out


class TestOrdinaryCommandsStayOrdinary:
    @pytest.mark.parametrize(
        "command",
        [
            "ls /*",
            "ls ~/*",
            "ls *.key",
            "find . -name '*.py'",
            "grep -r wallets .",
            "cd src && cat main.py",
            "echo $HOME",
            "cat $UNKNOWN_VARIABLE_XYZ",
            "ls ~/projects/*/README.md",
            "for f in *.txt; do wc -l $f; done",
            "git log --grep 'operator' -- docs/",
        ],
    )
    def test_not_blocked(self, layout: dict[str, Path], command: str) -> None:
        marker = sensitive_path_in_text(
            command, workspace=layout["workspace"], cwd=layout["workspace"]
        )
        assert marker is None, (command, marker)


class TestActiveConfigFile:
    """The file the watcher reloads holds the operator-only settings."""

    @pytest.fixture
    def config_file(self, layout: dict[str, Path]) -> Path:
        from agentos.sandbox import sensitive_paths

        path = layout["home"] / ".agentos" / "config.toml"
        path.parent.mkdir(parents=True)
        path.write_text("[trading]\ndaily_cap_usd = 1000.0\n")
        project = layout["tmp"] / "project" / "agentos.toml"
        project.parent.mkdir()
        project.write_text("")
        sensitive_paths.protect_config_path(project)
        yield project
        sensitive_paths.reset_protected_config_paths()

    @pytest.mark.parametrize(
        "command",
        [
            "cat > ~/.agentos/config.toml <<'EOF'\n[trading]\ndaily_cap_usd = 1000000.0\nEOF",
            "echo '[trading]' >> ~/.agentos/config.toml",
            "cp /tmp/x ~/.agentos/conf*.toml",
            "cd ~/.agentos && sed -i s/1000.0/1e9/ config.toml",
            "cat {project}",
        ],
    )
    async def test_agent_shell_cannot_touch_it(
        self, config_file: Path, layout: dict[str, Path], command: str
    ) -> None:
        out = await _exec(
            command.format(project=config_file), layout["workspace"], elevated="bypass"
        )
        assert _blocked(out), out
        assert "1000000" not in (layout["home"] / ".agentos" / "config.toml").read_text()

    def test_file_tools_see_it_too(self, config_file: Path, layout: dict[str, Path]) -> None:
        assert is_sensitive_path(str(layout["home"] / ".agentos" / "config.toml")) is not None
        assert is_sensitive_path(str(config_file)) is not None
        # A sibling is not the config file.
        assert is_sensitive_path(str(layout["home"] / ".agentos" / "config.toml.bak")) is None
