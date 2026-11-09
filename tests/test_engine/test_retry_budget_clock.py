"""The retry budget counts recovery time only (``provider-attempt-policy`` spec).

``retry_budget_seconds`` bounds the time spent recovering one provider call:
waits plus failed attempts, from the first failure. Time spent on a request
that succeeded before it -- a length-capped continuation, for example -- is
not recovery time and must not use the budget up.
"""

from __future__ import annotations

from typing import Any

from agentos.engine import Agent, AgentConfig
from agentos.engine.types import DoneEvent, ErrorEvent
from tests.fakes import Hang, ScriptedProvider, done, error, text


def _agent(provider: ScriptedProvider) -> Agent:
    return Agent(
        provider=provider,
        config=AgentConfig(retry_budget_seconds=0.5, length_capped_continuations=1),
        session_key="agent:main:webchat:budget",
    )


async def _run(agent: Agent) -> list[Any]:
    return [event async for event in agent.run_turn("go")]


async def test_a_slow_continuation_does_not_use_up_the_retry_budget() -> None:
    provider = ScriptedProvider(
        [
            # 0.4 s of successful streaming, cut by the output cap ...
            [text("part one"), Hang(0.4), done("max_tokens")],
            # ... then the continuation hits a short, hinted 429.
            [error("429", "rate limited", retry_after=0.2)],
            [text(" part two"), done()],
        ]
    )

    events = await _run(_agent(provider))

    assert not [e for e in events if isinstance(e, ErrorEvent)]
    assert len(provider.calls) == 3
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].outcome.kind == "completed"


async def test_waits_beyond_the_budget_still_fail() -> None:
    provider = ScriptedProvider(
        [[error("429", "rate limited", retry_after=0.4)], [text("never"), done()]]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(retry_budget_seconds=0.3),
        session_key="agent:main:webchat:budget",
    )

    events = await _run(agent)

    assert len(provider.calls) == 1
    assert events[-1].outcome.kind == "failed"
