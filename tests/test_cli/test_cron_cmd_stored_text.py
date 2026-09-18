"""``agentos cron …`` shows job and run data as it is stored.

Rich parses ``[...]`` as markup and ``:name:`` as an emoji code in a plain
``str`` table cell and in ``console.print``. Job names, prompts, run output and
error text come from users, models and scripts, so ``[daily] standup`` printed
as `` standup``, a ``- [x]`` checkbox lost its ``[x]``, and a ``[/]`` anywhere
raised ``MarkupError`` and took the command down. ``cron output`` also let the
console hard-wrap a long line, which split a JSON string in a piped copy.

The gateway transport is stubbed; everything from the payload onwards is the
real command.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from typer.testing import CliRunner

from agentos.cli import cron_cmd

runner = CliRunner()

# Wide enough that no table cell wraps: the assertions look for whole strings.
_WIDE = {"COLUMNS": "240"}


class _Gateway:
    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.methods: list[str] = []

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        self.methods.append(method)
        return self.reply


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch):
    def install(reply: Any) -> _Gateway:
        client = _Gateway(reply)
        monkeypatch.setattr(
            cron_cmd,
            "run_gateway_sync",
            lambda fn, **_kw: asyncio.run(fn(client)),
        )
        return client

    return install


def _run(*argv: str, env: dict[str, str] | None = None) -> str:
    result = runner.invoke(cron_cmd.cron_app, list(argv), env=_WIDE if env is None else env)
    assert result.exit_code == 0, (result.output, result.exception)
    return result.output


def _job(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "a1b2",
        "name": "standup digest",
        "enabled": True,
        "expression": "0 9 * * 1-5",
        "payloadKind": "agent_turn",
        "agentId": "main",
        **overrides,
    }


# ── cron list ────────────────────────────────────────────────────────────────


def test_cron_list_keeps_a_bracketed_job_name(gateway) -> None:
    gateway({"jobs": [_job(name="[daily] standup digest :warning:")]})

    out = _run("list")

    assert "[daily] standup digest :warning:" in out


def test_cron_list_survives_a_closing_tag_in_a_job_name(gateway) -> None:
    gateway({"jobs": [_job(name="backup [/] nightly")]})

    out = _run("list")

    assert "backup [/] nightly" in out


def test_cron_list_still_renders_a_plain_job(gateway) -> None:
    """Positive control: an ordinary job lists with every column filled in."""
    client = gateway({"jobs": [_job()]})

    out = _run("list")

    assert client.methods == ["cron.list"]
    for value in ("a1b2", "standup digest", "0 9 * * 1-5", "agent_turn", "main"):
        assert value in out


# ── cron status ──────────────────────────────────────────────────────────────


def test_cron_status_keeps_brackets_in_the_job_fields(gateway) -> None:
    gateway(_job(name="[daily] standup digest", text="Check [ops] and list[int] items"))

    out = _run("status", "a1b2")

    assert "[daily] standup digest" in out
    assert "Check [ops] and list[int] items" in out


def test_cron_status_survives_a_closing_tag(gateway) -> None:
    gateway(_job(name="backup [/] nightly"))

    out = _run("status", "a1b2")

    assert "backup [/] nightly" in out


# ── cron runs ────────────────────────────────────────────────────────────────


def test_cron_runs_keeps_checkboxes_and_emoji_codes_in_the_output(gateway) -> None:
    gateway([{"id": "r1", "status": "ok", "summary": "- [x] deploy done - [ ] review :warning:"}])

    out = _run("runs", "a1b2")

    assert "- [x] deploy done - [ ] review :warning:" in out


def test_cron_runs_survives_a_closing_tag_in_the_error(gateway) -> None:
    gateway([{"id": "r2", "status": "error", "error": "grep: Unmatched [/] in pattern"}])

    out = _run("runs", "a1b2")

    assert "grep: Unmatched [/] in pattern" in out


# ── cron output ──────────────────────────────────────────────────────────────


def test_cron_output_survives_a_closing_tag_in_the_error(gateway) -> None:
    gateway({"runId": "r2", "error": "grep: Unmatched [/] in pattern", "output": ""})

    out = _run("output", "a1b2")

    assert "error: grep: Unmatched [/] in pattern" in out
    assert "(no output)" in out


def test_cron_output_keeps_emoji_codes_in_the_output(gateway) -> None:
    gateway({"runId": "r1", "error": None, "output": "disk 91% :warning: on [db-1]"})

    out = _run("output", "a1b2")

    assert out == "disk 91% :warning: on [db-1]\n"


def test_cron_output_prints_a_long_json_line_byte_for_byte(gateway) -> None:
    """A redirected stdout is not a terminal, so Rich wrapped it at 80 columns."""
    stdout = json.dumps({"items": [{"title": "Q3", "url": "https://example.com/" + "a" * 90}]})
    gateway({"runId": "r1", "error": None, "output": stdout})

    out = _run("output", "a1b2", env={"COLUMNS": "80"})

    assert out == stdout + "\n"
    assert json.loads(out) == json.loads(stdout)
