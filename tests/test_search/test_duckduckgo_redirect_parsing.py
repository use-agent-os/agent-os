"""Regression tests for DuckDuckGo redirect URL parsing."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agentos.search.providers.duckduckgo import DuckDuckGoProvider, _clean_ddg_url


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
