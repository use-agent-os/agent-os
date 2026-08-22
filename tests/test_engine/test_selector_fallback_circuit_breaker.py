"""``_SelectorFallbackProvider`` feeds the selector's provider circuit breaker."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from agentos.engine.runtime import _SelectorFallbackProvider
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import ErrorEvent as ProviderError
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.provider.circuit_breaker import (
    BreakerSettings,
    BreakerState,
    ProviderCircuitBreaker,
)
from agentos.provider.selector import ModelSelector, ProviderConfig, SelectorConfig

pytestmark = pytest.mark.anyio


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
    """Real breaker bookkeeping, stubbed provider construction."""

    def __init__(self, breaker: ProviderCircuitBreaker, chain: list[str]) -> None:
        self._breaker = breaker
        self._chain = chain
        self._index = 0
        self.fallbacks: list[str] = []
        self.providers: dict[str, _StubProvider] = {}

    @property
    def active_provider_id(self) -> str:
        return self._chain[self._index]

    def next_fallback_after_failure(self, failure: Exception) -> _StubProvider:
        if self._index >= len(self._chain) - 1:
            raise IndexError("No fallback chain available")
        self._index += 1
        self.fallbacks.append(str(failure))
        return self.providers[self.active_provider_id]

    def record_provider_failure(self, kind: Any = None, reason: str = "") -> BreakerState:
        return self._breaker.record_failure(self.active_provider_id, kind=kind, reason=reason)

    def record_provider_success(self) -> None:
        self._breaker.record_success(self.active_provider_id)


def _breaker(threshold: int = 2) -> ProviderCircuitBreaker:
    return ProviderCircuitBreaker(
        BreakerSettings(failure_threshold=threshold, cooldown_seconds=60.0)
    )


async def _drain(wrapper: _SelectorFallbackProvider) -> list[Any]:
    return [event async for event in wrapper.chat([], tools=None, config=None)]


async def test_overload_errors_accumulate_until_the_breaker_opens() -> None:
    breaker = _breaker(threshold=2)
    selector = _StubSelector(breaker, ["openrouter"])
    provider = _StubProvider("openai", [[ProviderError(code="503", message="upstream 503")]])

    for _ in range(2):
        await _drain(_SelectorFallbackProvider(provider, selector))

    assert breaker.state("openrouter") is BreakerState.OPEN
    assert breaker.status("openrouter").last_failure_kind == "provider_overloaded"


async def test_streamed_text_closes_the_breaker() -> None:
    breaker = _breaker(threshold=2)
    selector = _StubSelector(breaker, ["openrouter"])

    failing = _StubProvider("openai", [[ProviderError(code="503", message="upstream 503")]])
    await _drain(_SelectorFallbackProvider(failing, selector))
    assert breaker.status("openrouter").consecutive_failures == 1

    healthy = _StubProvider("openai", [[ProviderText(text="hi"), ProviderDone(stop_reason="stop")]])
    await _drain(_SelectorFallbackProvider(healthy, selector))

    assert breaker.status("openrouter").consecutive_failures == 0
    assert breaker.state("openrouter") is BreakerState.CLOSED


async def test_clean_tool_only_stream_counts_as_success() -> None:
    """A turn that emits no user-visible text still proves the provider is up."""
    breaker = _breaker(threshold=2)
    selector = _StubSelector(breaker, ["openrouter"])

    failing = _StubProvider("openai", [[ProviderError(code="503", message="upstream 503")]])
    await _drain(_SelectorFallbackProvider(failing, selector))

    quiet = _StubProvider("openai", [[ProviderDone(stop_reason="tool_use")]])
    await _drain(_SelectorFallbackProvider(quiet, selector))

    assert breaker.status("openrouter").consecutive_failures == 0


async def test_request_shaped_error_does_not_count_against_the_provider() -> None:
    breaker = _breaker(threshold=1)
    selector = _StubSelector(breaker, ["openrouter"])
    provider = _StubProvider(
        "openai", [[ProviderError(code="400", message="model not found: bogus")]]
    )

    await _drain(_SelectorFallbackProvider(provider, selector))

    assert breaker.state("openrouter") is BreakerState.CLOSED
    assert breaker.status("openrouter").consecutive_failures == 0


async def test_failover_records_the_primary_failure_and_the_fallback_success() -> None:
    breaker = _breaker(threshold=1)
    selector = _StubSelector(breaker, ["openrouter", "ollama"])
    fallback = _StubProvider(
        "ollama", [[ProviderText(text="hello"), ProviderDone(stop_reason="stop")]]
    )
    selector.providers["ollama"] = fallback
    primary = _StubProvider("openai", [[ProviderError(code="503", message="upstream 503")]])

    events = await _drain(_SelectorFallbackProvider(primary, selector))

    assert [getattr(event, "text", None) for event in events if hasattr(event, "text")] == ["hello"]
    assert breaker.state("openrouter") is BreakerState.OPEN
    assert breaker.state("ollama") is BreakerState.CLOSED
    assert breaker.status("ollama").consecutive_failures == 0


async def test_fallback_failure_is_recorded_against_the_fallback() -> None:
    breaker = _breaker(threshold=1)
    selector = _StubSelector(breaker, ["openrouter", "ollama"])
    selector.providers["ollama"] = _StubProvider(
        "ollama", [[ProviderError(code="503", message="upstream 503")]]
    )
    primary = _StubProvider("openai", [[ProviderError(code="503", message="upstream 503")]])

    await _drain(_SelectorFallbackProvider(primary, selector))

    assert breaker.state("openrouter") is BreakerState.OPEN
    assert breaker.state("ollama") is BreakerState.OPEN


async def test_selector_without_breaker_hooks_is_tolerated() -> None:
    """Older selector stand-ins (tests, plugins) must keep working."""

    class _Bare:
        def next_fallback_after_failure(self, failure: Exception) -> Any:
            raise IndexError("none")

    provider = _StubProvider(
        "openai", [[ProviderText(text="ok"), ProviderDone(stop_reason="stop")]]
    )
    events = await _drain(_SelectorFallbackProvider(provider, _Bare()))

    assert any(getattr(event, "text", "") == "ok" for event in events)


async def test_real_selector_skips_the_open_primary_on_the_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a failure on turn 1 routes turn 2 straight to the fallback.

    ``_build_provider`` is stubbed so the chain stays entirely offline — the
    point under test is the breaker bookkeeping, not provider transports.
    """
    from agentos.provider import selector as selector_module

    built: list[str] = []

    def _fake_build(cfg: ProviderConfig) -> _StubProvider:
        built.append(cfg.provider)
        return _StubProvider(
            cfg.provider, [[ProviderText(text="from fallback"), ProviderDone(stop_reason="stop")]]
        )

    monkeypatch.setattr(selector_module, "_build_provider", _fake_build)

    breaker = _breaker(threshold=1)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig("openrouter", "openai/gpt-5.6-luna", api_key="k"),
            fallbacks=[ProviderConfig("ollama", "llama3")],
        ),
        breaker=breaker,
    )

    turn_one = selector.clone()
    primary = _StubProvider("openai", [[ProviderError(code="503", message="upstream 503")]])
    events = await _drain(_SelectorFallbackProvider(primary, turn_one))

    assert breaker.state("openrouter") is BreakerState.OPEN
    assert built == ["ollama"]
    assert any(getattr(event, "text", "") == "from fallback" for event in events)

    turn_two = selector.clone()
    turn_two.resolve()
    assert turn_two.active_provider_id == "ollama"


async def test_repeated_error_events_in_one_stream_count_once() -> None:
    """One failed turn is one vote, however many error events it emitted."""
    breaker = _breaker(threshold=2)
    selector = _StubSelector(breaker, ["openrouter", "ollama"])
    selector.providers["ollama"] = _StubProvider(
        "ollama",
        [
            [
                ProviderError(code="503", message="upstream 503"),
                ProviderError(code="503", message="upstream 503 again"),
                ProviderError(code="503", message="and again"),
            ]
        ],
    )
    primary = _StubProvider("openai", [[ProviderError(code="503", message="upstream 503")]])

    await _drain(_SelectorFallbackProvider(primary, selector))

    assert breaker.status("openrouter").consecutive_failures == 1
    assert breaker.status("ollama").consecutive_failures == 1
    assert breaker.state("ollama") is BreakerState.CLOSED
