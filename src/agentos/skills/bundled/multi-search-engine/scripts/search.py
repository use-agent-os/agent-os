"""Query multiple search engines and emit a normalized JSON result list."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

# Bundled scripts run under AgentOS's own interpreter; the path insert only
# matters in a source checkout where the package is not installed (#2804).
_SRC_ROOT = str(Path(__file__).resolve().parents[5])
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)
from agentos.skills.stdio import configure_utf8_stdio  # noqa: E402

USER_AGENT = "Mozilla/5.0 (compatible; AgentOS-multi-search-engine/0.1)"
TIMEOUT_S = 8.0

# xAI's server-side x_search runs 60-120s for a complex query; mirrors the
# default ``[x_search].timeout_seconds`` of the built-in tool.
X_SEARCH_TIMEOUT_S = 180.0
X_SEARCH_DEFAULT_MODEL = "grok-4.5"
X_SEARCH_DEFAULT_BASE_URL = "https://api.x.ai/v1"
XAI_OAUTH_PROVIDER_ID = "xai-oauth"
# Treat an OAuth access token as unusable this close to its ``exp`` so a
# request started now does not expire mid-flight.
_XAI_TOKEN_MIN_REMAINING_S = 60.0

# DuckDuckGo answers a bot challenge with HTTP 202 and an "anomaly" page
# instead of a 4xx, so ``raise_for_status()`` never fires on it.
_DDG_CHALLENGE_STATUS = 202
_DDG_CHALLENGE_RETRY_DELAY_S = 1.5


@dataclass
class Result:
    engine: str
    title: str
    url: str
    snippet: str
    rank: int


@dataclass
class EngineError:
    engine: str
    reason: str


@dataclass
class EngineOutput:
    """Ranked results plus an optional synthesized answer (x_search)."""

    results: list[Result]
    answer: str = ""


def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"},
        follow_redirects=True,
        timeout=TIMEOUT_S,
    )


def _is_ddg_challenge(response: httpx.Response, soup: BeautifulSoup) -> bool:
    if response.status_code == _DDG_CHALLENGE_STATUS:
        return True
    return "anomaly" in response.text.lower() and not soup.select("div.result")


def _ddg_search(query: str, limit: int) -> list[Result]:
    with _client() as client:
        soup: BeautifulSoup | None = None
        for attempt in range(2):
            response = client.post("https://html.duckduckgo.com/html/", data={"q": query})
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            if not _is_ddg_challenge(response, soup):
                break
            if attempt == 0:
                time.sleep(_DDG_CHALLENGE_RETRY_DELAY_S)
                continue
            raise RuntimeError(
                "DuckDuckGo bot challenge (HTTP 202) — rate-limited; "
                "retry later or use another engine"
            )
        assert soup is not None
        results: list[Result] = []
        for item in soup.select("div.result"):
            title_el = item.select_one("a.result__a")
            snippet_el = item.select_one("a.result__snippet")
            if title_el is None:
                continue
            href_value = title_el.get("href", "")
            href = href_value if isinstance(href_value, str) else ""
            # Sponsored links route through y.js; organic ones through /l/?uddg=.
            if not href or "y.js" in href:
                continue
            if "uddg=" in href:
                href = urllib.parse.unquote(href.split("uddg=")[1].split("&")[0])
            results.append(
                Result(
                    engine="duckduckgo",
                    title=title_el.get_text(" ", strip=True),
                    url=href,
                    snippet=snippet_el.get_text(" ", strip=True) if snippet_el is not None else "",
                    rank=len(results) + 1,
                )
            )
            if len(results) >= limit:
                break
        return results


_BRAVE_MAX_COUNT = 20  # Brave Web Search API hard-caps `count` at 20; >20 → HTTP 422.


def _brave_search(query: str, limit: int) -> list[Result]:
    api_key = os.environ.get("BRAVE_SEARCH_API_KEY") or os.environ.get("BRAVE_API_KEY")
    if not api_key:
        raise RuntimeError("BRAVE_SEARCH_API_KEY/BRAVE_API_KEY not set; skipping")
    effective_count = min(max(limit, 1), _BRAVE_MAX_COUNT)
    if limit > _BRAVE_MAX_COUNT:
        print(
            f"[multi-search-engine] brave count clamped {limit}→{_BRAVE_MAX_COUNT} (API hard-cap)",
            file=sys.stderr,
        )
    with _client() as client:
        response = client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": effective_count},
            headers={"X-Subscription-Token": api_key},
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("web", {}).get("results", []) or []
        results: list[Result] = []
        for idx, item in enumerate(items[:limit], start=1):
            results.append(
                Result(
                    engine="brave",
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("description", ""),
                    rank=idx,
                )
            )
        return results


def _tavily_search(query: str, limit: int) -> list[Result]:
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY not set; skipping")
    with _client() as client:
        response = client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": "basic",
                "max_results": limit,
            },
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("results", []) or []
        results: list[Result] = []
        for idx, item in enumerate(items[:limit], start=1):
            results.append(
                Result(
                    engine="tavily",
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("content", ""),
                    rank=idx,
                )
            )
        return results


def _serpapi_search(query: str, limit: int) -> list[Result]:
    api_key = os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        raise RuntimeError("SERPAPI_API_KEY not set; skipping")
    with _client() as client:
        response = client.get(
            "https://serpapi.com/search.json",
            params={"q": query, "engine": "google", "num": limit, "api_key": api_key},
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("organic_results", []) or []
        results: list[Result] = []
        for idx, item in enumerate(items[:limit], start=1):
            results.append(
                Result(
                    engine="serpapi",
                    title=item.get("title", ""),
                    url=item.get("link", ""),
                    snippet=item.get("snippet", ""),
                    rank=idx,
                )
            )
        return results


_FIRECRAWL_MAX_LIMIT = 100  # Firecrawl /v2/search caps `limit` at 100 per source.
_FIRECRAWL_MAX_QUERY_CHARS = 500
# A Firecrawl search runs a live crawl behind the API; its own default timeout
# is 60s, so the skill's 8s default would cut most calls short.
FIRECRAWL_TIMEOUT_S = 30.0


def _firecrawl_search(query: str, limit: int) -> list[Result]:
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        raise RuntimeError("FIRECRAWL_API_KEY not set; skipping")
    with _client() as client:
        response = client.post(
            "https://api.firecrawl.dev/v2/search",
            json={
                "query": query[:_FIRECRAWL_MAX_QUERY_CHARS],
                "limit": min(max(limit, 1), _FIRECRAWL_MAX_LIMIT),
                # Metadata only: no scrapeOptions, so no per-result page scrape
                # is billed and the call returns in seconds instead of a minute.
                "sources": [{"type": "web"}],
            },
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=FIRECRAWL_TIMEOUT_S,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success", True):
            raise RuntimeError(f"firecrawl: {payload.get('error') or 'unsuccessful response'}")
        data = payload.get("data") or {}
        # v2 nests by source; v1 returned a flat list.
        items = data.get("web", []) if isinstance(data, dict) else data
        results: list[Result] = []
        for idx, item in enumerate((items or [])[:limit], start=1):
            results.append(
                Result(
                    engine="firecrawl",
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("description", ""),
                    rank=idx,
                )
            )
        return results


# --- xAI x_search -----------------------------------------------------------


def _xai_auth_store_path() -> Path:
    override = os.environ.get("AGENTOS_AUTH_STORE", "").strip()
    if override:
        return Path(override).expanduser()
    state_dir = os.environ.get("AGENTOS_STATE_DIR", "").strip()
    home = Path(state_dir).expanduser() if state_dir else Path.home() / ".agentos"
    return home / "auth.json"


def _jwt_expiry(token: str) -> float | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    segment = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(segment.encode("ascii")))
    except (ValueError, UnicodeDecodeError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    # Tuple, not ``int | float``: a runtime union needs 3.10, and this script
    # must at least fail loudly-but-gracefully on an older PATH python.
    return float(exp) if isinstance(exp, (int, float)) else None


@dataclass
class XaiCredential:
    token: str
    base_url: str
    source: str  # "xai-oauth" | "xai"


def _read_oauth_access_token() -> tuple[str | None, str, bool]:
    """Return ``(access_token, base_url, expired)`` from the AgentOS auth store.

    Never refreshes: xAI refresh tokens are single-use and the gateway owns
    that path. An expiring token is reported as unusable instead.
    """
    path = _xai_auth_store_path()
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, X_SEARCH_DEFAULT_BASE_URL, False
    providers = store.get("providers") if isinstance(store, dict) else None
    state = providers.get(XAI_OAUTH_PROVIDER_ID) if isinstance(providers, dict) else None
    if not isinstance(state, dict):
        return None, X_SEARCH_DEFAULT_BASE_URL, False
    tokens = state.get("tokens")
    access_token = str(tokens.get("access_token") or "").strip() if isinstance(tokens, dict) else ""
    base_url = str(state.get("base_url") or "").strip().rstrip("/")
    if not base_url.startswith("https://"):
        base_url = X_SEARCH_DEFAULT_BASE_URL
    if not access_token:
        return None, base_url, False
    exp = _jwt_expiry(access_token)
    if exp is not None and exp <= time.time() + _XAI_TOKEN_MIN_REMAINING_S:
        return None, base_url, True
    return access_token, base_url, False


def _resolve_xai_credential() -> XaiCredential:
    oauth_token, base_url, expired = _read_oauth_access_token()
    if oauth_token:
        return XaiCredential(oauth_token, base_url, "xai-oauth")
    api_key = os.environ.get("XAI_API_KEY", "").strip()
    if api_key:
        return XaiCredential(api_key, X_SEARCH_DEFAULT_BASE_URL, "xai")
    if expired:
        raise RuntimeError(
            "xAI OAuth login token is expired and XAI_API_KEY is not set; the gateway "
            "refreshes the login on its next x_search turn (or run `agentos auth login xai`); "
            "skipping"
        )
    raise RuntimeError(
        "xAI credential not found (run `agentos auth login xai` or set XAI_API_KEY); skipping"
    )


def _has_xai_credential() -> bool:
    try:
        _resolve_xai_credential()
    except RuntimeError:
        return False
    return True


def _x_answer_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    chunks: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict) or part.get("type") not in {"output_text", "text"}:
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
    return "\n\n".join(chunks)


def _x_citations(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Return ``(url, title)`` pairs, inline annotations first, deduplicated."""
    seen: set[str] = set()
    cited: list[tuple[str, str]] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            for annotation in part.get("annotations") or []:
                if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
                    continue
                url = str(annotation.get("url") or "").strip()
                if url and url not in seen:
                    seen.add(url)
                    cited.append((url, str(annotation.get("title") or "").strip()))
    for raw in payload.get("citations") or []:
        url = str(raw or "").strip()
        if url and url not in seen:
            seen.add(url)
            cited.append((url, ""))
    return cited


def _x_search(query: str, limit: int) -> EngineOutput:
    credential = _resolve_xai_credential()
    body = {
        "model": os.environ.get("AGENTOS_X_SEARCH_MODEL", "").strip() or X_SEARCH_DEFAULT_MODEL,
        "input": [{"role": "user", "content": query}],
        "tools": [{"type": "x_search"}],
        "store": False,
    }
    with _client() as client:
        response = client.post(
            f"{credential.base_url}/responses",
            json=body,
            headers={
                "Authorization": f"Bearer {credential.token}",
                "Content-Type": "application/json",
                "User-Agent": "AgentOS/multi-search-engine x_search",
            },
            timeout=X_SEARCH_TIMEOUT_S,
        )
        response.raise_for_status()
        payload = response.json()
    # xAI labels inline annotations with the citation index ("1", "2"), which
    # is not a title; fall back to the URL for those.
    results = [
        Result(
            engine="x",
            title=title if title and not title.isdigit() else url,
            url=url,
            snippet="",
            rank=idx,
        )
        for idx, (url, title) in enumerate(_x_citations(payload)[:limit], start=1)
    ]
    return EngineOutput(results=results, answer=_x_answer_text(payload))


EngineHandler = Callable[[str, int], "list[Result] | EngineOutput"]

ENGINES: dict[str, EngineHandler] = {
    "duckduckgo": _ddg_search,
    "brave": _brave_search,
    "tavily": _tavily_search,
    "serpapi": _serpapi_search,
    "firecrawl": _firecrawl_search,
    "x": _x_search,
}

# Engine → env vars, any of which makes it eligible for ``auto``.
_KEYED_ENGINES: dict[str, tuple[str, ...]] = {
    "brave": ("BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY"),
    "tavily": ("TAVILY_API_KEY",),
    "serpapi": ("SERPAPI_API_KEY",),
    "firecrawl": ("FIRECRAWL_API_KEY",),
}


def resolve_engines(requested: list[str]) -> list[str]:
    """Expand ``auto`` into every engine that can actually run here."""
    resolved: list[str] = []
    for name in requested:
        expansion = [name]
        if name == "auto":
            expansion = ["duckduckgo"]
            expansion.extend(
                engine
                for engine, env_names in _KEYED_ENGINES.items()
                if any(os.environ.get(env) for env in env_names)
            )
            if _has_xai_credential():
                expansion.append("x")
        for engine in expansion:
            if engine not in resolved:
                resolved.append(engine)
    return resolved


def _normalize_query(query: str) -> str:
    """Extract the actual web query from structured planner output."""
    lines = [line.strip() for line in query.splitlines() if line.strip()]
    for line in lines:
        if line.upper().startswith("SEARCH_QUERY:"):
            extracted = line.split(":", 1)[1].strip()
            if extracted:
                return extracted
    return query.strip()


def search_all(
    query: str,
    engines: list[str],
    limit: int,
    strict: bool,
) -> dict[str, object]:
    normalized_query = _normalize_query(query)
    engines = resolve_engines(engines)
    results: list[dict[str, object]] = []
    answers: list[dict[str, str]] = []
    errors: list[EngineError] = []
    handlers: list[tuple[str, EngineHandler | None, str | None]] = []
    for name in engines:
        handler = ENGINES.get(name)
        if handler is None:
            handlers.append((name, None, "unknown engine"))
            if strict:
                break
            continue
        handlers.append((name, handler, None))

    def _run_engine(handler: EngineHandler) -> EngineOutput:
        output = handler(normalized_query, limit)
        if isinstance(output, EngineOutput):
            return output
        return EngineOutput(results=output)

    max_workers = max(1, len(handlers))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            name: executor.submit(_run_engine, handler)
            for name, handler, known_error in handlers
            if handler is not None and known_error is None
        }

        for name, _handler, known_error in handlers:
            if known_error is not None:
                errors.append(EngineError(name, known_error))
                if strict:
                    break
                continue
            try:
                output = futures[name].result()
            except Exception as exc:  # network, key missing, parser breaks — keep going
                errors.append(EngineError(name, str(exc)))
                if strict:
                    break
                continue
            for r in output.results:
                results.append(r.__dict__)
            if output.answer:
                answers.append({"engine": name, "text": output.answer})
    return {
        "query": normalized_query,
        "engines": engines,
        "results": results,
        "answers": answers,
        "errors": [e.__dict__ for e in errors],
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Multi-engine web search.")
    parser.add_argument("--query", required=True)
    parser.add_argument(
        "--engines",
        default="auto",
        help=(
            "Comma-separated engine list (auto,duckduckgo,brave,tavily,serpapi,firecrawl,x). "
            "`auto` = duckduckgo + every key-backed engine whose key is set + x when an "
            "xAI credential is available."
        ),
    )
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--strict", action="store_true", help="Fail on first engine error")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="(default; kept for clarity)")
    return parser.parse_args()


def main() -> int:
    configure_utf8_stdio()
    args = _parse_args()
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    payload = search_all(args.query, engines, args.limit, args.strict)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded, encoding="utf-8")
    else:
        sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
