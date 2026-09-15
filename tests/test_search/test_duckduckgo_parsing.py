"""Tests for DuckDuckGo search result URL parsing and cleaning."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agentos.search.providers.duckduckgo import DuckDuckGoProvider


def _mock_html_response(monkeypatch, html: str) -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = html
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    monkeypatch.setattr("httpx.AsyncClient.__aenter__", AsyncMock(return_value=mock_client))


@pytest.mark.asyncio
async def test_duckduckgo_relative_redirect_url_cleaned(monkeypatch) -> None:
    """Relative /l/?uddg=... redirect links are unquoted to clean target URLs."""
    html = """
    <div class="result">
        <h2 class="result__title">
            <a href="/l/?uddg=https%3A%2F%2Fpython.org%2Fdocs&amp;rut=123">Python Docs</a>
        </h2>
        <div class="result__snippet">Official Python documentation.</div>
    </div>
    """
    _mock_html_response(monkeypatch, html)

    results = await DuckDuckGoProvider().search("python docs")

    assert len(results) == 1
    assert results[0].title == "Python Docs"
    assert results[0].url == "https://python.org/docs"
    assert results[0].snippet == "Official Python documentation."
    assert results[0].source == "duckduckgo"


@pytest.mark.asyncio
async def test_duckduckgo_protocol_relative_redirect_url_cleaned(monkeypatch) -> None:
    """Protocol-relative //duckduckgo.com/l/?uddg=... redirect links are cleaned."""
    html = """
    <div class="result">
        <h2 class="result__title">
            <a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fpypi.org%2Fhttpx">httpx PyPI</a>
        </h2>
        <div class="result__snippet">httpx package.</div>
    </div>
    """
    _mock_html_response(monkeypatch, html)

    results = await DuckDuckGoProvider().search("httpx pypi")

    assert len(results) == 1
    assert results[0].url == "https://pypi.org/httpx"


@pytest.mark.asyncio
async def test_duckduckgo_absolute_redirect_url_cleaned(monkeypatch) -> None:
    """Absolute https://duckduckgo.com/l/?uddg=... redirect links are cleaned."""
    html = """
    <div class="result">
        <h2 class="result__title">
            <a href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fgh.com%2Fagent">GitHub</a>
        </h2>
        <div class="result__snippet">AgentOS repo.</div>
    </div>
    """
    _mock_html_response(monkeypatch, html)

    results = await DuckDuckGoProvider().search("agentos github")

    assert len(results) == 1
    assert results[0].url == "https://gh.com/agent"


@pytest.mark.asyncio
async def test_duckduckgo_direct_url_preserved(monkeypatch) -> None:
    """Direct links without uddg= parameter are preserved intact."""
    html = """
    <div class="result">
        <h2 class="result__title">
            <a href="https://example.com/page">Example Page</a>
        </h2>
        <div class="result__snippet">Example site.</div>
    </div>
    """
    _mock_html_response(monkeypatch, html)

    results = await DuckDuckGoProvider().search("example")

    assert len(results) == 1
    assert results[0].url == "https://example.com/page"
