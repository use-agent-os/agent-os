"""DuckDuckGo provider raises classified SearchProviderError on upstream failures.

Regression for #1122: the provider used to swallow every ``httpx.HTTPError``
when ``diagnostics`` was off (the default) and return ``[]``, making a real
HTTP/network failure indistinguishable from "0 organic results found" and
preventing the search fallback policy from cascading to a secondary provider.

It must now align with the Brave and Tavily providers and always raise a
structured ``SearchProviderError`` with a classified ``kind``
(``timeout`` / ``rate_limit`` / ``http`` / ``network``).
"""

from __future__ import annotations

import httpx
import pytest

from agentos.search.providers.duckduckgo import DuckDuckGoProvider
from agentos.search.types import SearchProviderError


def _client_raising(exc: Exception):
    """Build an httpx.AsyncClient stand-in whose post() always raises *exc*."""

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> _Client.Inner:
            return self.Inner()

        async def __aexit__(self, *args) -> None:
            return None

        class Inner:
            def __init__(self) -> None:
                pass

            async def post(self, *args, **kwargs):
                raise exc

    return _Client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "kind", "retryable"),
    [
        (httpx.TimeoutException("t"), "timeout", True),
        (
            httpx.HTTPStatusError(
                "429",
                request=httpx.Request("POST", "x"),
                response=httpx.Response(429),
            ),
            "rate_limit",
            True,
        ),
        (
            httpx.HTTPStatusError(
                "500",
                request=httpx.Request("POST", "x"),
                response=httpx.Response(500),
            ),
            "http",
            False,
        ),
        (httpx.ConnectError("boom"), "network", True),
    ],
)
async def test_error_raises_classified(
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    kind: str,
    retryable: bool,
) -> None:
    """A provider error is surfaced as a classified SearchProviderError."""
    import agentos.search.providers.duckduckgo as mod

    monkeypatch.setattr(mod.httpx, "AsyncClient", _client_raising(exc))
    provider = DuckDuckGoProvider()  # diagnostics off (default)

    with pytest.raises(SearchProviderError) as info:
        await provider.search("hello")

    assert info.value.provider == "duckduckgo"
    assert info.value.kind == kind
    assert info.value.retryable is retryable


@pytest.mark.asyncio
async def test_error_raises_with_diagnostics_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raising (not returning []) holds even when diagnostics is enabled."""
    import agentos.search.providers.duckduckgo as mod

    exc = httpx.ConnectError("boom")
    monkeypatch.setattr(mod.httpx, "AsyncClient", _client_raising(exc))
    provider = DuckDuckGoProvider(diagnostics=True)

    with pytest.raises(SearchProviderError) as info:
        await provider.search("hello")

    assert info.value.kind == "network"


@pytest.mark.asyncio
async def test_success_still_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 response still yields parsed results (happy path unchanged)."""
    import agentos.search.providers.duckduckgo as mod

    html = (
        '<div class="result web-result">'
        '<h2 class="result__title"><a href="https://example.com/a">Example</a></h2>'
        '<div class="result__snippet">snippet text</div>'
        "</div>"
    )

    class _Inner:
        async def post(self, *args, **kwargs):
            resp = httpx.Response(200, text=html)
            resp.request = httpx.Request("POST", "x")
            return resp

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> _Inner:
            return _Inner()

        async def __aexit__(self, *args) -> None:
            return None

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    provider = DuckDuckGoProvider()

    results = await provider.search("hello")
    assert len(results) == 1
    assert results[0].title == "Example"
    assert results[0].url == "https://example.com/a"
    assert results[0].source == "duckduckgo"
