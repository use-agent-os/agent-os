"""Every compaction trigger goes through ``CompactionCoordinator`` (issue #3820).

The in-turn triggers are exercised here (preflight, provider overflow,
repeated timeout); the between-turns one is covered in
``test_proactive_compaction.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agentos.engine import Agent, AgentConfig
from agentos.engine.compaction_coordinator import TRIGGERS, CompactionCoordinator
from agentos.engine.compaction_service import CompactionResult, CompactionService
from agentos.engine.types import CompactionEvent, WarningEvent
from agentos.provider import Message
from tests.fakes import ScriptedProvider, done, error, text

OVERFLOW = "This model's maximum context length is 1000 tokens"


@pytest.fixture
def triggers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    real_compact = CompactionCoordinator.compact

    async def spy(self: Any, messages: Any, *, trigger: str, **kwargs: Any) -> Any:
        seen.append(trigger)
        return await real_compact(self, messages, trigger=trigger, **kwargs)

    monkeypatch.setattr(CompactionCoordinator, "compact", spy)
    real_sleep = asyncio.sleep

    async def no_sleep(*args: Any, **kwargs: Any) -> None:
        await real_sleep(0)

    monkeypatch.setattr("agentos.engine.agent.asyncio.sleep", no_sleep)
    return seen


def _history(turns: int = 30) -> list[Message]:
    history: list[Message] = []
    for i in range(turns):
        history.append(Message(role="user", content=f"question {i} " + "x" * 400))
        history.append(Message(role="assistant", content=f"answer {i} " + "y" * 400))
    return history


async def _run(provider: ScriptedProvider, **config: Any) -> list[Any]:
    agent = Agent(provider=provider, config=AgentConfig(**config))
    agent.set_history(_history())
    return [event async for event in agent.run_turn("hi")]


async def test_preflight_trigger(triggers: list[str]) -> None:
    events = await _run(ScriptedProvider([[text("ok"), done()]]), context_window_tokens=3_000)

    # One request, one preflight check; nothing else asked for compaction.
    assert triggers == ["preflight"]
    assert [e.trigger for e in events if isinstance(e, CompactionEvent)] == ["preflight"]


async def test_overflow_trigger(triggers: list[str]) -> None:
    events = await _run(ScriptedProvider([[error("400", OVERFLOW)], [text("ok"), done()]]))

    # preflight before each request, overflow once in between.
    assert triggers == ["preflight", "overflow", "preflight"]
    assert [e.trigger for e in events if isinstance(e, CompactionEvent)] == ["overflow"]


_TIMEOUT_TWICE = [
    [error("timeout", "request timed out")],
    [error("timeout", "request timed out")],
    [text("ok"), done()],
]


async def test_timeout_trigger(triggers: list[str]) -> None:
    # ~3.4k tokens: over half of what a 20k window lets a request reach.
    agent = Agent(
        provider=ScriptedProvider(_TIMEOUT_TWICE), config=AgentConfig(context_window_tokens=20_000)
    )
    agent.set_history(_history(15))
    events = [event async for event in agent.run_turn("hi")]

    assert triggers == ["preflight", "preflight", "timeout", "preflight"]
    assert [e.trigger for e in events if isinstance(e, CompactionEvent)] == ["timeout"]


async def test_timeout_on_a_small_request_does_not_compact(triggers: list[str]) -> None:
    events = await _run(ScriptedProvider(_TIMEOUT_TWICE))

    assert "timeout" not in triggers
    assert not [e for e in events if isinstance(e, CompactionEvent)]
    assert "context_auto_compaction_start" not in [
        e.code for e in events if isinstance(e, WarningEvent)
    ]


async def test_failed_timeout_compaction_moves_on_to_failover(
    triggers: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_compact = CompactionService.compact

    async def fail(self: Any, messages: Any, **kwargs: Any) -> Any:
        if not kwargs.get("force"):
            return await real_compact(self, messages, **kwargs)
        return CompactionResult(
            status="failed",
            messages=messages,
            turn_input_index=kwargs["turn_input_index"],
            reason="summarizer_failed",
        )

    monkeypatch.setattr(CompactionService, "compact", fail)
    provider = ScriptedProvider([[error("timeout", "request timed out")]], offers_fallback=True)
    agent = Agent(provider=provider, config=AgentConfig(context_window_tokens=20_000))
    agent.set_history(_history(15))

    events = [event async for event in agent.run_turn("hi")]

    assert triggers.count("timeout") == 1
    assert provider.fallback_reasons == ["request timed out"]
    assert events[-1].outcome.kind == "interrupted"


class _Recorder:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str, dict[str, Any]]] = []

    def __getattr__(self, level: str) -> Any:
        def record(event: str, **fields: Any) -> None:
            self.entries.append((level, event, fields))

        return record

    def decisions(self) -> list[tuple[str, dict[str, Any]]]:
        return [(lvl, f) for lvl, event, f in self.entries if event == "compaction.decision"]


@pytest.fixture
def decision_log(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr("agentos.engine.compaction_coordinator.log", recorder)
    return recorder


async def test_under_threshold_decision_is_logged_at_debug(decision_log: _Recorder) -> None:
    agent = Agent(provider=ScriptedProvider([[text("ok"), done()]]), config=AgentConfig())
    agent.set_history(_history(1))

    [event async for event in agent.run_turn("hi")]

    decisions = decision_log.decisions()
    assert decisions
    assert {fields["reason"] for _, fields in decisions} == {"under_threshold"}
    assert {level for level, _ in decisions} == {"debug"}


async def test_a_real_compaction_decision_is_logged_at_info(decision_log: _Recorder) -> None:
    await _run(ScriptedProvider([[text("ok"), done()]]), context_window_tokens=3_000)

    assert [level for level, _ in decision_log.decisions()] == ["info"]


async def test_unknown_trigger_is_rejected() -> None:
    coordinator = CompactionCoordinator(AgentConfig(), provider=None, session_key=None)

    with pytest.raises(ValueError):
        await coordinator.compact([], trigger="whenever")  # type: ignore[arg-type]
    assert set(TRIGGERS) == {"preflight", "overflow", "timeout", "proactive"}
