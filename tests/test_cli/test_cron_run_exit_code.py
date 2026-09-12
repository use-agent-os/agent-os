"""``agentos cron run`` must exit non-zero when the job did not actually run.

``cron.run`` reports a missing / disabled / busy job as a *successful* RPC whose
payload carries ``success: false`` (so the Control UI can render it). The CLI
has to translate that into ``$?`` itself; before the fix ``_emit_success``
printed the honest payload and returned 0 (#1684).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import typer

from agentos.cli import cron_cmd


@pytest.fixture
def run_payload(monkeypatch):
    """Drive ``cron_run`` against a canned ``cron.run`` reply, skipping confirmation."""

    def _install(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        calls: list[tuple[str, dict[str, Any]]] = []

        class _Client:
            async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
                calls.append((method, params))
                return payload

        def _runner(fn, *, json_output: bool = False):  # noqa: ARG001
            return asyncio.run(fn(_Client()))

        monkeypatch.setattr(cron_cmd, "run_gateway_sync", _runner)
        monkeypatch.setattr(cron_cmd, "confirm_or_exit", lambda *a, **kw: None)
        return calls

    return _install


def _invoke(job_id: str = "missing-job-xyz", *, json_output: bool) -> int:
    try:
        cron_cmd.cron_run(job_id=job_id, yes=True, json_output=json_output)
    except typer.Exit as exc:
        return int(exc.exit_code)
    return 0


def test_missing_job_exits_2_and_keeps_the_payload_on_stdout(run_payload, capsys) -> None:
    payload = {
        "success": False,
        "status": "not_found",
        "reason": "not_found",
        "error": "Cron job not found",
    }
    calls = run_payload(payload)

    assert _invoke(json_output=True) == 2

    captured = capsys.readouterr()
    assert calls == [("cron.run", {"id": "missing-job-xyz"})]
    # stdout is still exactly the wire payload, so ``--json`` consumers keep it.
    assert json.loads(captured.out) == payload
    # the diagnostic goes to stderr with the same code ``cron status`` uses.
    err = json.loads(captured.err)
    assert err["error"]["code"] == "NOT_FOUND"
    assert "Cron job not found" in err["error"]["message"]


def test_missing_job_exits_2_in_human_mode(run_payload, capsys) -> None:
    run_payload({"success": False, "status": "not_found", "reason": "not_found", "error": None})

    assert _invoke(json_output=False) == 2

    captured = capsys.readouterr()
    assert "not_found" in captured.err


@pytest.mark.parametrize("status", ["disabled", "busy", "blocked", "no_handler"])
def test_rejected_run_exits_1(run_payload, status: str, capsys) -> None:
    run_payload({"success": False, "status": status, "reason": status, "error": None})

    assert _invoke("job-1", json_output=True) == 1

    err = json.loads(capsys.readouterr().err)
    assert err["error"]["code"] == f"RUN_{status.upper()}"
    assert status in err["error"]["message"]


def test_accepted_run_that_failed_exits_1(run_payload, capsys) -> None:
    run_payload(
        {
            "success": False,
            "status": "accepted",
            "reply": "",
            "error": "handler exploded",
            "duration_ms": 12,
        }
    )

    assert _invoke("job-1", json_output=True) == 1

    err = json.loads(capsys.readouterr().err)
    assert err["error"]["code"] == "RUN_FAILED"
    assert err["error"]["message"] == "handler exploded"


def test_successful_run_still_exits_0(run_payload, capsys) -> None:
    payload = {"success": True, "status": "accepted", "reply": "done", "error": None}
    run_payload(payload)

    assert _invoke("job-1", json_output=True) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out) == payload
    assert captured.err == ""


def test_non_dict_payload_is_left_alone(run_payload) -> None:
    run_payload("ok")  # type: ignore[arg-type]

    assert _invoke("job-1", json_output=False) == 0
