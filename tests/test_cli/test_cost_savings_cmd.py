"""`agentos cost savings` — Pilot Router savings report from the decision log."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agentos.cli.main import app

runner = CliRunner()


def _write_log(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "turn_id": "t1",
            "session_key": "s1",
            "prompt_hash": "a" * 16,
            "system_prompt_hash": "b" * 16,
            "tool_list_hash": "c" * 16,
            "tool_choice": "auto",
            "tokens_input": 1000,
            "tokens_output": 100,
            "model": "glm-5.2",
            "provider": "openrouter",
            "latency_ms": 900,
            "ts": "2026-09-01T10:00:00Z",
            "savings": {
                "routed_model": "glm-5.2",
                "baseline_model": "gpt-5.6-luna",
                "routing_confidence": 0.6,
                "routing_savings_pct": 20.0,
                "routing_savings_usd_estimated_vs_baseline": 0.25,
                "cost_usd": 0.75,
            },
        },
    ]
    (log_dir / "decisions-20260901.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    path = tmp_path / "logs"
    _write_log(path)
    return path


def _explode(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("cost savings must not talk to the gateway")


def test_savings_reads_the_decision_log_without_the_gateway(
    log_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agentos.cli.cost_cmd.run_gateway_sync", _explode)

    result = runner.invoke(app, ["cost", "savings", "--log-dir", str(log_dir)])

    assert result.exit_code == 0, result.output
    assert "0.25" in result.output


def test_savings_json_carries_the_aggregate_and_route_rows(
    log_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agentos.cli.cost_cmd.run_gateway_sync", _explode)

    result = runner.invoke(app, ["cost", "savings", "--log-dir", str(log_dir), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["routingSavingsUsd"] == 0.25
    assert payload["topTierCostUsd"] == 1.0
    assert payload["savingsPct"] == 25.0
    assert payload["byRoute"][0]["routedModel"] == "glm-5.2"
    assert payload["byRoute"][0]["requestedModel"] == "gpt-5.6-luna"


def test_savings_csv_emits_one_row_per_route_pair(
    log_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agentos.cli.cost_cmd.run_gateway_sync", _explode)

    result = runner.invoke(app, ["cost", "savings", "--log-dir", str(log_dir), "--csv"])

    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert lines[0].startswith("RequestedModel,RoutedModel,Turns")
    assert "gpt-5.6-luna,glm-5.2,1" in lines[1]


def test_savings_pdf_writes_a_branded_report(
    log_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agentos.cli.cost_cmd.run_gateway_sync", _explode)
    out = tmp_path / "report.pdf"

    result = runner.invoke(app, ["cost", "savings", "--log-dir", str(log_dir), "--pdf", str(out)])

    assert result.exit_code == 0, result.output
    assert out.read_bytes().startswith(b"%PDF-")
    assert str(out) in result.output


def test_savings_date_window_is_applied(
    log_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agentos.cli.cost_cmd.run_gateway_sync", _explode)

    result = runner.invoke(
        app,
        [
            "cost",
            "savings",
            "--log-dir",
            str(log_dir),
            "--start-date",
            "2026-09-02",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["turnsRouted"] == 0
    assert payload["routingSavingsUsd"] == 0.0


def test_bare_cost_still_queries_the_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []

    def _fake_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(True)
        return {"breakdown": [], "totalCostUsd": 0.0}

    monkeypatch.setattr("agentos.cli.cost_cmd.run_gateway_sync", _fake_run)

    result = runner.invoke(app, ["cost"])

    assert result.exit_code == 0, result.output
    assert calls == [True]


def test_cost_export_creates_parent_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "breakdown": [
                {
                    "session": "s1",
                    "model": "gpt-5.6-luna",
                    "provider": "openrouter",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cost_usd": 0.01,
                    "created_at": 1700000000,
                }
            ],
            "totalCostUsd": 0.01,
        }

    monkeypatch.setattr("agentos.cli.cost_cmd.run_gateway_sync", _fake_run)

    json_export = tmp_path / "nested" / "reports" / "cost.json"
    result_json = runner.invoke(app, ["cost", "--export", str(json_export)])
    assert result_json.exit_code == 0, result_json.output
    assert json_export.exists()
    payload = json.loads(json_export.read_text(encoding="utf-8"))
    assert payload["totalCostUsd"] == 0.01

    csv_export = tmp_path / "deep" / "nested" / "cost.csv"
    result_csv = runner.invoke(app, ["cost", "--export", str(csv_export)])
    assert result_csv.exit_code == 0, result_csv.output
    assert csv_export.exists()
    assert "gpt-5.6-luna" in csv_export.read_text(encoding="utf-8")

