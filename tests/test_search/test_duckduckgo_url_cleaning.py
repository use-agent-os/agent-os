"""DuckDuckGo redirect links are cleaned to their real target URL (#2082, #1017)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agentos.search.providers.duckduckgo import DuckDuckGoProvider


def _mock_html(monkeypatch, html: str) -> None:
    mock_client = AsyncMock()
    mock_client.post.return_value.raise_for_status = lambda: None
    mock_client.post.return_value.text = html
    monkeypatch.setattr("httpx.AsyncClient.__aenter__", AsyncMock(return_value=mock_client))


def _result_html(href: str) -> str:
    return f"""
    <div class="result">
        <h2 class="result__title"><a href="{href}">Title</a></h2>
        <a class="result__snippet">Snippet text</a>
    </div>
    """


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "href,expected",
    [
        pytest.param(
            "/l/?uddg=https%3A%2F%2Fpython.org%2Fdocs&rut=123",
            "https://python.org/docs",
            id="relative",
        ),
        pytest.param(
            "//duckduckgo.com/l/?uddg=https%3A%2F%2Fpypi.org%2Fhttpx&rut=1",
            "https://pypi.org/httpx",
            id="protocol-relative",
        ),
        pytest.param(
            "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2Fagent&rut=1",
            "https://github.com/agent",
            id="absolute",
        ),
        pytest.param(
            # uddg is not the first query parameter.
            "/l/?rut=abc123&uddg=https%3A%2F%2Fexample.com%2Fpage&kh=-1",
            "https://example.com/page",
            id="uddg-not-first-param",
        ),
        pytest.param(
            # The target URL has its own (percent-encoded) query string.
            "/l/?uddg=https%3A%2F%2Fexample.org%2Fsearch%3Fq%3Dtest%26page%3D2&rut=1",
            "https://example.org/search?q=test&page=2",
            id="target-has-its-own-query-string",
        ),
    ],
)
async def test_ddg_redirect_link_resolves_to_target_url(
    monkeypatch: pytest.MonkeyPatch, href: str, expected: str
) -> None:
    _mock_html(monkeypatch, _result_html(href))

    results = await DuckDuckGoProvider().search("query")

    assert len(results) == 1
    assert results[0].url == expected
    assert results[0].snippet == "Snippet text"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "href",
    [
        pytest.param("https://example.com/page", id="plain-organic-result"),
        pytest.param(
            # Organic result whose own query string happens to contain a
            # `uddg` parameter -- must NOT be mistaken for a DDG redirect.
            "https://example.com/page?uddg=notaredirect",
            id="organic-result-with-uddg-looking-query-param",
        ),
        pytest.param("https://duckduckgo.com/search?q=test", id="ddg-search-page-not-redirect"),
    ],
)
async def test_non_redirect_url_is_left_unchanged(
    monkeypatch: pytest.MonkeyPatch, href: str
) -> None:
    _mock_html(monkeypatch, _result_html(href))

    results = await DuckDuckGoProvider().search("query")

    assert len(results) == 1
    assert results[0].url == href
