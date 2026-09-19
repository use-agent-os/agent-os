"""``agentos cost --csv`` and ``cost savings --csv`` print CSV a parser can read.

Both printed the CSV through the Rich console, which wraps a stdout that is not
a terminal at 80 columns. A row longer than that -- any row with a Telegram topic
session key, or an OpenRouter-style ``vendor/model`` id -- was split into two
records, so ``agentos cost --csv > usage.csv`` wrote a file whose rows no longer
line up with its header. ``CliRunner`` output is not a terminal either.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agentos.cli.main import app

runner = CliRunner()

_HEADER = [
    "Session",
    "Model",
    "Provider",
    "Agent",
    "Channel",
    "Tool",
    "Skill",
    "Input",
    "Output",
    "Cost",
    "CreatedAt",
]
_ROWS = [
    {
        "sessionKey": "agent:main:telegram:group:-1001234567890:topic:42",
        "model": "claude-sonnet-5",
        "provider": "anthropic",
        "agentId": "main",
        "channelType": "telegram",
        "toolName": "web_fetch",
        "skill": "deep-research",
        "inputTokens": 18234,
        "outputTokens": 1422,
        "costUsd": 0.0761,
        "createdAt": 1758240000000,
    },
    {
        "sessionKey": "agent:main:main",
        "model": "gpt-5.5",
        "provider": "openai",
        "agentId": "main",
        "inputTokens": 900,
        "outputTokens": 120,
        "costUsd": 0.0031,
        "createdAt": 1758240100000,
    },
]


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"breakdown": _ROWS, "totalCostUsd": 0.0792}

    monkeypatch.setattr("agentos.cli.cost_cmd.run_gateway_sync", _fake_run)


def _records(output: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(output)))


def test_cost_csv_keeps_every_row_one_record(gateway: None) -> None:
    result = runner.invoke(app, ["cost", "--csv"])

    assert result.exit_code == 0, result.output
    assert _records(result.output) == [
        _HEADER,
        [
            "agent:main:telegram:group:-1001234567890:topic:42",
            "claude-sonnet-5",
            "anthropic",
            "main",
            "telegram",
            "web_fetch",
            "deep-research",
            "18234",
            "1422",
            "0.0761",
            "1758240000000",
        ],
        [
            "agent:main:main",
            "gpt-5.5",
            "openai",
            "main",
            "",
            "",
            "",
            "900",
            "120",
            "0.0031",
            "1758240100000",
        ],
    ]


def test_cost_csv_matches_the_export_file(gateway: None, tmp_path: Path) -> None:
    """--export writes the file directly and was always intact; stdout must match it."""
    export = tmp_path / "usage.csv"
    exported = runner.invoke(app, ["cost", "--export", str(export)])
    printed = runner.invoke(app, ["cost", "--csv"])

    assert exported.exit_code == 0 and printed.exit_code == 0
    # Blank records are dropped: a text-mode write of csv's \r\n on Windows reads
    # back as an extra empty line, which is the export file's business, not this one.
    exported_records = [r for r in _records(export.read_text(encoding="utf-8")) if r]
    assert _records(printed.output) == exported_records


def _write_savings_log(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "turn_id": "t1",
        "session_key": "s1",
        "prompt_hash": "a" * 16,
        "system_prompt_hash": "b" * 16,
        "tool_list_hash": "c" * 16,
        "tool_choice": "auto",
        "tokens_input": 1000,
        "tokens_output": 100,
        "model": "deepseek/deepseek-v4-chat-preview",
        "provider": "openrouter",
        "latency_ms": 900,
        "ts": "2026-09-01T10:00:00Z",
        "savings": {
            "routed_model": "deepseek/deepseek-v4-chat-preview",
            "baseline_model": "anthropic/claude-opus-5-thinking",
            "routing_confidence": 0.6,
            "routing_savings_pct": 20.0,
            "routing_savings_usd_estimated_vs_baseline": 0.25,
            "cost_usd": 0.75,
        },
    }
    (log_dir / "decisions-20260901.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")


def test_savings_csv_keeps_a_long_route_row_one_record(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    _write_savings_log(log_dir)

    result = runner.invoke(app, ["cost", "savings", "--log-dir", str(log_dir), "--csv"])

    assert result.exit_code == 0, result.output
    header, *rows = _records(result.output)
    assert header == [
        "RequestedModel",
        "RoutedModel",
        "Turns",
        "AvgSavingsPct",
        "AvgConfidence",
        "SavedUsd",
    ]
    assert rows == [
        [
            "anthropic/claude-opus-5-thinking",
            "deepseek/deepseek-v4-chat-preview",
            "1",
            "20.00",
            "0.6000",
            "0.250000",
        ]
    ]
