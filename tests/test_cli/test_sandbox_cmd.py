from __future__ import annotations

import json
import tomllib
from pathlib import Path

from typer.testing import CliRunner

from agentos.cli.main import app
from agentos.onboarding.config_store import load_config

runner = CliRunner()


def _invoke(config_path: Path, *args: str):
    return runner.invoke(app, ["sandbox", *args, "--config", str(config_path)])


def test_sandbox_status_preserves_default_bypass_posture(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"

    result = runner.invoke(
        app,
        ["sandbox", "status", "--config", str(config_path), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["posture"] == "bypass"
    assert payload["sandbox"]["sandbox"] is False
    assert payload["sandbox"]["security_grading"] is False
    assert payload["permissions"]["default_mode"] == "bypass"
    assert payload["restart_required"] is False


def test_sandbox_bypass_persists_global_bypass_posture(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"

    result = _invoke(config_path, "bypass")

    assert result.exit_code == 0, result.output
    cfg = load_config(config_path)
    assert cfg.sandbox.sandbox is False
    assert cfg.sandbox.security_grading is False
    assert cfg.permissions.default_mode == "bypass"
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert data["sandbox"]["sandbox"] is False
    assert data["sandbox"]["security_grading"] is False
    assert data["permissions"]["default_mode"] == "bypass"
    assert "restart" in result.output.lower()


def test_sandbox_full_and_on_are_reversible(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"

    full = _invoke(config_path, "full")
    on = _invoke(config_path, "on")

    assert full.exit_code == 0, full.output
    assert on.exit_code == 0, on.output
    cfg = load_config(config_path)
    assert cfg.sandbox.sandbox is True
    assert cfg.sandbox.security_grading is True
    assert cfg.permissions.default_mode == "off"


def test_sandbox_reset_restores_default_bypass_posture(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"

    on = _invoke(config_path, "on")
    reset = _invoke(config_path, "reset")

    assert on.exit_code == 0, on.output
    assert reset.exit_code == 0, reset.output
    cfg = load_config(config_path)
    assert cfg.sandbox.sandbox is False
    assert cfg.sandbox.security_grading is False
    assert cfg.permissions.default_mode == "bypass"
