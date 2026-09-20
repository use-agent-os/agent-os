"""Search provider registry and factory."""

from __future__ import annotations

from agentos.search.types import SearchProvider, SearchProviderSpec

_providers: dict[str, type[SearchProvider]] = {}
_provider_specs: dict[str, SearchProviderSpec] = {
    "brave": SearchProviderSpec(
        provider_id="brave",
        requires_api_key=True,
        env_key="BRAVE_SEARCH_API_KEY",
    ),
    "duckduckgo": SearchProviderSpec(provider_id="duckduckgo"),
    "tavily": SearchProviderSpec(
        provider_id="tavily",
        requires_api_key=True,
        env_key="TAVILY_API_KEY",
    ),
    "exa": SearchProviderSpec(
        provider_id="exa",
        runtime_supported=False,
        requires_api_key=True,
        env_key="EXA_API_KEY",
    ),
    "perplexity": SearchProviderSpec(
        provider_id="perplexity",
        runtime_supported=False,
        requires_api_key=True,
        env_key="PERPLEXITY_API_KEY",
    ),
}


def register_provider(
    name: str,
    cls: type[SearchProvider],
    spec: SearchProviderSpec | None = None,
) -> None:
    """Register a runtime provider class.

    ``spec`` is optional to keep the historical ``register_provider(name, cls)``
    call shape working for tests and third-party callers.
    """

    key = str(name or "").strip().lower()
    _providers[key] = cls
    if spec is not None:
        _provider_specs[key] = spec
    elif key not in _provider_specs:
        _provider_specs[key] = SearchProviderSpec(provider_id=key)


def get_provider(name: str, **kwargs) -> SearchProvider:
    key = str(name or "").strip().lower()
    if key not in _providers:
        available = ", ".join(sorted(_providers.keys())) if _providers else "none"
        raise ValueError(f"Unknown search provider '{name}'. Available: {available}")
    return _providers[key](**kwargs)


def list_providers() -> list[str]:
    return sorted(_providers.keys())


def list_provider_specs() -> tuple[SearchProviderSpec, ...]:
    return tuple(_provider_specs[name] for name in sorted(_provider_specs))


def get_provider_spec(name: str) -> SearchProviderSpec:
    key = str(name or "").strip().lower()
    try:
        return _provider_specs[key]
    except KeyError as exc:
        available = ", ".join(sorted(_provider_specs))
        raise ValueError(f"Unknown search provider '{name}'. Available: {available}") from exc

