"""A provider selector over a fixed chain of scripted providers.

Pairs with ``agentos.engine.runtime._SelectorFallbackProvider`` so a test can
exercise failover without building real providers: the breaker bookkeeping is
the real ``ProviderCircuitBreaker``, only provider construction is faked.

Example::

    chain = ChainSelector({"primary": ScriptedProvider(...), "backup": ScriptedProvider(...)})
    wrapped = chain.wrapped()          # the _SelectorFallbackProvider over the chain
    agent = Agent(provider=wrapped, ...)
"""

from __future__ import annotations

from typing import Any

from agentos.provider.circuit_breaker import (
    BreakerSettings,
    BreakerState,
    ProviderCircuitBreaker,
)
from tests.fakes.scripted_provider import ScriptedProvider


class ChainSelector:
    def __init__(
        self,
        providers: dict[str, ScriptedProvider],
        *,
        breaker: ProviderCircuitBreaker | None = None,
    ) -> None:
        if not providers:
            raise ValueError("ChainSelector needs at least one provider")
        self.providers = dict(providers)
        self._chain = list(providers)
        self._index = 0
        self.breaker = breaker or ProviderCircuitBreaker(BreakerSettings(failure_threshold=3))
        #: Provider ids failed over to, in order.
        self.failovers: list[str] = []

    @property
    def active_provider_id(self) -> str:
        return self._chain[self._index]

    @property
    def active(self) -> ScriptedProvider:
        return self.providers[self.active_provider_id]

    def has_fallback(self) -> bool:
        return self._index < len(self._chain) - 1

    def next_fallback_after_failure(self, failure: Exception) -> ScriptedProvider:
        if not self.has_fallback():
            raise IndexError("No more provider fallbacks available")
        self._index += 1
        self.failovers.append(self.active_provider_id)
        return self.active

    def record_provider_failure(
        self, kind: Any = None, reason: str = "", **hint: Any
    ) -> BreakerState:
        return self.breaker.record_failure(
            self.active_provider_id, kind=kind, reason=reason, **hint
        )

    def record_provider_success(self) -> None:
        self.breaker.record_success(self.active_provider_id)

    def wrapped(self) -> Any:
        """The real selector-fallback wrapper over this chain."""

        from agentos.engine.runtime import _SelectorFallbackProvider  # noqa: PLC0415

        return _SelectorFallbackProvider(self.active, self)
