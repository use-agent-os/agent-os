"""Resources a provider stream holds are scoped to the stream, not to each event."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agentos.engine import Agent, AgentConfig
from agentos.engine.turn_progress import TurnStopSignal, bind_turn_stop_signal
from agentos.engine.types import TextDeltaEvent
from tests.fakes import Hang, ScriptedProvider, done, text


async def test_one_abort_waiter_serves_the_whole_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits = 0
    real_wait = TurnStopSignal.wait_abort

    async def counting_wait(self: TurnStopSignal) -> None:
        nonlocal waits
        waits += 1
        await real_wait(self)

    monkeypatch.setattr(TurnStopSignal, "wait_abort", counting_wait)
    provider = ScriptedProvider([[*(text(f"t{i} ") for i in range(40)), done()]])
    agent = Agent(provider=provider, config=AgentConfig())

    async def consume() -> list[Any]:
        bind_turn_stop_signal(TurnStopSignal())
        return [event async for event in agent.run_turn("go")]

    events = await asyncio.create_task(consume())

    assert len([e for e in events if isinstance(e, TextDeltaEvent)]) == 40
    assert waits == 1
    assert events[-1].outcome.kind == "completed"


async def test_closing_the_turn_closes_the_provider_stream_at_once() -> None:
    """``aclose`` on ``run_turn`` reaches the provider stream before it returns,
    not later from the async-generator finalizer."""
    provider = ScriptedProvider([[text("first "), Hang(), text("never"), done()]])
    agent = Agent(provider=provider, config=AgentConfig())
    signal = TurnStopSignal()
    bind_turn_stop_signal(signal)

    turn = agent.run_turn("go")
    async for event in turn:
        if isinstance(event, TextDeltaEvent):
            break
    assert signal.accepts_abort is True
    await turn.aclose()

    assert provider.calls[0].closed_early is True
    assert signal.accepts_abort is False
