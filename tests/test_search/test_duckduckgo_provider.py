from __future__ import annotations

import httpx
import pytest

from agentos.search.providers.duckduckgo import DuckDuckGoProvider, _clean_ddg_url
from agentos.search.types import SearchProviderError


def test_clean_ddg_url_relative_and_absolute_redirects() -> None:
    # Relative redirect path
    relative = "/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F&rut=abc123"
    assert _clean_ddg_url(relative) == "https://docs.python.org/3/"

    # Protocol-relative redirect
    proto_rel = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Ftest%3Fq%3D1&rut=xyz"
    assert _clean_ddg_url(proto_rel) == "https://example.com/test?q=1"

    # Fully qualified redirect
    full = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2Fuse-agent-os&rut=123"
    assert _clean_ddg_url(full) == "https://github.com/use-agent-os"

    # Nested query redirect
    nested = (
        "/l/?rut=abc123xyz&uddg=https%3A%2F%2Fexample.org%2Fsearch%3Fq%3Dtest%26page%3D2&other=1"
    )
    assert _clean_ddg_url(nested) == "https://example.org/search?q=test&page=2"

    # Non-DDG URL with uddg query parameter untouched
    non_ddg = "https://example.com/page?uddg=notaredirect"
    assert _clean_ddg_url(non_ddg) == "https://example.com/page?uddg=notaredirect"

    # Direct non-redirect URL
    direct = "https://direct.example.org/resource"
    assert _clean_ddg_url(direct) == direct

    # Empty string
    assert _clean_ddg_url("") == ""


@pytest.mark.asyncio
async def test_duckduckgo_search_parses_html_and_cleans_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html_content = """
    <div class="result">
        <h2 class="result__title">
            <a href="/l/?uddg=https%3A%2F%2Fexample.com%2Ftarget&rut=123">Example Domain</a>
        </h2>
        <a class="result__snippet">Example domain snippet content</a>
    </div>
    <div class="result">
        <h2 class="result__title">
            <a href="https://ad.duckduckgo.com/y.js?ad_id=123">Ad Title</a>
        </h2>
        <a class="result__snippet">Ad snippet</a>
    </div>
    """

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def post(self, url: str, data: dict, headers: dict) -> httpx.Response:
            return httpx.Response(200, text=html_content, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    provider = DuckDuckGoProvider()
    results = await provider.search("test query", max_results=5)

    assert len(results) == 1
    assert results[0].title == "Example Domain"
    assert results[0].url == "https://example.com/target"
    assert results[0].snippet == "Example domain snippet content"
    assert results[0].source == "duckduckgo"


@pytest.mark.asyncio
async def test_duckduckgo_search_raises_search_provider_error_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimeoutClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> TimeoutClient:
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def post(self, url: str, data: dict, headers: dict) -> httpx.Response:
            raise httpx.ReadTimeout("Connection timed out", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", TimeoutClient)

    provider = DuckDuckGoProvider()
    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("query")

    assert exc_info.value.provider == "duckduckgo"
    assert exc_info.value.kind == "timeout"
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_kind", "expected_retryable"),
    [
        (401, "auth", False),
        (403, "auth", False),
        (429, "rate_limit", True),
        (500, "http", True),
        (503, "http", True),
    ],
)
async def test_duckduckgo_search_raises_classified_http_status_error(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected_kind: str,
    expected_retryable: bool,
) -> None:
    class StatusErrorClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> StatusErrorClient:
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def post(self, url: str, data: dict, headers: dict) -> httpx.Response:
            req = httpx.Request("POST", url)
            resp = httpx.Response(status_code, request=req)
            resp.raise_for_status()
            return resp

    monkeypatch.setattr(httpx, "AsyncClient", StatusErrorClient)

    provider = DuckDuckGoProvider()
    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("query")

    assert exc_info.value.provider == "duckduckgo"
    assert exc_info.value.kind == expected_kind
    assert exc_info.value.status_code == status_code
    assert exc_info.value.retryable is expected_retryable


@pytest.mark.asyncio
async def test_duckduckgo_search_opt_out_diagnostics_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ErrorClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> ErrorClient:
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def post(self, url: str, data: dict, headers: dict) -> httpx.Response:
            raise httpx.ConnectError("Host unreachable", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", ErrorClient)

    provider = DuckDuckGoProvider(diagnostics=False)
    results = await provider.search("query")
    assert results == []
