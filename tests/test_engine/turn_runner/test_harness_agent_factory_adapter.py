"""Tests for TurnRunner harness adapters."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from agentos.engine.runtime import TurnRunner


def test_agent_factory_adapter_passes_runner_tool_registry(monkeypatch) -> None:
    """Meta-skill execution needs the per-runner registry on constructed Agents."""

    captured: dict[str, Any] = {}

    class RecordingAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    import agentos.engine.agent as agent_module

    monkeypatch.setattr(agent_module, "Agent", RecordingAgent)

    registry = object()

    def _check_spend_budget(session_key: str) -> tuple[bool, str | None]:
        return False, None

    runner = SimpleNamespace(
        _tool_registry=registry,
        _usage_tracker=None,
        _session_flush_service=None,
        _check_spend_budget=_check_spend_budget,
    )

    TurnRunner.build_agent(
        runner,
        provider=object(),
        config=object(),
        tool_definitions=[],
        tool_handler=None,
        session_key="agent:main:test",
        turn_call_logger=None,
        memory_sync_manager=None,
        tool_context=None,
    )

    assert captured["tool_registry"] is registry
    # The constructed Agent re-checks spend ceilings between iterations, so the
    # runner's guard has to reach it.
    assert captured["spend_budget_guard"] is _check_spend_budget
