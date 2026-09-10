"""CLI cron run must exit non-zero when the gateway reports a failed run.

Regression for https://github.com/use-agent-os/agent-os/issues/1684 —
``cron.run`` returns an honest ``success: false`` payload over a successful
RPC; the CLI used to print it via ``_emit_success`` and still exit 0.
"""

from __future__ import annotations

import json
from typing import Any

from typer.testing import CliRunner

from agentos.cli.main import app

runner = CliRunner()


class _RunPayloadGateway:
    calls: list[tuple[str, dict[str, Any]]] = []
    payload: dict[str, Any] = {}

    def __init__(self) -> None:
        pass

    async def connect(self, url: str, *, token=None) -> None:
        return None

    async def close(self) -> None:
        return None

    async def call(self, method: str, params: dict | None = None) -> Any:
        type(self).calls.append((method, params or {}))
        return type(self).payload


def _install(monkeypatch, payload: dict[str, Any]) -> type[_RunPayloadGateway]:
    _RunPayloadGateway.calls = []
    _RunPayloadGateway.payload = payload
    monkeypatch.setattr("agentos.cli.gateway_client.GatewayClient", _RunPayloadGateway)
    return _RunPayloadGateway


def test_cron_run_missing_job_exits_nonzero(monkeypatch) -> None:
    fake = _install(
        monkeypatch,
        {
            "success": False,
            "status": "not_found",
            "reason": "not_found",
            "error": "Cron job not found",
        },
    )

    result = runner.invoke(
        app,
        ["cron", "run", "missing-job-xyz", "--yes", "--json"],
    )

    assert result.exit_code == 2
    err = json.loads(result.stderr)
    assert err["error"]["code"] == "NOT_FOUND"
    assert "Cron job not found" in err["error"]["message"]
    assert ("cron.run", {"id": "missing-job-xyz"}) in fake.calls


def test_cron_run_other_failure_exits_nonzero(monkeypatch) -> None:
    _install(
        monkeypatch,
        {
            "success": False,
            "status": "disabled",
            "reason": "disabled",
            "error": "Cron job is disabled",
        },
    )

    result = runner.invoke(
        app,
        ["cron", "run", "job-disabled", "--yes", "--json"],
    )

    assert result.exit_code == 1
    err = json.loads(result.stderr)
    assert err["error"]["code"] == "CRON_RUN_FAILED"


def test_cron_run_accepted_success_still_exits_zero(monkeypatch) -> None:
    fake = _install(
        monkeypatch,
        {
            "success": True,
            "status": "accepted",
            "reply": "done",
            "error": None,
            "duration_ms": 12,
        },
    )

    result = runner.invoke(
        app,
        ["cron", "run", "job-1", "--yes", "--json"],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["status"] == "accepted"
    assert ("cron.run", {"id": "job-1"}) in fake.calls
