"""`agentos upgrade` command — delegate, check, dry-run, restart+verify."""

from __future__ import annotations

from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from agentos.cli import upgrade_cmd
from agentos.cli.install_method import InstallMethod, UpgradePlan

runner = CliRunner()


def _app() -> typer.Typer:
    app = typer.Typer()
    app.command("upgrade")(upgrade_cmd.upgrade_command)

    @app.command("noop")
    def _noop() -> None:  # keeps Typer in multi-command mode
        return None

    return app


def _delegated_plan() -> UpgradePlan:
    return UpgradePlan(
        method=InstallMethod.UV_TOOL,
        delegated=True,
        tool="/abs/uv",
        command=["/abs/uv", "tool", "upgrade", "use-agent-os"],
        manual_hint="uv tool upgrade use-agent-os",
    )


def _pip_plan() -> UpgradePlan:
    return UpgradePlan(
        method=InstallMethod.PIP,
        delegated=False,
        tool=None,
        command=["python", "-m", "pip", "install", "--upgrade", "use-agent-os"],
        manual_hint="python -m pip install --upgrade use-agent-os",
    )


def _ok_run(*_: Any, **__: Any) -> upgrade_cmd.UpgradeRunResult:
    return upgrade_cmd.UpgradeRunResult(
        ok=True, timed_out=False, returncode=0, stdout="upgraded", stderr=""
    )


# --- --check ---------------------------------------------------------------


def test_check_reports_newer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr("agentos.cli.pypi_client.latest_version", lambda timeout=5.0: "99999.1.1")
    result = runner.invoke(_app(), ["upgrade", "--check"])
    assert result.exit_code == 0
    assert "newer version is available" in result.stdout


def test_check_offline_exit_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr("agentos.cli.pypi_client.latest_version", lambda timeout=5.0: None)
    result = runner.invoke(_app(), ["upgrade", "--check"])
    assert result.exit_code == 0
    assert "could not check (offline)" in result.stdout


def test_check_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"run": False}
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr("agentos.cli.pypi_client.latest_version", lambda timeout=5.0: "99999.1.1")
    monkeypatch.setattr(
        upgrade_cmd,
        "_run_upgrade_subprocess",
        lambda *a, **k: called.__setitem__("run", True),
    )
    runner.invoke(_app(), ["upgrade", "--check"])
    assert called["run"] is False


# --- non-delegated (pip/editable) ------------------------------------------


def test_pip_prints_manual_and_exits_3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _pip_plan)
    result = runner.invoke(_app(), ["upgrade"])
    assert result.exit_code == 3
    assert "pip install --upgrade use-agent-os" in result.stdout


# --- --dry-run -------------------------------------------------------------


def test_dry_run_touches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = {"run": False}
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(
        upgrade_cmd, "_run_upgrade_subprocess", lambda *a, **k: ran.__setitem__("run", True)
    )
    result = runner.invoke(_app(), ["upgrade", "--dry-run"])
    assert result.exit_code == 0
    assert "Would run: /abs/uv tool upgrade use-agent-os" in result.stdout
    assert ran["run"] is False


# --- successful delegate + restart+verify ----------------------------------


def test_upgrade_success_restarts_and_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(upgrade_cmd, "_run_upgrade_subprocess", _ok_run)
    monkeypatch.setattr(upgrade_cmd, "_installed_version_via", lambda *a, **k: "99999.2.0")
    seen: dict[str, Any] = {}

    def fake_restart(**kwargs: Any) -> bool:
        seen.update(kwargs)
        return True

    monkeypatch.setattr(upgrade_cmd, "_restart_and_verify", fake_restart)
    result = runner.invoke(_app(), ["upgrade"])
    assert result.exit_code == 0
    assert "Upgraded:" in result.stdout
    assert "→ 99999.2.0" in result.stdout
    assert seen["expected_version"] == "99999.2.0"


def test_upgrade_verify_failure_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(upgrade_cmd, "_run_upgrade_subprocess", _ok_run)
    monkeypatch.setattr(upgrade_cmd, "_installed_version_via", lambda *a, **k: "99999.2.0")
    monkeypatch.setattr(upgrade_cmd, "_restart_and_verify", lambda **k: False)
    result = runner.invoke(_app(), ["upgrade"])
    assert result.exit_code == 1


# --- --no-restart ----------------------------------------------------------


def test_no_restart_loud_warning_exit_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(upgrade_cmd, "_run_upgrade_subprocess", _ok_run)
    monkeypatch.setattr(upgrade_cmd, "_installed_version_via", lambda *a, **k: "99999.2.0")
    restarted = {"called": False}
    monkeypatch.setattr(
        upgrade_cmd,
        "_restart_and_verify",
        lambda **k: restarted.__setitem__("called", True),
    )
    result = runner.invoke(_app(), ["upgrade", "--no-restart"])
    assert result.exit_code == 0
    assert restarted["called"] is False
    # Loud warning, prefixed ⚠ (emitted to stderr; CliRunner merges streams).
    assert "⚠" in result.output
    assert "OLD version" in result.output


# --- timeout ---------------------------------------------------------------


def test_upgrade_timeout_exits_one_with_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(
        upgrade_cmd,
        "_run_upgrade_subprocess",
        lambda *a, **k: upgrade_cmd.UpgradeRunResult(
            ok=False, timed_out=True, returncode=None, stdout="", stderr=""
        ),
    )
    result = runner.invoke(_app(), ["upgrade"])
    assert result.exit_code == 1
    assert "timed out" in result.stdout
    assert "process group" in result.stdout


def test_upgrade_failure_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_cmd, "build_upgrade_plan", _delegated_plan)
    monkeypatch.setattr(
        upgrade_cmd,
        "_run_upgrade_subprocess",
        lambda *a, **k: upgrade_cmd.UpgradeRunResult(
            ok=False, timed_out=False, returncode=2, stdout="", stderr="boom"
        ),
    )
    result = runner.invoke(_app(), ["upgrade"])
    assert result.exit_code == 1
    assert "Upgrade failed" in result.stdout
