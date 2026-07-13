"""Tests for cost provenance in structured decision logs."""

from types import SimpleNamespace

from agentos.engine.runtime import TurnRunner
from agentos.engine.types import DoneEvent


def test_decision_log_savings_telemetry_carries_cost_source(monkeypatch) -> None:
    captured = {}

    def fake_write_decision_entry(entry):
        captured["entry"] = entry

    monkeypatch.setattr(
        "agentos.engine.runtime.write_decision_entry",
        fake_write_decision_entry,
    )
    runner = TurnRunner(provider_selector=None)

    runner._emit_decision_entry(
        turn_id="turn-1",
        session_key="agent:main:session",
        session_id="session-id",
        message="hello",
        final_prompt="system",
        tool_defs=[],
        turn_obj=SimpleNamespace(metadata={}),
        provider=SimpleNamespace(),
        resolved_model="claude-opus-4-7",
        turn_started_at=0.0,
        done_event=DoneEvent(
            input_tokens=100,
            output_tokens=10,
            cost_usd=0.004,
            billed_cost=0.004,
            cost_source="provider_billed",
        ),
    )

    entry = captured["entry"]
    assert entry.savings.cost_usd == 0.004
    assert entry.savings.billed_cost_usd == 0.004
    assert entry.savings.cost_source == "provider_billed"


def test_decision_log_normalizes_tokenized_zero_cost_turns_as_unavailable(monkeypatch) -> None:
    captured = {}

    def fake_write_decision_entry(entry):
        captured["entry"] = entry

    monkeypatch.setattr(
        "agentos.engine.runtime.write_decision_entry",
        fake_write_decision_entry,
    )
    runner = TurnRunner(provider_selector=None)

    runner._emit_decision_entry(
        turn_id="turn-2",
        session_key="agent:main:session",
        session_id="session-id",
        message="hello",
        final_prompt="system",
        tool_defs=[],
        turn_obj=SimpleNamespace(metadata={}),
        provider=SimpleNamespace(),
        resolved_model="local/free",
        turn_started_at=0.0,
        done_event=DoneEvent(
            input_tokens=100,
            output_tokens=10,
            cost_usd=0.0,
            billed_cost=0.0,
            cost_source="none",
        ),
    )

    entry = captured["entry"]
    assert entry.savings.cost_usd == 0.0
    assert entry.savings.billed_cost_usd == 0.0
    assert entry.savings.cost_source == "unavailable"
