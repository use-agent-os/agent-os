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

            # Clean DDG redirect URLs
            if "/l/?uddg=" in href:
                uddg = href.split("uddg=", 1)[1]
                if "&" in uddg:
                    uddg = uddg.split("&")[0]
                href = urllib.parse.unquote(uddg)

            results.append(
                SearchResult(title=title, url=href, snippet="", source="duckduckgo")
            )
            if len(results) >= max_results:
                break

        return results


register_provider("duckduckgo", DuckDuckGoProvider)
