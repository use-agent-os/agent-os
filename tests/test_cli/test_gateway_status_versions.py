"""Two-version gateway status: CLI + gateway version, mismatch diagnostic."""

from __future__ import annotations

import json
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

import agentos
from agentos.cli import gateway_cmd
from agentos.cli.gateway_lifecycle import GatewayLifecycleResult

runner = CliRunner()


def _app() -> typer.Typer:
    app = typer.Typer()
    app.command("status")(gateway_cmd.status_gateway)

    @app.command("noop")
    def _noop() -> None:  # keeps Typer in multi-command mode
        return None

    return app


class _FakeManager:
    def __init__(self, state: str) -> None:
        self._state = state

    def status(self) -> GatewayLifecycleResult:
        return GatewayLifecycleResult(
            action="status", state=self._state, ok=True, managed=True, port=18791
        )


def _install(monkeypatch: pytest.MonkeyPatch, state: str, gateway_version: str | None) -> None:
    monkeypatch.setattr(gateway_cmd, "_lifecycle_manager", lambda **_: _FakeManager(state))
    monkeypatch.setattr(
        gateway_cmd, "gateway_handshake_version", lambda **_: gateway_version
    )


def test_status_shows_both_versions_matching(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, "running", agentos.__version__)
    result = runner.invoke(_app(), ["status"])
    assert result.exit_code == 0
    assert f"CLI version:     {agentos.__version__}" in result.output
    assert f"Gateway version: {agentos.__version__}" in result.output
    assert "Version mismatch" not in result.output


def test_status_flags_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, "running", "0.0.1")
    result = runner.invoke(_app(), ["status"])
    assert result.exit_code == 0
    assert "Gateway version: 0.0.1" in result.output
    assert "Version mismatch" in result.output
    assert "gateway restart" in result.output


def test_status_not_running_reports_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gateway_cmd,
        "_lifecycle_manager",
        lambda **_: _FakeManager("not_started"),
    )
    calls: dict[str, Any] = {"handshake": 0}

    def _hs(**_: Any) -> None:
        calls["handshake"] += 1
        return None

    monkeypatch.setattr(gateway_cmd, "gateway_handshake_version", _hs)
    result = runner.invoke(_app(), ["status"])
    assert result.exit_code == 0
    assert "unknown (not running" in result.output
    # No handshake attempted for a non-running gateway.
    assert calls["handshake"] == 0


def test_status_json_carries_both_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, "running", "0.0.1")
    result = runner.invoke(_app(), ["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["cliVersion"] == agentos.__version__
    assert payload["gatewayVersion"] == "0.0.1"
    assert payload["versionMismatch"] is True
