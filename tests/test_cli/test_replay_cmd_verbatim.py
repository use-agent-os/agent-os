"""``agentos replay`` prints the recorded transcript as it was recorded.

Rich parses ``[...]`` as markup in everything ``console.print`` renders. The
replay command handed it the transcript string directly, so a session key
containing a ``[/]``-shaped sequence raised ``MarkupError`` and the whole
command died, and a ``[redacted]``-shaped sequence silently vanished from the
output. The same defect class was fixed at the other CLI render sites
(#2822, #2824); these are the replay render sites.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from agentos.cli import replay as replay_cmd
from agentos.cli.main import app
from agentos.observability.decision_log import DecisionEntry, PipelineStepRecord

runner = CliRunner()


def _entry(session_key: str, turn_id: str = "turn-1") -> DecisionEntry:
    return DecisionEntry(
        turn_id=turn_id,
        session_key=session_key,
        prompt_hash="p",
        system_prompt_hash="s",
        tool_list_hash="t",
        tool_choice="auto",
        tokens_input=10,
        tokens_output=20,
        model="test/model",
        provider="test",
        latency_ms=5,
        ts="2026-09-22T00:00:00Z",
        pipeline_steps=[PipelineStepRecord(step_name="router", applied=True)],
    )


@pytest.fixture
def replay_from_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(replay_cmd, "load_turn", lambda session, turn: _entry(session, turn))


def test_a_closing_tag_shaped_session_is_printed_verbatim(
    replay_from_log: None,
) -> None:
    result = runner.invoke(app, ["replay", "--session", "release[/]", "--turn", "turn-1"])

    assert result.exit_code == 0, result.output
    assert "(session release[/])" in result.stdout


def test_a_bracketed_token_shaped_session_is_printed_verbatim(
    replay_from_log: None,
) -> None:
    result = runner.invoke(app, ["replay", "--session", "release [redacted]", "--turn", "turn-1"])

    assert result.exit_code == 0, result.output
    assert "(session release [redacted])" in result.stdout


def test_a_bracketed_turn_id_is_printed_verbatim(replay_from_log: None) -> None:
    result = runner.invoke(app, ["replay", "--session", "plain", "--turn", "turn[/]1"])

    assert result.exit_code == 0, result.output
    assert "Turn turn[/]1" in result.stdout


def test_the_not_found_message_names_the_arguments_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(replay_cmd, "load_turn", lambda session, turn: None)

    result = runner.invoke(app, ["replay", "--session", "release[/]", "--turn", "turn-1"])

    assert result.exit_code == 1
    assert "release[/]" in result.stdout
