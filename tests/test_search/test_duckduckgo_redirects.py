"""DuckDuckGo redirect links are resolved to their target however they are spelled (#2082).

The HTML endpoint links each result through ``/l/?uddg=<target>``, and it
emits that link relative (``/l/?uddg=``), protocol-relative
(``//duckduckgo.com/l/?uddg=``) or absolute. The cleaner matched only the
protocol-relative spelling, so the relative one -- the one the endpoint uses
most -- reached the caller raw, and a follow-up ``web_fetch`` had nothing it
could route.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agentos.search.providers.duckduckgo import DuckDuckGoProvider


def _mock_html(monkeypatch: pytest.MonkeyPatch, html: str) -> None:
    mock_client = AsyncMock()
    mock_client.post.return_value.raise_for_status = lambda: None
    mock_client.post.return_value.text = html
    monkeypatch.setattr("httpx.AsyncClient.__aenter__", AsyncMock(return_value=mock_client))


def _result_html(href: str) -> str:
    return (
        '<div class="result">'
        f'<h2 class="result__title"><a href="{href}">Title</a></h2>'
        '<a class="result__snippet">Snippet</a>'
        "</div>"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("href", "expected"),
    [
        pytest.param(
            "/l/?uddg=https%3A%2F%2Fpython.org%2Fdocs&amp;rut=123",
            "https://python.org/docs",
            id="relative",
        ),
        pytest.param(
            "//duckduckgo.com/l/?uddg=https%3A%2F%2Fpypi.org%2Fhttpx&amp;rut=1",
            "https://pypi.org/httpx",
            id="protocol-relative",
        ),
        pytest.param(
            "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2Fagent&amp;rut=1",
            "https://github.com/agent",
            id="absolute",
        ),
        pytest.param(
            "https://html.duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2F",
            "https://example.org/",
            id="html-subdomain",
        ),
        pytest.param(
            "/l/?rut=123&amp;uddg=https%3A%2F%2Fexample.org%2Fa%3Fq%3D1%26r%3D2",
            "https://example.org/a?q=1&r=2",
            id="uddg-not-first-and-target-has-query",
        ),
    ],
)
async def test_redirect_links_resolve_to_the_target(
    monkeypatch: pytest.MonkeyPatch, href: str, expected: str
) -> None:
    _mock_html(monkeypatch, _result_html(href))

    results = await DuckDuckGoProvider().search("query")

    assert [result.url for result in results] == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "href",
    [
        pytest.param("https://example.org/docs", id="plain-organic"),
        pytest.param("https://example.org/l/?uddg=https%3A%2F%2Fother.test", id="foreign-host-l"),
        pytest.param(
            "https://example.org/?uddg=https%3A%2F%2Fother.test", id="uddg-in-organic-query"
        ),
        pytest.param(
            "https://notduckduckgo.com/l/?uddg=https%3A%2F%2Fother.test", id="lookalike-host"
        ),
        pytest.param("/l/?rut=123", id="redirect-without-target"),
        pytest.param("http://[bad/l/?uddg=x", id="malformed-host-does-not-raise"),
    ],
)
async def test_non_redirect_links_are_left_alone(
    monkeypatch: pytest.MonkeyPatch, href: str
) -> None:
    _mock_html(monkeypatch, _result_html(href))

    results = await DuckDuckGoProvider().search("query")

    assert [result.url for result in results] == [href.replace("&amp;", "&")]


@pytest.mark.asyncio
async def test_ads_are_still_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_html(
        monkeypatch,
        _result_html("https://duckduckgo.com/y.js?ad_provider=x")
        + _result_html("/l/?uddg=https%3A%2F%2Fexample.org%2F"),
    )

    results = await DuckDuckGoProvider().search("query")

    assert [result.url for result in results] == ["https://example.org/"]


@pytest.mark.asyncio
async def test_search_providers_zero_or_negative_max_results_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_client(*args, **kwargs):
        raise AssertionError("network client must not be constructed when max_results <= 0")

    monkeypatch.setattr("httpx.AsyncClient", _fail_client)

    assert await DuckDuckGoProvider().search("query", max_results=0) == []
    assert await DuckDuckGoProvider().search("query", max_results=-1) == []
