"""Search types — request/result/spec dataclasses and provider protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

SearchErrorKind = Literal["auth", "rate_limit", "timeout", "network", "http", "parse", "unknown"]


@dataclass
class SearchRequest:
    """A search request with the same defaults as provider.search(...)."""

    query: str
    max_results: int = 5


@dataclass
class SearchResult:
    """A single search result entry.

    Extra metadata is optional so existing callers that construct only
    title/url/snippet remain source-compatible.
    """

    title: str
    url: str
    snippet: str
    source: str = ""
    published_at: str | None = None


@dataclass(frozen=True)
class SearchProviderSpec:
    """Static metadata for a web search provider."""

    provider_id: str
    runtime_supported: bool = True
    metadata_supported: bool = True
    requires_api_key: bool = False
    env_key: str = ""
    capabilities: frozenset[str] = field(default_factory=lambda: frozenset({"web"}))


@dataclass
class SearchProviderError(RuntimeError):
    """Structured search provider failure for diagnostics and fallback policy."""

    provider: str
    kind: SearchErrorKind
    message: str
    retryable: bool = False
    status_code: int | None = None

    def __str__(self) -> str:
        return self.message


@runtime_checkable
class SearchProvider(Protocol):
    """Protocol for search backends."""

    name: str

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]: ...
