"""Contract: how a turn recovers from provider failures.

One test per scenario of the ``provider-attempt-recovery`` spec (OpenSpec
change ``refactor-agent-harness-core``). ``asyncio.sleep`` inside the agent is
replaced by a recorder, so the sum of recorded delays is the turn's virtual
wall clock and the suite never waits for real backoff. Gaps that exist today
are ``xfail(strict=True)`` and name the phase that closes them.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agentos.engine import Agent, AgentConfig
from agentos.engine.fallback import FallbackPolicy, ProviderErrorKind, backoff_sleep
from agentos.engine.types import DoneEvent, ErrorEvent
from agentos.provider import Message
from tests.fakes import ChainSelector, ScriptedProvider, done, error, text

OVERFLOW = "This model's maximum context length is 1000 tokens"


class _CallLog:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def write(self, kind: str, payload: dict[str, Any]) -> None:
        self.records.append((kind, payload))

    def recovery_decisions(self) -> list[dict[str, Any]]:
        return [
            payload
            for kind, payload in self.records
            if kind == "turn_policy_decision" and "failure_kind" in payload
        ]


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Delays the agent asked to sleep; their sum is the virtual elapsed time."""

    recorded: list[float] = []
    real_sleep = asyncio.sleep

    async def _record(delay: float, *args: Any, **kwargs: Any) -> None:
        if delay > 0:
            recorded.append(delay)
        await real_sleep(0)

    monkeypatch.setattr("agentos.engine.agent.asyncio.sleep", _record)
    return recorded


def _agent(provider: Any, log: _CallLog | None = None, **config: Any) -> Agent:
    return Agent(provider=provider, config=AgentConfig(**config), turn_call_logger=log)


async def _run(agent: Agent, message: str = "hi") -> list[Any]:
    return [event async for event in agent.run_turn(message)]


def _long_history(turns: int = 30) -> list[Message]:
    history: list[Message] = []
    for i in range(turns):
        history.append(Message(role="user", content=f"question {i} " + "x" * 400))
        history.append(Message(role="assistant", content=f"answer {i} " + "y" * 400))
    return history


def _errors(events: list[Any]) -> list[ErrorEvent]:
    return [event for event in events if isinstance(event, ErrorEvent)]


def _text(events: list[Any]) -> str:
    return "".join(getattr(e, "text", "") for e in events if e.kind == "text_delta")


def _outcome(event: Any) -> str | None:
    outcome = getattr(event, "outcome", None)
    # ``engine.outcome.TurnOutcome``: the kind persisted as ``details.turn_outcome``.
    return None if outcome is None else str(getattr(outcome, "kind", outcome))


# ------------------------------------------------ one decision per failed attempt


async def test_each_failed_attempt_records_exactly_one_recovery_decision(
    sleeps: list[float],
) -> None:
    provider = ScriptedProvider(
        [[error("503", "busy")], [error("503", "busy")], [text("ok"), done()]]
    )
    log = _CallLog()

    await _run(_agent(provider, log))

    decisions = log.recovery_decisions()
    assert len(decisions) == 2
    assert all(d["failure_kind"] and d["action"] for d in decisions)


# ------------------------------------------------ overloaded is a distinct kind


def test_529_is_classified_as_overloaded_not_transport() -> None:
    kind = FallbackPolicy.classify_error("overloaded", status_code=529, raw_code="529")

    assert kind is ProviderErrorKind.OVERLOADED


# ------------------------------------------------ wall-clock bounded retries


async def test_burst_of_429s_keeps_retrying_within_the_default_budget(
    sleeps: list[float],
) -> None:
    provider = ScriptedProvider([[error("429", "rate limited")]])

    events = await _run(_agent(provider))

    assert len(provider.calls) >= 6
    assert sum(sleeps) <= 90 + 30
    assert _errors(events)


async def test_configured_retry_budget_bounds_same_model_retries(sleeps: list[float]) -> None:
    provider = ScriptedProvider([[error("429", "rate limited")]])

    await _run(_agent(provider, retry_budget_seconds=30, max_provider_retries=50))

    assert sum(sleeps[:-1]) < 30
    assert len(provider.calls) < 51


# ------------------------------------------------ Retry-After is a minimum delay


async def test_short_retry_after_is_waited_out_on_the_same_model(sleeps: list[float]) -> None:
    provider = ScriptedProvider([[error("429", "slow down", retry_after=5)], [text("ok"), done()]])

    events = await _run(_agent(provider, rate_limit_retry_after_cap_seconds=30))

    assert len(provider.calls) == 2
    assert sleeps and sleeps[0] >= 5
    assert _text(events) == "ok"


async def test_long_retry_after_fails_over_without_waiting(sleeps: list[float]) -> None:
    chain = ChainSelector(
        {
            "primary": ScriptedProvider([[error("429", "slow down", retry_after=120)]]),
            "backup": ScriptedProvider([[text("from backup"), done()]]),
        }
    )

    events = await _run(_agent(chain.wrapped(), rate_limit_retry_after_cap_seconds=30))

    assert chain.failovers == ["backup"]
    assert all(delay < 120 for delay in sleeps)
    assert _text(events) == "from backup"


# ------------------------------------------------ multiplicative jitter


def test_backoff_jitter_spreads_retries_multiplicatively() -> None:
    nominal = 4.0  # attempt 2 at a 1 s base
    samples = [backoff_sleep(2, 1000, 30_000, _fake=True) for _ in range(400)]

    assert min(samples) >= 0.5 * nominal
    assert max(samples) <= 1.5 * nominal
    assert min(samples) < 0.8 * nominal
    assert max(samples) > 1.2 * nominal


# ------------------------------------------------ overflow compacts then retries


async def test_context_overflow_is_compacted_and_retried(sleeps: list[float]) -> None:
    provider = ScriptedProvider([[error("400", OVERFLOW)], [text("ok"), done()]])
    agent = _agent(provider)
    agent.set_history(_long_history())

    events = await _run(agent)

    assert len(provider.calls) == 2
    assert len(provider.calls[1].messages) < len(provider.calls[0].messages)
    assert any(e.kind == "compaction" for e in events)
    assert isinstance(events[-1], DoneEvent)
    assert _text(events) == "ok"


async def test_overflow_that_compaction_cannot_shrink_fails_with_an_explanation(
    sleeps: list[float],
) -> None:
    provider = ScriptedProvider([[error("400", OVERFLOW)]])

    events = await _run(_agent(provider))

    assert len(provider.calls) == 1
    (failure,) = _errors(events)
    assert "did not reduce" in failure.message or "could not" in failure.message


async def test_overflow_that_cannot_shrink_ends_with_done_failed(sleeps: list[float]) -> None:
    provider = ScriptedProvider([[error("400", OVERFLOW)]])

    events = await _run(_agent(provider))

    assert isinstance(events[-1], DoneEvent)
    assert _outcome(events[-1]) == "failed"


# ------------------------------------------------ timeouts reduce context first


async def test_timeout_on_large_context_compacts_and_retries_before_failing(
    sleeps: list[float],
) -> None:
    provider = ScriptedProvider(
        [
            [error("timeout", "request timed out")],
            [error("timeout", "request timed out")],
            [text("ok"), done()],
        ]
    )
    agent = _agent(provider)
    agent.set_history(_long_history())

    events = await _run(agent)

    assert any(e.kind == "compaction" for e in events)
    assert len(provider.calls[-1].messages) < len(provider.calls[0].messages)
    assert _text(events) == "ok"


# ------------------------------------------------ failover only before output


async def test_failure_after_visible_output_does_not_fail_over(sleeps: list[float]) -> None:
    chain = ChainSelector(
        {
            "primary": ScriptedProvider([[text("partial "), error("503", "dropped")]]),
            "backup": ScriptedProvider([[text("from backup"), done()]]),
        }
    )

    events = await _run(_agent(chain.wrapped(), max_provider_retries=0))

    assert chain.failovers == []
    assert "from backup" not in _text(events)
    assert "partial" in _text(events)


# ------------------------------------------------ non-recoverable fail fast


async def test_invalid_api_key_is_not_retried(sleeps: list[float]) -> None:
    provider = ScriptedProvider([[error("401", "invalid api key")]])

    events = await _run(_agent(provider))

    assert len(provider.calls) == 1
    assert sleeps == []
    assert _errors(events)


async def test_invalid_api_key_error_tells_the_operator_to_fix_credentials(
    sleeps: list[float],
) -> None:
    provider = ScriptedProvider([[error("401", "invalid api key")]])

    events = await _run(_agent(provider))

    message = _errors(events)[-1].message.lower()
    assert "credential" in message or ("api key" in message and "config" in message)


async def test_timeout_recovery_disabled_keeps_the_previous_failure(
    sleeps: list[float],
) -> None:
    provider = ScriptedProvider([[error("timeout", "request timed out")]])
    agent = _agent(provider, timeout_context_recovery=False)
    agent.set_history(_long_history())

    events = await _run(agent)

    assert not any(e.kind == "compaction" for e in events)
    assert len(provider.calls) == 2
    message = _errors(events)[-1].message
    assert "/c0" in message
    # ``timeout`` is an interrupted outcome in agentos.engine.outcome.
    assert _outcome(events[-1]) == "interrupted"
