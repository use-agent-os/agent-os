"""Model selector with fallback chain and config-driven provider resolution."""

from __future__ import annotations

from dataclasses import dataclass, field

from .anthropic import AnthropicProvider
from .circuit_breaker import (
    BreakerState,
    ProviderBreakerStatus,
    ProviderCircuitBreaker,
)
from .failures import ProviderFailureKind
from .ollama import OllamaProvider
from .openai import OpenAIProvider
from .openai_responses import OpenAIResponsesProvider
from .protocol import LLMProvider, ProviderPlugin, resolve_failover_chain
from .registry import UnknownProviderError, get_provider_spec


@dataclass
class ProviderConfig:
    """Runtime configuration for a single provider."""

    provider: str  # "anthropic" | "openai" | "ollama"
    model: str
    api_key: str = ""
    base_url: str = ""
    org_id: str = ""
    proxy: str = ""  # explicit HTTP proxy URL
    provider_routing: dict[str, str] = field(default_factory=dict)


@dataclass
class SelectorConfig:
    """Full model selection config: primary + ordered fallback chain."""

    primary: ProviderConfig
    fallbacks: list[ProviderConfig] = field(default_factory=list)


class ProviderBuildError(Exception):
    """Raised when a provider cannot be instantiated."""


def _unsupported_runtime_message(provider: str) -> str:
    return (
        f"Provider '{provider}' is registered but runtime support "
        "is not enabled in this wave"
    )


def _missing_base_url_message(provider: str) -> str:
    return f"Provider '{provider}' requires an explicit base_url"


def _build_provider(cfg: ProviderConfig) -> LLMProvider:
    """Instantiate the correct provider class from a ProviderConfig."""
    try:
        spec = get_provider_spec(cfg.provider)
    except UnknownProviderError as exc:
        raise ProviderBuildError(str(exc)) from exc

    if not spec.runtime_supported:
        raise ProviderBuildError(_unsupported_runtime_message(cfg.provider))

    base_url = cfg.base_url or spec.default_base_url

    if not base_url and spec.provider_id in {"azure", "vllm"}:
        raise ProviderBuildError(_missing_base_url_message(cfg.provider))

    match spec.backend:
        case "anthropic":
            kwargs: dict = {"api_key": cfg.api_key, "model": cfg.model}
            if base_url:
                kwargs["base_url"] = base_url
            if cfg.proxy:
                kwargs["proxy"] = cfg.proxy
            return AnthropicProvider(**kwargs)

        case "openai_compat":
            kwargs = {
                "api_key": cfg.api_key,
                "model": cfg.model,
                "provider_kind": spec.provider_kind,
            }
            if base_url:
                kwargs["base_url"] = base_url
            if cfg.org_id:
                kwargs["org_id"] = cfg.org_id
            if cfg.proxy:
                kwargs["proxy"] = cfg.proxy
            if cfg.provider_routing:
                kwargs["provider_routing"] = cfg.provider_routing
            return OpenAIProvider(**kwargs)

        case "openai_responses":
            kwargs = {
                "api_key": cfg.api_key,
                "model": cfg.model,
            }
            if base_url:
                kwargs["base_url"] = base_url
            if cfg.org_id:
                kwargs["org_id"] = cfg.org_id
            if cfg.proxy:
                kwargs["proxy"] = cfg.proxy
            return OpenAIResponsesProvider(**kwargs)

        case "ollama":
            kwargs = {"model": cfg.model}
            if base_url:
                kwargs["base_url"] = base_url
            if cfg.proxy:
                kwargs["proxy"] = cfg.proxy
            return OllamaProvider(**kwargs)

        case _:
            raise ProviderBuildError(_unsupported_runtime_message(cfg.provider))


class ModelSelector:
    """Resolves a provider from primary config with fallback chain support.

    Usage::

        selector = ModelSelector(SelectorConfig(
            primary=ProviderConfig("anthropic", "claude-sonnet-4-6", api_key="..."),
            fallbacks=[ProviderConfig("ollama", "llama3")],
        ))
        provider = selector.resolve()  # returns primary
        # on failure, call selector.next_fallback() to get next in chain
    """

    def __init__(
        self,
        config: SelectorConfig,
        plugin: ProviderPlugin | None = None,
        breaker: ProviderCircuitBreaker | None = None,
    ) -> None:
        self._config = config
        self._chain: list[ProviderConfig] = [config.primary, *config.fallbacks]
        self._index = 0
        self._plugin = plugin
        # Breaker state has to outlive a single turn — ``clone()`` shares this
        # instance so every turn sees the failures the previous ones recorded.
        self._breaker = breaker if breaker is not None else ProviderCircuitBreaker()
        # Chain index this selector already holds breaker admission for. A turn
        # may call ``resolve()`` more than once (e.g. again after
        # ``override_model``); re-asking the breaker would find the half-open
        # probe this very turn was granted already in flight and wrongly skip
        # to the fallback, burning a probe per turn so the primary never
        # recovers.
        self._admitted_index: int | None = None

    @property
    def circuit_breaker(self) -> ProviderCircuitBreaker:
        """Shared per-provider health breaker (also shared with clones)."""
        return self._breaker

    def resolve(self) -> LLMProvider:
        """Return the current provider, skipping links whose breaker is open.

        This is the health-aware half of failover: during a provider outage a
        fresh turn starts on the first chain link the breaker still admits
        instead of paying the dead primary's timeout again.
        """
        self._index = self._first_admitted_index(self._index)
        return _build_provider(self._chain[self._index])

    def _first_admitted_index(self, start: int) -> int:
        """First index at or after ``start`` the breaker admits.

        Falls back to ``start`` when every remaining link is in cooldown —
        a provider in cooldown still beats no provider at all.
        """
        if self._admitted_index is not None and self._admitted_index >= start:
            return self._admitted_index
        for index in range(start, len(self._chain)):
            if self._breaker.allow(self._chain[index].provider):
                self._admitted_index = index
                return index
        self._admitted_index = start
        return start

    @property
    def active_provider_id(self) -> str:
        """Configured provider id of the currently-active chain link.

        This is the operator-facing identity (e.g. ``"openrouter"``,
        ``"deepseek"``) — distinct from the wire-protocol backend class that
        serves it. OpenAI-compatible providers all run through
        ``OpenAIProvider``, whose ``provider_name`` is the generic ``"openai"``;
        surfacing that would mislabel an OpenRouter deployment as OpenAI.
        """
        return self._chain[self._index].provider

    def has_fallback(self) -> bool:
        """True if there is at least one more fallback available."""
        return self._index < len(self._chain) - 1

    def next_fallback(self) -> LLMProvider:
        """Advance to the next fallback and return it.

        Raises IndexError if no more fallbacks are available.
        """
        if not self.has_fallback():
            raise IndexError("No more provider fallbacks available")
        self._index = self._first_admitted_index(self._index + 1)
        return _build_provider(self._chain[self._index])

    def record_provider_failure(
        self,
        kind: ProviderFailureKind | None = None,
        reason: str = "",
    ) -> BreakerState:
        """Count a failure against the currently-active chain link."""
        return self._breaker.record_failure(self.active_provider_id, kind=kind, reason=reason)

    def record_provider_success(self) -> None:
        """Clear the currently-active chain link's failure history."""
        self._breaker.record_success(self.active_provider_id)

    def circuit_breaker_status(self, provider: str) -> ProviderBreakerStatus:
        """Side-effect-free breaker snapshot for one provider id."""
        return self._breaker.status(provider)

    def circuit_breaker_snapshot(self) -> list[ProviderBreakerStatus]:
        """Side-effect-free breaker snapshot for every tracked provider."""
        return self._breaker.snapshot()

    def next_fallback_after_failure(self, primary_failure: Exception) -> LLMProvider:
        """Advance to the next fallback, consulting ``plugin.failover_hook``.

        When a plugin is registered its ``failover_hook`` return value
        replaces the static fallback chain from ``SelectorConfig``. An
        empty chain raises ``IndexError`` exactly like ``next_fallback``.
        """
        chain = resolve_failover_chain(primary_failure, self._config, self._plugin)
        if not chain:
            raise IndexError("No fallback chain available")
        self._chain = [self._chain[0], *chain]
        self._index = self._first_admitted_index(1)
        return _build_provider(self._chain[self._index])

    def override_model(self, model: str) -> None:
        """Update the model on the primary provider config (for runtime switching)."""
        if model and model != self._chain[0].model:
            self._chain[0] = ProviderConfig(
                provider=self._chain[0].provider,
                model=model,
                api_key=self._chain[0].api_key,
                base_url=self._chain[0].base_url,
                org_id=self._chain[0].org_id,
                proxy=self._chain[0].proxy,
                provider_routing=self._chain[0].provider_routing,
            )

    def sync_primary(self, cfg: ProviderConfig) -> None:
        """Replace the primary provider config for future resolves and clones."""
        self._config.primary = cfg
        self._chain[0] = cfg
        self.reset()

    def reset(self) -> None:
        """Reset to primary provider and drop any held breaker admission."""
        self._index = 0
        self._admitted_index = None

    def clone(self) -> ModelSelector:
        """Return an independent copy for concurrent use.

        The clone starts at index 0 with its own chain list, so mutations
        (override_model, next_fallback) don't affect the original. The circuit
        breaker is deliberately *shared*: provider health is a property of the
        outage, not of one turn.
        """
        return ModelSelector(self._config, plugin=self._plugin, breaker=self._breaker)

    async def list_models(self) -> list[dict]:
        """Aggregate models from all configured providers in the chain."""
        models: list[dict] = []
        for cfg in self._chain:
            try:
                provider = _build_provider(cfg)
                provider_models = await provider.list_models()
                models.extend(m.model_dump() for m in provider_models)
            except Exception:
                continue
        return models

    @property
    def current_config(self) -> ProviderConfig:
        return self._chain[self._index]


def build_provider(
    provider: str,
    model: str,
    api_key: str = "",
    base_url: str = "",
    org_id: str = "",
) -> LLMProvider:
    """Convenience factory: build a single provider directly."""
    return _build_provider(
        ProviderConfig(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            org_id=org_id,
        )
    )
