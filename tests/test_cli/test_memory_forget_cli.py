"""``agentos memory forget``: params sent, ``--json`` output and exit codes.

The gateway transport is stubbed; everything from the payload onwards is the
real command.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from agentos.cli.gateway_client import GatewayRPCError
from agentos.cli.main import app

runner = CliRunner()


def _report(**overrides: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "agentId": "main",
        "dryRun": False,
        "sessions": [
            {
                "selector": "s1-uuid",
                "matchedBy": "session_id",
                "sessionKey": "agent:main:webchat:direct:s1",
                "sessionIds": ["s1-uuid"],
                "alreadyForgotten": False,
            }
        ],
        "curated": [{"target": "memory", "entry": "Deploy - [x] on Friday", "mixed": True}],
        "mixed": [],
        "notes": [{"path": "memory/n.md", "itemHash": "h", "chars": 5, "occurrences": 1}],
        "unmatched": [{"path": "memory/m.md", "itemHash": "g", "reason": "changed"}],
        "sessionDocuments": ["sessions/main/s1-uuid.md"],
        "staleOrigins": 0,
        "untracked": {"memory": 1, "user": 0},
        "providerMirrored": False,
        "errors": [],
        "notCovered": "Forget does not remove session transcripts.",
    }
    report.update(overrides)
    return report


class _Gateway:
    response: Any = None
    calls: list[tuple[str, dict | None]] = []

    async def connect(self, url: str, *, token: Any = None) -> None:
        return None

    async def close(self) -> None:
        return None

    async def call(self, method: str, params: dict | None = None) -> Any:
        type(self).calls.append((method, params))
        response = type(self).response
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> type[_Gateway]:
    _Gateway.response = _report()
    _Gateway.calls = []
    monkeypatch.setattr("agentos.cli.gateway_client.GatewayClient", _Gateway)
    return _Gateway


def test_forget_sends_every_selector_and_prints_the_report_as_json(
    gateway: type[_Gateway],
) -> None:
    result = runner.invoke(
        app,
        [
            "memory",
            "forget",
            "--session",
            "s1-uuid",
            "-s",
            "agent:main:webchat:direct:s2",
            "--agent",
            "ops",
            "--json",
        ],
    )

    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout) == _report()
    assert gateway.calls == [
        (
            "memory.forget",
            {
                "agentId": "ops",
                "sessions": ["s1-uuid", "agent:main:webchat:direct:s2"],
                "dryRun": False,
            },
        )
    ]


def test_dry_run_flag_is_forwarded(gateway: type[_Gateway]) -> None:
    gateway.response = _report(dryRun=True)

    result = runner.invoke(app, ["memory", "forget", "-s", "s1-uuid", "--dry-run", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["dryRun"] is True
    assert gateway.calls[0][1]["dryRun"] is True  # type: ignore[index]


def test_an_unresolved_selector_exits_non_zero_naming_it(gateway: type[_Gateway]) -> None:
    gateway.response = GatewayRPCError(
        "memory.forget",
        code="NOT_FOUND",
        message="Unresolved session selector(s): '3f2a'.",
        data={"agentId": "main", "unresolved": [{"selector": "3f2a", "reason": "not_found"}]},
    )

    result = runner.invoke(app, ["memory", "forget", "-s", "3f2a", "--json"])

    assert result.exit_code == 2
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert "3f2a" in json.dumps(error)


def test_a_report_with_errors_exits_one_and_still_prints_it(gateway: type[_Gateway]) -> None:
    errors = [{"stage": "curated", "target": "memory", "message": "Refusing to write MEMORY.md"}]
    gateway.response = _report(errors=errors)

    result = runner.invoke(app, ["memory", "forget", "-s", "s1-uuid", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["errors"] == errors


def test_session_is_required(gateway: type[_Gateway]) -> None:
    result = runner.invoke(app, ["memory", "forget", "--json"])

    assert result.exit_code != 0
    assert gateway.calls == []


def test_human_output_prints_stored_text_verbatim(gateway: type[_Gateway]) -> None:
    result = runner.invoke(app, ["memory", "forget", "-s", "s1-uuid"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stdout
    assert "memory: Deploy - [x] on Friday [mixed: also from other sessions]" in result.stdout
    assert "unmatched memory/m.md: changed" in result.stdout
    assert "Forget does not remove session transcripts." in result.stdout
