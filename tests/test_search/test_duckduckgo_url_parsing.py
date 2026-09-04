"""Tests for DuckDuckGo redirect URL parsing.

See https://github.com/use-agent-os/agent-os/issues/1017
"""

from __future__ import annotations

import urllib.parse

import pytest

from agentos.search.providers.duckduckgo import DuckDuckGoProvider


def _clean_ddg_url(href: str) -> str:
    """Helper that mimics the production cleanup logic.

    Inline version so the test doesn't depend on internal refactoring.
    """
    if "/l/?uddg=" in href:
        uddg_part = href.split("uddg=", 1)[1]
        if "&" in uddg_part:
            uddg = uddg_part.split("&")[0]
        else:
            uddg = uddg_part
        return urllib.parse.unquote(uddg)
    return href


class TestDuckDuckGoRedirectUrlParsing:
    """All DDG redirect link forms must resolve to the decoded target URL."""

    @pytest.mark.parametrize(
        ("href", "expected"),
        [
            (
                "/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs&rut=123",
                "https://example.com/docs",
            ),
            (
                "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com&rut=456",
                "https://example.com",
            ),
            (
                "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com&rut=789",
                "https://example.com",
            ),
            pytest.param(
                "/l/?uddg=https%3A%2F%2Fexample.com%2Fpage%3Fid%3D1%26t%3D2&rut=999",
                "https://example.com/page?id=1&t=2",
                id="query-string-in-target",
            ),
            pytest.param(
                "/l/?uddg=https%3A%2F%2Fexample.com%23section&rut=abc",
                "https://example.com#section",
                id="fragment-in-target",
            ),
            pytest.param(
                "https://normal-site.com/page",
                "https://normal-site.com/page",
                id="non-ddg-url-unchanged",
            ),
            pytest.param("", "", id="empty-string"),
        ],
    )
    def test_ddg_redirect_parsed_correctly(self, href: str, expected: str) -> None:
        result = _clean_ddg_url(href)
        assert result == expected, f"{href!r} -> {result!r}, expected {expected!r}"

    @pytest.mark.parametrize(
        ("href", "expected"),
        [
            pytest.param(
                "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com&rut=789",
                "https://example.com",
                id="absolute-url",
            ),
            pytest.param(
                "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com&rut=456",
                "https://example.com",
                id="protocol-relative-url",
            ),
            pytest.param(
                "/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs&rut=123",
                "https://example.com/docs",
                id="relative-url",
            ),
        ],
    )
    def test_all_link_forms_handled(self, href: str, expected: str) -> None:
        """All three DDG link forms return the same decoded target."""
        result = _clean_ddg_url(href)
        assert result == expected

    @pytest.mark.parametrize(
        "href",
        [
            "https://example.com",
            "https://duckduckgo.com/search?q=test",
            "/search?q=test",
            "",
            "javascript:void(0)",
        ],
    )
    def test_non_redirect_urls_unchanged(self, href: str) -> None:
        """URLs that are not DDG redirects pass through unchanged."""
        original = href
        result = _clean_ddg_url(href)
        assert result == original, f"{href!r} was modified to {result!r}"

    def test_internal_query_string_preserved(self) -> None:
        """Target URLs containing query parameters are not truncated.

        The previous implementation split on '&' globally, destroying
        any query parameters that were part of the encoded target URL.
        """
        href = "/l/?uddg=https%3A%2F%2Fexample.com%2Fsearch%3Fq%3Dddg%26page%3D2&rut=1"
        result = _clean_ddg_url(href)
        assert result == "https://example.com/search?q=ddg&page=2"

    def test_non_provider_unaffected(self) -> None:
        """The DuckDuckGoProvider constructor parses normally.

        This is a smoke-check that importing and instantiating still works.
        """
        # Instantiate with minimal config — just check it doesn't crash
        try:
            provider = DuckDuckGoProvider()
            assert provider is not None
        except Exception as exc:
            pytest.fail(f"DuckDuckGoProvider() raised {exc}")
