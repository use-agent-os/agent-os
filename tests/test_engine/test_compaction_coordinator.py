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
    await _run(ScriptedProvider([[text("ok"), done()]]), context_window_tokens=3_000)

    assert "preflight" in triggers


async def test_overflow_trigger(triggers: list[str]) -> None:
    await _run(ScriptedProvider([[error("400", OVERFLOW)], [text("ok"), done()]]))

    assert "overflow" in triggers


async def test_timeout_trigger(triggers: list[str]) -> None:
    await _run(
        ScriptedProvider(
            [
                [error("timeout", "request timed out")],
                [error("timeout", "request timed out")],
                [text("ok"), done()],
            ]
        )
    )

    assert "timeout" in triggers


async def test_unknown_trigger_is_rejected() -> None:
    coordinator = CompactionCoordinator(AgentConfig(), provider=None, session_key=None)

    with pytest.raises(ValueError):
        await coordinator.compact([], trigger="whenever")  # type: ignore[arg-type]
    assert set(TRIGGERS) == {"preflight", "overflow", "timeout", "proactive"}
