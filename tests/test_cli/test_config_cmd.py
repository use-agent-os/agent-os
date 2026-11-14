"""CLI tests for ``agentos config set``."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentos.cli.config_cmd import _set_key
from agentos.cli.main import app
from agentos.gateway.config import GatewayConfig
from agentos.onboarding.config_store import load_config
from agentos.skills.config_vars import _value_at

runner = CliRunner()


def test_set_key_creates_nested_skills_config() -> None:
    data = GatewayConfig().to_toml_dict()
    assert "config" not in data.get("skills", {})
    assert _set_key(data, "skills.config.wiki.path", "/srv/wiki") is True
    assert data["skills"]["config"]["wiki"]["path"] == "/srv/wiki"
    cfg = GatewayConfig.model_validate(data)
    assert _value_at(cfg, "wiki.path") == "/srv/wiki"


def test_set_key_rejects_unknown_keys_outside_skills_config() -> None:
    data = GatewayConfig().to_toml_dict()
    assert _set_key(data, "skills.no_such_key", "x") is False
    assert _set_key(data, "skills.max_skills_prompt_chars", 32000) is True


def test_config_set_persists_documented_wiki_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "state"))
    cfg_path = tmp_path / "config.toml"
    result = runner.invoke(
        app,
        [
            "config",
            "set",
            "skills.config.wiki.path",
            "/srv/wiki",
            "--config",
            str(cfg_path),
        ],
    )
    assert result.exit_code == 0, result.output
    loaded = load_config(cfg_path)
    assert _value_at(loaded, "wiki.path") == "/srv/wiki"


def test_config_set_existing_model_field_still_works(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "state"))
    cfg_path = tmp_path / "config.toml"
    result = runner.invoke(
        app,
        [
            "config",
            "set",
            "skills.max_skills_prompt_chars",
            "32000",
            "--config",
            str(cfg_path),
        ],
    )
    assert result.exit_code == 0, result.output
    loaded = load_config(cfg_path)
    assert loaded.skills.max_skills_prompt_chars == 32000


def test_config_set_unknown_key_still_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "state"))
    cfg_path = tmp_path / "config.toml"
    result = runner.invoke(
        app,
        ["config", "set", "skills.no_such_key", "x", "--config", str(cfg_path)],
    )
    assert result.exit_code == 1
    assert "Key not found" in result.output
    assert not cfg_path.exists()


def test_config_set_env_hint_rejects_unknown_key() -> None:
    result = runner.invoke(app, ["config", "set", "gateway.port", "18791"])
    assert result.exit_code == 1
    assert "Key not found" in result.output
    assert "export " not in result.output.lower()


def test_config_set_env_hint_rejects_skills_config_map() -> None:
    result = runner.invoke(app, ["config", "set", "skills.config.wiki.path", "/srv/wiki"])
    assert result.exit_code == 1
    assert "Key not found" in result.output
    assert "export " not in result.output.lower()


def test_config_set_env_hint_still_prints_for_existing_field() -> None:
    result = runner.invoke(app, ["config", "set", "skills.max_skills_prompt_chars", "32000"])
    assert result.exit_code == 0, result.output
    assert "export AGENTOS_GATEWAY_SKILLS__MAX_SKILLS_PROMPT_CHARS=32000" in result.output


def _exported_assignment(output: str) -> str:
    """Return the ``NAME=value`` a shell would receive from the printed hint.

    Parsed with ``shlex`` rather than compared as text so the assertion holds
    for either quoting style ``quote_cli_arg`` produces -- POSIX single quotes
    on Linux/macOS, double quotes on Windows.
    """
    line = next((ln for ln in output.splitlines() if ln.strip().startswith("export ")), "")
    assert line, f"no export hint in output: {output!r}"
    return shlex.split(line.strip())[-1]


class TestExportHintPastesBack:
    """Without ``--config`` this command persists nothing.

    Its whole output is one line the operator is told to paste, so the value in
    that line has to survive both the shell and the renderer.
    """

    @pytest.fixture(autouse=True)
    def fixed_width(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Pin the width the renderer would use, so the same output is produced
        # on every machine and in CI.
        monkeypatch.setenv("COLUMNS", "80")

    def test_a_value_with_spaces_stays_one_token(self) -> None:
        value = "/srv/team data/My Projects"
        result = runner.invoke(app, ["config", "set", "workspace_dir", value])
        assert result.exit_code == 0, result.output
        assert _exported_assignment(result.output) == f"AGENTOS_GATEWAY_WORKSPACE_DIR={value}"

    def test_a_bracketed_span_is_not_read_as_markup(self) -> None:
        value = "/srv/projects[archive]/ws"
        result = runner.invoke(app, ["config", "set", "workspace_dir", value])
        assert result.exit_code == 0, result.output
        assert _exported_assignment(result.output) == f"AGENTOS_GATEWAY_WORKSPACE_DIR={value}"

    def test_a_closing_tag_does_not_abort_the_command(self) -> None:
        value = "p[/]ss"
        result = runner.invoke(app, ["config", "set", "auth.password", value])
        assert result.exit_code == 0, result.output
        assert result.exception is None
        assert _exported_assignment(result.output) == f"AGENTOS_GATEWAY_AUTH__PASSWORD={value}"

    def test_a_long_value_is_not_folded_mid_token(self) -> None:
        # 46 chars of value plus the 39-char assignment prefix: past the 80
        # columns the renderer falls back to when stdout is not a terminal.
        value = "/srv/shared/projects/agentos-workspace-primary"
        result = runner.invoke(app, ["config", "set", "workspace_dir", value])
        assert result.exit_code == 0, result.output
        assert f"export AGENTOS_GATEWAY_WORKSPACE_DIR={value}" in result.output
        assert _exported_assignment(result.output) == f"AGENTOS_GATEWAY_WORKSPACE_DIR={value}"

    def test_an_ordinary_value_is_still_printed_bare(self) -> None:
        # Control: quoting only where it is needed, so the common line reads
        # exactly as it did before.
        result = runner.invoke(app, ["config", "set", "port", "18800"])
        assert result.exit_code == 0, result.output
        assert "export AGENTOS_GATEWAY_PORT=18800" in result.output


def test_config_reload_asks_the_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """``agentos config reload`` is the operator's way to apply held file edits."""
    from agentos.cli import gateway_rpc

    calls: list[tuple[str, dict]] = []

    class _Client:
        async def call(self, method: str, params: dict | None = None) -> dict:
            calls.append((method, params or {}))
            return {"changed": ["trading.daily_cap_usd"], "restartRequired": []}

    def fake_sync(action, **_kwargs):  # type: ignore[no-untyped-def]
        import asyncio

        return asyncio.run(action(_Client()))

    monkeypatch.setattr(gateway_rpc, "run_gateway_sync", fake_sync)
    result = runner.invoke(app, ["config", "reload"])
    assert result.exit_code == 0, result.output
    assert calls == [("config.reload", {})]
    assert "trading.daily_cap_usd" in result.output
