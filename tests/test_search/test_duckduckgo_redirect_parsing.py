"""Regression tests for DuckDuckGo redirect URL parsing."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agentos.search.providers.duckduckgo import DuckDuckGoProvider, _clean_ddg_url
from agentos.search.types import SearchProviderError


def test_clean_ddg_url_relative_redirect() -> None:
    url = "/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs&rut=123"
    assert _clean_ddg_url(url) == "https://example.com/docs"


def test_clean_ddg_url_absolute_redirect() -> None:
    url = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fpython.org%2Fpeps&rut=abc"
    assert _clean_ddg_url(url) == "https://python.org/peps"


def test_clean_ddg_url_protocol_relative() -> None:
    url = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2Fuse-agent-os"
    assert _clean_ddg_url(url) == "https://github.com/use-agent-os"


def test_clean_ddg_url_complex_nested_query() -> None:
    url = "/l/?rut=abc123xyz&uddg=https%3A%2F%2Fexample.org%2Fsearch%3Fq%3Dtest%26page%3D2&other=1"
    assert _clean_ddg_url(url) == "https://example.org/search?q=test&page=2"


def test_clean_ddg_url_direct_link_unchanged() -> None:
    url = "https://direct-link.org/test"
    assert _clean_ddg_url(url) == "https://direct-link.org/test"
    assert _clean_ddg_url("") == ""


@pytest.mark.asyncio
async def test_duckduckgo_provider_search_parses_html_with_relative_redirects() -> None:
    html_doc = (
        '<html><body>'
        '<div class="result">'
        '<h2 class="result__title">'
        '<a class="result__url" href="/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F&rut=abc">'
        'Python Docs</a>'
        '</h2>'
        '<div class="result__snippet">Official Python documentation.</div>'
        '</div>'
        '<div class="result">'
        '<h2 class="result__title">'
        '<a class="result__url" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fpypi.org%2F&rut=def">'
        'PyPI</a>'
        '</h2>'
        '<div class="result__snippet">Python Package Index.</div>'
        '</div>'
        '</body></html>'
    )
    mock_resp = httpx.Response(
        status_code=200,
        text=html_doc,
        request=httpx.Request("POST", "https://html.duckduckgo.com/html"),
    )

    provider = DuckDuckGoProvider()
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        results = await provider.search("python")

    assert len(results) == 2
    assert results[0].title == "Python Docs"
    assert results[0].url == "https://docs.python.org/3/"
    assert results[0].snippet == "Official Python documentation."
    assert results[0].source == "duckduckgo"

    assert results[1].title == "PyPI"
    assert results[1].url == "https://pypi.org/"
    assert results[1].snippet == "Python Package Index."
    assert results[1].source == "duckduckgo"


# ---------------------------------------------------------------------------
# Ad filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duckduckgo_provider_skips_ad_results() -> None:
    html_doc = (
        '<html><body>'
        '<div class="result">'
        '<h2 class="result__title">'
        '<a class="result__url" href="https://duckduckgo.com/y.js?ad_domain=ad.example.com">Ad</a>'
        '</h2>'
        '<div class="result__snippet">Sponsored result.</div>'
        '</div>'
        '<div class="result">'
        '<h2 class="result__title">'
        '<a class="result__url" href="https://real.example.com">Real</a>'
        '</h2>'
        '<div class="result__snippet">Real result.</div>'
        '</div>'
        '</body></html>'
    )
    mock_resp = httpx.Response(
        status_code=200,
        text=html_doc,
        request=httpx.Request("POST", "https://html.duckduckgo.com/html"),
    )

    provider = DuckDuckGoProvider()
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        results = await provider.search("test")

    assert len(results) == 1
    assert results[0].title == "Real"
    assert results[0].url == "https://real.example.com"


# ---------------------------------------------------------------------------
# Empty HTML / no results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duckduckgo_provider_empty_html_returns_empty_list() -> None:
    mock_resp = httpx.Response(
        status_code=200,
        text="<html><body><p>No results.</p></body></html>",
        request=httpx.Request("POST", "https://html.duckduckgo.com/html"),
    )

    provider = DuckDuckGoProvider()
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        results = await provider.search("xyznonexistent")

    assert results == []


# ---------------------------------------------------------------------------
# max_results capping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duckduckgo_provider_respects_max_results() -> None:
    # Build 5 result divs
    divs = ""
    for i in range(5):
        divs += (
            f'<div class="result">'
            f'<h2 class="result__title">'
            f'<a class="result__url" href="https://example.com/{i}">Result {i}</a>'
            f'</h2>'
            f'<div class="result__snippet">Snippet {i}.</div>'
            f'</div>'
        )
    html_doc = f"<html><body>{divs}</body></html>"
    mock_resp = httpx.Response(
        status_code=200,
        text=html_doc,
        request=httpx.Request("POST", "https://html.duckduckgo.com/html"),
    )

    provider = DuckDuckGoProvider()
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        results = await provider.search("test", max_results=2)

    assert len(results) == 2
    assert results[0].url == "https://example.com/0"
    assert results[1].url == "https://example.com/1"


# ---------------------------------------------------------------------------
# Error handling — structured SearchProviderError
# ---------------------------------------------------------------------------



@pytest.mark.asyncio
async def test_duckduckgo_provider_raises_on_timeout() -> None:
    provider = DuckDuckGoProvider()
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.ConnectTimeout("connection timed out")
        with pytest.raises(SearchProviderError) as exc_info:
            await provider.search("test")

    assert exc_info.value.kind == "timeout"
    assert exc_info.value.retryable is True
    assert exc_info.value.provider == "duckduckgo"


@pytest.mark.asyncio
async def test_duckduckgo_provider_raises_on_rate_limit() -> None:
    mock_resp = httpx.Response(
        status_code=429,
        text="Too Many Requests",
        request=httpx.Request("POST", "https://html.duckduckgo.com/html"),
    )

    provider = DuckDuckGoProvider()
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        with pytest.raises(SearchProviderError) as exc_info:
            await provider.search("test")

    assert exc_info.value.kind == "rate_limit"
    assert exc_info.value.retryable is True
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_duckduckgo_provider_raises_on_forbidden() -> None:
    mock_resp = httpx.Response(
        status_code=403,
        text="Forbidden",
        request=httpx.Request("POST", "https://html.duckduckgo.com/html"),
    )

    provider = DuckDuckGoProvider()
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        with pytest.raises(SearchProviderError) as exc_info:
            await provider.search("test")

    assert exc_info.value.kind == "auth"
    assert exc_info.value.retryable is False
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_duckduckgo_provider_raises_on_server_error() -> None:
    mock_resp = httpx.Response(
        status_code=500,
        text="Internal Server Error",
        request=httpx.Request("POST", "https://html.duckduckgo.com/html"),
    )

    provider = DuckDuckGoProvider()
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        with pytest.raises(SearchProviderError) as exc_info:
            await provider.search("test")

    assert exc_info.value.kind == "http"
    assert exc_info.value.retryable is True
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_duckduckgo_provider_raises_on_network_error() -> None:
    provider = DuckDuckGoProvider()
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.ConnectError("DNS resolution failed")
        with pytest.raises(SearchProviderError) as exc_info:
            await provider.search("test")

    assert exc_info.value.kind == "network"
    assert exc_info.value.retryable is True
