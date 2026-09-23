"""``agentos replay`` renders the decision-log transcript as it was recorded.

Rich parses ``[...]`` as markup and a stray ``[/...]`` closing tag raises
``MarkupError``. A session key set via ``agentos chat --session <id>`` (a
free-form CLI argument) or a router ``fallback_reason`` free-text string can
both contain brackets, and neither the "no entry found" error line nor the
transcript itself escaped or disabled markup before this fix.
"""

from __future__ import annotations

from typer.testing import CliRunner

from agentos.cli import replay as replay_cmd
from agentos.observability.decision_log import DecisionEntry, PipelineStepRecord

runner = CliRunner()


def _entry(**overrides: object) -> DecisionEntry:
    defaults: dict[str, object] = {
        "turn_id": "t1",
        "session_key": "cli:main:default",
        "prompt_hash": "ph",
        "system_prompt_hash": "sph",
        "tool_list_hash": "tlh",
        "tool_choice": "auto",
        "tokens_input": 10,
        "tokens_output": 5,
        "model": "gpt-x",
        "provider": "openrouter",
        "latency_ms": 123,
        "ts": "2026-09-22T00:00:00Z",
        "pipeline_steps": [],
    }
    defaults.update(overrides)
    return DecisionEntry(**defaults)  # type: ignore[arg-type]


def test_replay_survives_a_closing_tag_in_the_session_key(monkeypatch) -> None:
    entry = _entry(session_key="release[/]")
    monkeypatch.setattr(replay_cmd, "load_turn", lambda *_a, **_kw: entry)

    result = runner.invoke(replay_cmd.replay_app, ["--session", "release[/]", "--turn", "t1"])

    assert result.exit_code == 0, (result.output, result.exception)
    assert "release[/]" in result.output


def test_replay_keeps_a_bracketed_fallback_reason(monkeypatch) -> None:
    step = PipelineStepRecord(
        step_name="agentos_router",
        applied=False,
        fallback_reason="jev_unavailable: http 429: rate limited [/quota]",
    )
    entry = _entry(pipeline_steps=[step])
    monkeypatch.setattr(replay_cmd, "load_turn", lambda *_a, **_kw: entry)

    result = runner.invoke(replay_cmd.replay_app, ["--session", "s", "--turn", "t1"])

    assert result.exit_code == 0, (result.output, result.exception)
    assert "rate limited [/quota]" in result.output


def test_replay_no_entry_message_survives_a_closing_tag_in_the_argument(monkeypatch) -> None:
    monkeypatch.setattr(replay_cmd, "load_turn", lambda *_a, **_kw: None)

    result = runner.invoke(replay_cmd.replay_app, ["--session", "[/]", "--turn", "t1"])

    assert result.exit_code == 1, (result.output, result.exception)
    assert "session=[/]" in result.output


def test_replay_still_renders_a_plain_transcript(monkeypatch) -> None:
    """Positive control: an ordinary entry renders every field."""
    entry = _entry()
    monkeypatch.setattr(replay_cmd, "load_turn", lambda *_a, **_kw: entry)

    result = runner.invoke(replay_cmd.replay_app, ["--session", "cli:main:default", "--turn", "t1"])

    assert result.exit_code == 0, (result.output, result.exception)
    for value in ("t1", "cli:main:default", "gpt-x", "openrouter", "auto"):
        assert value in result.output
