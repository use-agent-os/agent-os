"""RoutingSource surface: the pilot telemetry tags round-trip through the log.

``"pilot_v1"`` (healthy) and ``"pilot_unavailable"`` (degraded) are valid
``RoutingSource`` literal values, so a ``PipelineStepRecord`` carrying either
persists and reloads unchanged.
"""

from __future__ import annotations

import typing
from pathlib import Path

from agentos.observability.decision_log import (
    DecisionEntry,
    PipelineStepRecord,
    RoutingSource,
    load_entries,
    write_decision_entry,
)


def _make_entry(**overrides) -> DecisionEntry:
    defaults = dict(
        turn_id="t1",
        session_key="s1",
        prompt_hash="a" * 16,
        system_prompt_hash="b" * 16,
        tool_list_hash="c" * 16,
        tool_choice="auto",
        tokens_input=10,
        tokens_output=20,
        model="claude",
        provider="anthropic",
        latency_ms=100,
        ts="2026-05-20T00:00:00Z",
    )
    defaults.update(overrides)
    return DecisionEntry(**defaults)


def test_pilot_sources_are_valid_routing_source_literals() -> None:
    allowed = set(typing.get_args(RoutingSource))
    assert "pilot_v1" in allowed
    assert "pilot_unavailable" in allowed


def test_pilot_v1_source_round_trips(tmp_path: Path) -> None:
    entry = _make_entry(
        pipeline_steps=[
            PipelineStepRecord(
                step_name="agentos_router",
                applied=True,
                routed_tier="c1",
                routing_source="pilot_v1",
                confidence=0.83,
            )
        ]
    )
    write_decision_entry(entry, log_dir=tmp_path)
    loaded = load_entries(next(tmp_path.glob("decisions-*.jsonl")))
    assert len(loaded) == 1
    assert loaded[0].pipeline_steps[0].routing_source == "pilot_v1"


def test_pilot_unavailable_source_round_trips(tmp_path: Path) -> None:
    entry = _make_entry(
        pipeline_steps=[
            PipelineStepRecord(
                step_name="agentos_router",
                applied=True,
                routed_tier="c1",
                routing_source="pilot_unavailable",
                confidence=0.0,
            )
        ]
    )
    write_decision_entry(entry, log_dir=tmp_path)
    loaded = load_entries(next(tmp_path.glob("decisions-*.jsonl")))
    assert len(loaded) == 1
    assert loaded[0].pipeline_steps[0].routing_source == "pilot_unavailable"
