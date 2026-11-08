"""A short ``Retry-After`` on a 429 is waited out on the same model (#3646).

Before this, the selector-fallback wrapper failed a hinted 429 straight over to
another model (losing the prompt cache and changing the cost), counted it
toward the provider's circuit breaker, and an agent with no fallback slept a
blind exponential backoff instead of the wait the provider named.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agentos.engine import Agent, AgentConfig, WarningEvent
from agentos.engine.fallback import retry_after_delay
from agentos.engine.runtime import _SelectorFallbackProvider
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import ErrorEvent as ProviderError
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.provider.circuit_breaker import (
    BreakerSettings,
    BreakerState,
    ProviderCircuitBreaker,
)
from agentos.provider.failures import ProviderFailureKind, honoured_retry_after

_REAL_SLEEP = asyncio.sleep


class _StubProvider:
    def __init__(self, name: str, streams: list[list[Any]]) -> None:
        self.provider_name = name
        self._streams = streams
        self.calls = 0

    def chat(self, messages: list[Any], tools: Any = None, config: Any = None) -> AsyncIterator:
        index = min(self.calls, len(self._streams) - 1)
        self.calls += 1
        return self._stream(self._streams[index])

    async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
        for event in events:
            yield event

    async def list_models(self) -> list[Any]:
        return []


class _StubSelector:
    """Real breaker bookkeeping (with the hint), stubbed provider construction."""

    def __init__(self, breaker: ProviderCircuitBreaker, chain: list[str]) -> None:
        self._breaker = breaker
        self._chain = chain
        self._index = 0
        self.providers: dict[str, _StubProvider] = {}

    @property
    def active_provider_id(self) -> str:
        return self._chain[self._index]

    def has_fallback(self) -> bool:
        return self._index < len(self._chain) - 1

    def next_fallback_after_failure(self, failure: Exception) -> _StubProvider:
        if not self.has_fallback():
            raise IndexError("No more provider fallbacks available")
        self._index += 1
        return self.providers[self.active_provider_id]

    def record_provider_failure(
        self, kind: Any = None, reason: str = "", **hint: Any
    ) -> BreakerState:
        return self._breaker.record_failure(
            self.active_provider_id, kind=kind, reason=reason, **hint
        )

    def record_provider_success(self) -> None:
        self._breaker.record_success(self.active_provider_id)


def _rate_limited(retry_after: float | None) -> ProviderError:
    return ProviderError(
        code="429",
        message="HTTP 429: rate limit exceeded",
        retry_after_seconds=retry_after,
    )


def _ok(text: str = "hello") -> list[Any]:
    return [ProviderText(text=text), ProviderDone(stop_reason="stop", output_tokens=1)]


def _breaker(threshold: int = 3) -> ProviderCircuitBreaker:
    return ProviderCircuitBreaker(BreakerSettings(failure_threshold=threshold))


def _setup(
    primary_streams: list[list[Any]],
    *,
    threshold: int = 3,
) -> tuple[ProviderCircuitBreaker, _StubSelector, _StubProvider, _StubProvider]:
    breaker = _breaker(threshold)
    selector = _StubSelector(breaker, ["openrouter", "deepseek"])
    fallback = _StubProvider("openai", [_ok("from fallback")])
    selector.providers["deepseek"] = fallback
    primary = _StubProvider("openai", primary_streams)
    return breaker, selector, primary, fallback


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []

    async def _fake_sleep(delay: float, *args: Any, **kwargs: Any) -> None:
        if delay > 0:
            recorded.append(delay)
        await _REAL_SLEEP(0)

    monkeypatch.setattr("agentos.engine.agent.asyncio.sleep", _fake_sleep)
    return recorded


async def _run(agent: Agent) -> list[Any]:
    return [event async for event in agent.run_turn("hi")]


def _text(events: list[Any]) -> str:
    return "".join(getattr(event, "text", "") for event in events if event.kind == "text_delta")


def _warning_codes(events: list[Any]) -> list[str]:
    return [event.code for event in events if isinstance(event, WarningEvent)]


# ----------------------------------------------------------------- policy helpers


def test_honoured_retry_after_rule() -> None:
    rate = ProviderFailureKind.RATE_LIMITED
    assert honoured_retry_after(rate, 2.0, 30.0) == 2.0
    assert honoured_retry_after(rate, 30.0, 30.0) == 30.0
    assert honoured_retry_after(rate, 31.0, 30.0) is None
    assert honoured_retry_after(rate, None, 30.0) is None
    assert honoured_retry_after(rate, 2.0, 0.0) is None
    assert honoured_retry_after(ProviderFailureKind.PROVIDER_OVERLOADED, 2.0, 30.0) == 2.0
    assert honoured_retry_after(ProviderFailureKind.AUTH_INVALID, 2.0, 30.0) is None


def test_retry_after_delay_adds_only_a_small_jitter() -> None:
    for hint in (0.0, 2.0, 30.0):
        for _ in range(50):
            delay = retry_after_delay(hint)
            assert hint <= delay <= hint + max(0.1, min(1.0, hint * 0.1))


# ----------------------------------------------------------------- wrapper


@pytest.mark.anyio
async def test_wrapper_passes_a_hinted_429_through_instead_of_failing_over() -> None:
    breaker, selector, primary, fallback = _setup([[_rate_limited(2.0)]], threshold=1)
    wrapper = _SelectorFallbackProvider(primary, selector)

    events = [event async for event in wrapper.chat([], tools=None, config=None)]

    assert [type(event) for event in events] == [ProviderError]
    assert events[0].retry_after_seconds == 2.0
    assert fallback.calls == 0
    assert selector.active_provider_id == "openrouter"
    assert breaker.state("openrouter") is BreakerState.CLOSED


@pytest.mark.anyio
@pytest.mark.parametrize("hint", [None, 120.0])
async def test_wrapper_still_fails_over_an_unhinted_or_long_hinted_429(
    hint: float | None,
) -> None:
    breaker, selector, primary, fallback = _setup([[_rate_limited(hint)]], threshold=1)
    wrapper = _SelectorFallbackProvider(primary, selector)

    events = [event async for event in wrapper.chat([], tools=None, config=None)]

    assert _text(events) == "from fallback"
    assert fallback.calls == 1
    assert breaker.state("openrouter") is BreakerState.OPEN


# ----------------------------------------------------------------- agent loop


@pytest.mark.anyio
async def test_agent_sleeps_the_hint_and_retries_the_same_model(sleeps: list[float]) -> None:
    breaker, selector, primary, fallback = _setup([[_rate_limited(2.0)], _ok("from primary")])
    agent = Agent(
        provider=_SelectorFallbackProvider(primary, selector),
        config=AgentConfig(max_provider_retries=3, retry_base_backoff_ms=10_000),
    )

    events = await _run(agent)

    assert _text(events) == "from primary"
    assert primary.calls == 2
    assert fallback.calls == 0
    assert len(sleeps) == 1
    assert 2.0 <= sleeps[0] <= 2.2
    assert "provider_retry_after_wait" in _warning_codes(events)
    assert breaker.status("openrouter").consecutive_failures == 0


@pytest.mark.anyio
async def test_three_hinted_429s_do_not_open_the_breaker(sleeps: list[float]) -> None:
    breaker, selector, primary, fallback = _setup(
        [[_rate_limited(1.0)], [_rate_limited(1.0)], [_rate_limited(1.0)], _ok("from primary")],
        threshold=3,
    )
    agent = Agent(
        provider=_SelectorFallbackProvider(primary, selector),
        config=AgentConfig(max_provider_retries=3),
    )

    events = await _run(agent)

    assert _text(events) == "from primary"
    assert primary.calls == 4
    assert fallback.calls == 0
    assert len(sleeps) == 3
    assert breaker.state("openrouter") is BreakerState.CLOSED


@pytest.mark.anyio
async def test_agent_fails_over_once_hinted_retries_are_spent(sleeps: list[float]) -> None:
    breaker, selector, primary, fallback = _setup([[_rate_limited(2.0)]])
    agent = Agent(
        provider=_SelectorFallbackProvider(primary, selector),
        config=AgentConfig(max_provider_retries=2),
    )

    events = await _run(agent)

    assert _text(events) == "from fallback"
    # One call plus two hinted retries on the primary, then the failover.
    assert primary.calls == 3
    assert fallback.calls == 1
    assert len(sleeps) == 2
    assert "provider_retry_after_fallback" in _warning_codes(events)
    assert breaker.state("openrouter") is BreakerState.CLOSED


@pytest.mark.anyio
async def test_hint_over_the_cap_fails_over_immediately(sleeps: list[float]) -> None:
    _breaker_, selector, primary, fallback = _setup([[_rate_limited(10.0)]])
    agent = Agent(
        provider=_SelectorFallbackProvider(primary, selector),
        config=AgentConfig(max_provider_retries=3, rate_limit_retry_after_cap_seconds=5.0),
    )

    events = await _run(agent)

    assert _text(events) == "from fallback"
    assert primary.calls == 1
    assert fallback.calls == 1
    assert sleeps == []


@pytest.mark.anyio
async def test_agent_without_a_selector_sleeps_the_hint_not_the_backoff(
    sleeps: list[float],
) -> None:
    provider = _StubProvider("openai", [[_rate_limited(2.0)], _ok("after wait")])
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_provider_retries=3, retry_base_backoff_ms=10_000),
    )

    events = await _run(agent)

    assert _text(events) == "after wait"
    assert provider.calls == 2
    assert len(sleeps) == 1
    assert 2.0 <= sleeps[0] <= 2.2


@pytest.mark.anyio
async def test_unhinted_429_keeps_the_exponential_backoff(sleeps: list[float]) -> None:
    provider = _StubProvider("openai", [[_rate_limited(None)], _ok("after backoff")])
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_provider_retries=3, retry_base_backoff_ms=10_000),
    )

    events = await _run(agent)

    assert _text(events) == "after backoff"
    assert len(sleeps) == 1
    # Exponential backoff with multiplicative jitter: 0.5x-1.5x the 10 s base.
    assert 5.0 <= sleeps[0] <= 15.0
    assert "provider_retry_after_wait" not in _warning_codes(events)


@pytest.mark.anyio
async def test_zero_cap_turns_the_behaviour_off(sleeps: list[float]) -> None:
    breaker, selector, primary, fallback = _setup([[_rate_limited(2.0)]], threshold=1)
    agent = Agent(
        provider=_SelectorFallbackProvider(primary, selector),
        config=AgentConfig(rate_limit_retry_after_cap_seconds=0.0),
    )

    events = await _run(agent)

    assert _text(events) == "from fallback"
    assert primary.calls == 1
    assert breaker.state("openrouter") is BreakerState.OPEN
