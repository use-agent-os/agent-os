"""DuckDuckGo search provider — HTML scraper via httpx."""

from __future__ import annotations

import urllib.parse

import httpx
from bs4 import BeautifulSoup

from agentos.search.registry import register_provider
from agentos.search.types import SearchErrorKind, SearchProviderError, SearchResult

_DDHTML_URL = "https://html.duckduckgo.com/html"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}


def _redirect_target(href: str) -> str:
    """Return the page a DuckDuckGo ``/l/?uddg=`` redirect points at, else ``href``.

    The HTML endpoint spells the redirect relative (``/l/?uddg=``),
    protocol-relative (``//duckduckgo.com/l/?uddg=``) or absolute, so it is
    recognised by its path on an empty or DuckDuckGo host rather than by a
    prefix substring. An organic result that merely carries a ``uddg`` query
    parameter of its own is not a redirect and is left as it is.
    """

    try:
        parts = urllib.parse.urlsplit(href)
        host = (parts.hostname or "").lower()
    except ValueError:
        # A malformed organic link (``http://[bad/``) is not a redirect; the
        # old substring check never raised, and ``diagnostics=False`` promises
        # ``search()`` does not either.
        return href
    if parts.path != "/l/" or (
        host and host != "duckduckgo.com" and not host.endswith(".duckduckgo.com")
    ):
        return href
    target = urllib.parse.parse_qs(parts.query).get("uddg", [""])[0]
    return target or href


class DuckDuckGoProvider:
    """Search provider using DuckDuckGo HTML endpoint."""

    name: str = "duckduckgo"

    def __init__(
        self,
        proxy: str = "",
        use_env_proxy: bool = False,
        diagnostics: bool = True,
    ) -> None:
        self._proxy = proxy or None
        self._trust_env = bool(use_env_proxy) and not self._proxy
        # Default on, so an upstream 403/429/timeout is distinguishable from
        # "no organic results" the way Brave and Tavily already make it.
        # ``diagnostics=False`` is the explicit opt-out for a caller that
        # relies on ``search()`` never raising.
        self._diagnostics = bool(diagnostics)

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        if max_results <= 0:
            return []
        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                proxy=self._proxy,
                trust_env=self._trust_env,
            ) as client:
                response = await client.post(
                    _DDHTML_URL,
                    data={"q": query, "b": "", "kl": ""},
                    headers=_HEADERS,
                )
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            if not self._diagnostics:
                return []
            raise SearchProviderError(
                provider=self.name,
                kind="timeout",
                message=str(exc) or "DuckDuckGo search request timed out.",
                retryable=True,
            ) from exc
        except httpx.HTTPStatusError as exc:
            if not self._diagnostics:
                return []
            status_code = exc.response.status_code
            if status_code in {401, 403}:
                kind: SearchErrorKind = "auth"
            elif status_code == 429:
                kind = "rate_limit"
            else:
                kind = "http"
            raise SearchProviderError(
                provider=self.name,
                kind=kind,
                message=str(exc) or f"DuckDuckGo search failed with HTTP {status_code}.",
                retryable=kind in {"rate_limit", "http"},
                status_code=status_code,
            ) from exc
        except httpx.HTTPError as exc:
            if not self._diagnostics:
                return []
            raise SearchProviderError(
                provider=self.name,
                kind="network",
                message=str(exc) or "DuckDuckGo search network request failed.",
                retryable=True,
            ) from exc

        soup = BeautifulSoup(response.text, "html.parser")
        results: list[SearchResult] = []

        for elem in soup.select(".result"):
            title_a = elem.select_one(".result__title a")
            if not title_a:
                continue

            title = title_a.get_text(strip=True)
            href_value = title_a.get("href", "")
            href = href_value if isinstance(href_value, str) else ""

            # Skip ads
            if "y.js" in href:
                continue

            href = _redirect_target(href)

            snippet_elem = elem.select_one(".result__snippet")
            snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""

            results.append(
                SearchResult(title=title, url=href, snippet=snippet, source="duckduckgo")
            )
            if len(results) >= max_results:
                break

        return results


register_provider("duckduckgo", DuckDuckGoProvider)
