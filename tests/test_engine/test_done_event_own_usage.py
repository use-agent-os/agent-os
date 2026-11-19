"""DoneEvent's own usage is the turn's own, whatever else the session did.

A turn reports the session usage tracker's delta over its run as
``input_tokens`` / ``output_tokens``. A background memory review runs as a
turn of the same session and usually overlaps the user's next turn, so each
delta also holds the other turn's calls. ``own_input_tokens`` /
``own_output_tokens`` hold only the turn's own provider calls; the review log
reports those.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from agentos.engine import Agent, AgentConfig
from agentos.engine.usage import UsageTracker
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import TextDeltaEvent as ProviderText

SESSION = "agent:test:own-usage"


class _OverlappedProvider:
    """Answers once; while it streams, another turn of the session spends tokens."""

    provider_name = "fake"

    def __init__(self, tracker: UsageTracker) -> None:
        self.tracker = tracker

    def chat(self, messages: Any, tools: Any = None, config: Any = None) -> AsyncIterator[Any]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderText(text="ok")
        self.tracker.add(SESSION, input_tokens=5000, output_tokens=70, model_id="gpt-test")
        yield ProviderDone(stop_reason="stop", input_tokens=120, output_tokens=8, model="gpt-test")

    async def list_models(self) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_own_usage_leaves_out_a_concurrent_turn_of_the_same_session() -> None:
    tracker = UsageTracker()
    agent = Agent(
        provider=_OverlappedProvider(tracker),
        config=AgentConfig(),
        usage_tracker=tracker,
        session_key=SESSION,
    )

    events = [event async for event in agent.run_turn("hello")]

    done = next(event for event in events if event.kind == "done")
    assert (done.own_input_tokens, done.own_output_tokens) == (120, 8)
    # The session delta still reports everything the session spent meanwhile.
    assert (done.input_tokens, done.output_tokens) == (5120, 78)
