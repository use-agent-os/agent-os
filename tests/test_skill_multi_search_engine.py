"""multi-search-engine skill — load + missing-key engines fail soft."""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

import pytest

from agentos.skills.eligibility import EligibilityContext, check_eligibility
from agentos.skills.loader import SkillLoader

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "agentos" / "skills" / "bundled"
SCRIPTS = BUNDLED / "multi-search-engine" / "scripts"


def _spec() -> object:
    return SkillLoader(bundled_dir=BUNDLED).get_by_name("multi-search-engine")


def test_skill_loads() -> None:
    spec = _spec()
    assert spec is not None
    assert spec.name == "multi-search-engine"


def test_eligibility_with_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentos.skills.eligibility.shutil.which",
        lambda name: "/usr/bin/python3" if name in {"python", "python3"} else None,
    )
    spec = _spec()
    assert spec is not None
    assert check_eligibility(spec, EligibilityContext.auto())


def test_engine_keys_are_declared_optional_so_no_key_hides_the_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every key unlocks an engine; none is a reason to withhold the skill."""
    for name in (
        "BRAVE_SEARCH_API_KEY",
        "TAVILY_API_KEY",
        "SERPAPI_API_KEY",
        "FIRECRAWL_API_KEY",
        "XAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "agentos.skills.eligibility.shutil.which",
        lambda name: "/usr/bin/python3" if name in {"python", "python3"} else None,
    )
    spec = _spec()
    assert spec is not None
    declared = {e.name: e.required for e in spec.metadata.requires.env}
    assert declared == {
        "BRAVE_SEARCH_API_KEY": False,
        "TAVILY_API_KEY": False,
        "SERPAPI_API_KEY": False,
        "FIRECRAWL_API_KEY": False,
        "XAI_API_KEY": False,
    }
    assert check_eligibility(spec, EligibilityContext.auto())


def test_brave_without_key_fails_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    """Engine missing its API key must not crash the run; record an error and continue."""
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    sys.path.insert(0, str(SCRIPTS))
    try:
        import search  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    payload = search.search_all(
        query="anything",
        engines=["brave"],
        limit=3,
        strict=False,
    )
    assert payload["query"] == "anything"
    assert payload["results"] == []
    assert any("BRAVE_SEARCH_API_KEY/BRAVE_API_KEY" in e["reason"] for e in payload["errors"])


def test_brave_accepts_current_search_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """AgentOS config uses BRAVE_SEARCH_API_KEY; the skill must honor it."""
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-current")
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    sys.path.insert(0, str(SCRIPTS))
    try:
        import search  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    captured: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "web": {
                    "results": [
                        {
                            "title": "Example",
                            "url": "https://example.com",
                            "description": "Snippet",
                        },
                    ],
                },
            }

    class _Client:
        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str, *, params: dict[str, object], headers: dict[str, str]) -> _Response:
            captured["headers"] = headers
            captured["params"] = params
            return _Response()

    monkeypatch.setattr(search, "_client", lambda: _Client())

    payload = search.search_all(
        query="anything",
        engines=["brave"],
        limit=1,
        strict=False,
    )

    assert payload["errors"] == []
    assert payload["results"][0]["url"] == "https://example.com"
    assert captured["headers"]["X-Subscription-Token"] == "brave-current"


def test_search_query_contract_extracts_planner_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Meta report planners may pass SEARCH_QUERY plus preferences; engines get only the query."""
    sys.path.insert(0, str(SCRIPTS))
    try:
        import search  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    captured: dict[str, object] = {}

    def fake_engine(query: str, limit: int) -> list[object]:
        captured["query"] = query
        captured["limit"] = limit
        return []

    monkeypatch.setitem(search.ENGINES, "fake", fake_engine)
    payload = search.search_all(
        query=(
            "SEARCH_QUERY: local-first AI coding assistants 2026 pros cons\n"
            "AUDIENCE: CTO\n"
            "REPORT_TYPE: technical"
        ),
        engines=["fake"],
        limit=7,
        strict=False,
    )

    assert payload["query"] == "local-first AI coding assistants 2026 pros cons"
    assert captured == {
        "query": "local-first AI coding assistants 2026 pros cons",
        "limit": 7,
    }


def test_unknown_engine_recorded() -> None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import search  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    payload = search.search_all(
        query="x",
        engines=["bogus-engine-name"],
        limit=1,
        strict=False,
    )
    assert payload["results"] == []
    assert any("unknown engine" in e["reason"] for e in payload["errors"])


def test_search_creates_parent_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import search  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    monkeypatch.setattr(
        search,
        "search_all",
        lambda *args, **kwargs: {"query": "q", "results": [], "errors": []},
    )
    out = tmp_path / "nested" / "dir" / "out.json"
    monkeypatch.setattr(sys, "argv", ["search.py", "--query", "test", "--out", str(out)])
    assert search.main() == 0
    assert out.is_file()


# --- helpers for the engine tests below ---------------------------------------


def _import_search() -> object:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import search  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    return search


def _clear_engine_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "BRAVE_SEARCH_API_KEY",
        "BRAVE_API_KEY",
        "TAVILY_API_KEY",
        "SERPAPI_API_KEY",
        "FIRECRAWL_API_KEY",
        "XAI_API_KEY",
        "AGENTOS_X_SEARCH_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


def _jwt(exp: float) -> str:
    """Unsigned JWT-shaped token whose payload carries ``exp``."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def _write_auth_store(path: Path, access_token: str, base_url: str = "https://api.x.ai/v1") -> None:
    path.write_text(
        json.dumps(
            {
                "providers": {
                    "xai-oauth": {
                        "tokens": {"access_token": access_token, "refresh_token": "r"},
                        "base_url": base_url,
                    }
                }
            }
        ),
        encoding="utf-8",
    )


class _Response:
    def __init__(
        self, *, text: str = "", status_code: int = 200, payload: dict[str, object] | None = None
    ) -> None:
        self.text = text
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class _FakeClient:
    """Records every call; ``responses`` are handed out in order."""

    def __init__(self, responses: list[_Response]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def _next(self, method: str, url: str, kwargs: dict[str, object]) -> _Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._responses.pop(0)

    def post(self, url: str, **kwargs: object) -> _Response:
        return self._next("post", url, kwargs)

    def get(self, url: str, **kwargs: object) -> _Response:
        return self._next("get", url, kwargs)


_DDG_HTML = (
    "<html><body>"
    '<div class="result">'
    '<a class="result__a" href="https://duckduckgo.com/y.js?ad_id=123">Sponsored Ad</a>'
    '<a class="result__snippet">Ad snippet</a>'
    "</div>"
    '<div class="result">'
    '<a class="result__a" href="//duckduckgo.com/l/?uddg='
    'https%3A%2F%2Fexample.com%2Fdocs%3Fid%3D10&rut=abc">Target One</a>'
    '<a class="result__snippet">Most <b>day-to-day</b> <b>AgentOS</b> surfaces</a>'
    "</div>"
    '<div class="result">'
    '<a class="result__a" href="/l/?uddg='
    'https%3A%2F%2Fdocs.example.org%2Fguide&rut=def">Target Two</a>'
    '<a class="result__snippet">Snippet 2</a>'
    "</div>"
    '<div class="result">'
    '<a class="result__a" href="https://direct.example.net/info">Target Three</a>'
    '<a class="result__snippet">Snippet 3</a>'
    "</div>"
    "</body></html>"
)

_DDG_CHALLENGE_HTML = (
    "<html><body><div class='anomaly-modal__title'>"
    "Unfortunately, bots use DuckDuckGo too.</div></body></html>"
)


# --- duckduckgo -----------------------------------------------------------------


def test_ddg_unquotes_redirects_skips_ads_and_keeps_spacing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1917: clean target URLs, no y.js ads, ranks sequential, limit applied after filtering."""
    search = _import_search()
    client = _FakeClient([_Response(text=_DDG_HTML)])
    monkeypatch.setattr(search, "_client", lambda: client)

    payload = search.search_all(query="q", engines=["duckduckgo"], limit=2, strict=False)

    assert payload["errors"] == []
    assert [r["url"] for r in payload["results"]] == [
        "https://example.com/docs?id=10",
        "https://docs.example.org/guide",
    ]
    assert [r["rank"] for r in payload["results"]] == [1, 2]
    # ``get_text(strip=True)`` used to glue words across <b> boundaries.
    assert payload["results"][0]["snippet"] == "Most day-to-day AgentOS surfaces"
    assert len(client.calls) == 1


def test_ddg_bot_challenge_is_reported_not_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """DDG answers a challenge with HTTP 202 + an anomaly page: retry once, then record an error."""
    search = _import_search()
    monkeypatch.setattr(search, "_DDG_CHALLENGE_RETRY_DELAY_S", 0.0)
    client = _FakeClient(
        [
            _Response(text=_DDG_CHALLENGE_HTML, status_code=202),
            _Response(text=_DDG_CHALLENGE_HTML, status_code=202),
        ]
    )
    monkeypatch.setattr(search, "_client", lambda: client)

    payload = search.search_all(query="q", engines=["duckduckgo"], limit=3, strict=False)

    assert payload["results"] == []
    assert len(client.calls) == 2
    assert any("bot challenge" in e["reason"] for e in payload["errors"])


def test_ddg_bot_challenge_recovers_on_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    search = _import_search()
    monkeypatch.setattr(search, "_DDG_CHALLENGE_RETRY_DELAY_S", 0.0)
    client = _FakeClient(
        [
            _Response(text=_DDG_CHALLENGE_HTML, status_code=202),
            _Response(text=_DDG_HTML),
        ]
    )
    monkeypatch.setattr(search, "_client", lambda: client)

    payload = search.search_all(query="q", engines=["duckduckgo"], limit=5, strict=False)

    assert payload["errors"] == []
    assert len(payload["results"]) == 3
    assert len(client.calls) == 2


# --- serpapi --------------------------------------------------------------------


def test_serpapi_without_key_fails_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_engine_keys(monkeypatch)
    search = _import_search()
    payload = search.search_all(query="q", engines=["serpapi"], limit=3, strict=False)
    assert payload["results"] == []
    assert any("SERPAPI_API_KEY" in e["reason"] for e in payload["errors"])


def test_serpapi_maps_organic_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERPAPI_API_KEY", "serp-key")
    search = _import_search()
    client = _FakeClient(
        [
            _Response(
                payload={
                    "organic_results": [
                        {"title": "One", "link": "https://one.example", "snippet": "S1"},
                        {"title": "Two", "link": "https://two.example", "snippet": "S2"},
                    ]
                }
            )
        ]
    )
    monkeypatch.setattr(search, "_client", lambda: client)

    payload = search.search_all(query="q", engines=["serpapi"], limit=1, strict=False)

    assert payload["errors"] == []
    assert payload["results"] == [
        {
            "engine": "serpapi",
            "title": "One",
            "url": "https://one.example",
            "snippet": "S1",
            "rank": 1,
        }
    ]
    call = client.calls[0]
    assert call["url"] == "https://serpapi.com/search.json"
    assert call["params"]["api_key"] == "serp-key"
    assert call["params"]["num"] == 1


# --- firecrawl ------------------------------------------------------------------


def test_firecrawl_without_key_fails_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_engine_keys(monkeypatch)
    search = _import_search()
    payload = search.search_all(query="q", engines=["firecrawl"], limit=3, strict=False)
    assert payload["results"] == []
    assert any("FIRECRAWL_API_KEY" in e["reason"] for e in payload["errors"])


def test_firecrawl_maps_v2_web_results_metadata_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-key")
    search = _import_search()
    client = _FakeClient(
        [
            _Response(
                payload={
                    "success": True,
                    "data": {
                        "web": [
                            {
                                "title": "One",
                                "url": "https://one.example",
                                "description": "D1",
                                "markdown": None,
                            },
                            {"title": "Two", "url": "https://two.example", "description": "D2"},
                        ],
                        "news": [{"title": "ignored", "url": "https://news.example"}],
                    },
                    "creditsUsed": 2,
                }
            )
        ]
    )
    monkeypatch.setattr(search, "_client", lambda: client)

    payload = search.search_all(query="q" * 600, engines=["firecrawl"], limit=150, strict=False)

    assert payload["errors"] == []
    assert [(r["rank"], r["url"], r["snippet"]) for r in payload["results"]] == [
        (1, "https://one.example", "D1"),
        (2, "https://two.example", "D2"),
    ]
    call = client.calls[0]
    assert call["url"] == "https://api.firecrawl.dev/v2/search"
    assert call["headers"]["Authorization"] == "Bearer fc-key"
    assert call["json"]["sources"] == [{"type": "web"}]
    assert "scrapeOptions" not in call["json"]  # metadata only, no per-page scrape credits
    assert call["json"]["limit"] == 100  # API hard cap
    assert len(call["json"]["query"]) == 500  # API max query length
    assert call["timeout"] == search.FIRECRAWL_TIMEOUT_S


def test_firecrawl_accepts_legacy_flat_data_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-key")
    search = _import_search()
    client = _FakeClient(
        [_Response(payload={"success": True, "data": [{"title": "T", "url": "https://t.example"}]})]
    )
    monkeypatch.setattr(search, "_client", lambda: client)
    payload = search.search_all(query="q", engines=["firecrawl"], limit=5, strict=False)
    assert payload["results"][0]["url"] == "https://t.example"


def test_firecrawl_unsuccessful_body_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-key")
    search = _import_search()
    client = _FakeClient([_Response(payload={"success": False, "error": "Insufficient credits"})])
    monkeypatch.setattr(search, "_client", lambda: client)
    payload = search.search_all(query="q", engines=["firecrawl"], limit=5, strict=False)
    assert payload["results"] == []
    assert any("Insufficient credits" in e["reason"] for e in payload["errors"])


# --- x (xAI x_search) -----------------------------------------------------------

_X_PAYLOAD: dict[str, object] = {
    "output": [
        {
            "type": "message",
            "content": [
                {
                    "type": "output_text",
                    "text": "AgentOS is a local-first agent runtime.",
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url": "https://x.com/useAgentOS/status/1",
                            "title": "1",
                        },
                        {
                            "type": "url_citation",
                            "url": "https://x.com/someone/status/2",
                            "title": "Someone on X",
                        },
                    ],
                }
            ],
        }
    ],
    "citations": [
        "https://x.com/useAgentOS/status/1",
        "https://x.com/third/status/3",
    ],
}


def test_x_search_uses_oauth_token_and_maps_citations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_engine_keys(monkeypatch)
    store = tmp_path / "auth.json"
    token = _jwt(exp=time.time() + 3600)
    _write_auth_store(store, token)
    monkeypatch.setenv("AGENTOS_AUTH_STORE", str(store))
    search = _import_search()
    client = _FakeClient([_Response(payload=_X_PAYLOAD)])
    monkeypatch.setattr(search, "_client", lambda: client)

    payload = search.search_all(query="q", engines=["x"], limit=10, strict=False)

    assert payload["errors"] == []
    assert payload["answers"] == [
        {"engine": "x", "text": "AgentOS is a local-first agent runtime."}
    ]
    assert [(r["rank"], r["url"], r["title"]) for r in payload["results"]] == [
        # Numeric annotation titles are citation indexes, not titles → URL fallback.
        (1, "https://x.com/useAgentOS/status/1", "https://x.com/useAgentOS/status/1"),
        (2, "https://x.com/someone/status/2", "Someone on X"),
        (3, "https://x.com/third/status/3", "https://x.com/third/status/3"),
    ]
    call = client.calls[0]
    assert call["url"] == "https://api.x.ai/v1/responses"
    assert call["headers"]["Authorization"] == f"Bearer {token}"
    assert call["json"]["tools"] == [{"type": "x_search"}]
    assert call["json"]["store"] is False
    assert call["json"]["model"] == "grok-4.5"
    assert call["timeout"] == search.X_SEARCH_TIMEOUT_S
    # The script must never touch the token store (refresh is the gateway's job).
    assert (
        json.loads(store.read_text())["providers"]["xai-oauth"]["tokens"]["access_token"] == token
    )


def test_x_search_limit_caps_citations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_engine_keys(monkeypatch)
    monkeypatch.setenv("XAI_API_KEY", "xai-key")
    monkeypatch.setenv("AGENTOS_AUTH_STORE", str(tmp_path / "missing.json"))
    search = _import_search()
    client = _FakeClient([_Response(payload=_X_PAYLOAD)])
    monkeypatch.setattr(search, "_client", lambda: client)

    payload = search.search_all(query="q", engines=["x"], limit=1, strict=False)

    assert len(payload["results"]) == 1
    assert client.calls[0]["headers"]["Authorization"] == "Bearer xai-key"


def test_x_search_expired_oauth_without_key_fails_soft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_engine_keys(monkeypatch)
    store = tmp_path / "auth.json"
    _write_auth_store(store, _jwt(exp=time.time() - 10))
    monkeypatch.setenv("AGENTOS_AUTH_STORE", str(store))
    search = _import_search()
    client = _FakeClient([])
    monkeypatch.setattr(search, "_client", lambda: client)

    payload = search.search_all(query="q", engines=["x"], limit=3, strict=False)

    assert payload["results"] == []
    assert client.calls == []
    assert any("expired" in e["reason"] for e in payload["errors"])


def test_x_search_expired_oauth_falls_back_to_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_engine_keys(monkeypatch)
    store = tmp_path / "auth.json"
    _write_auth_store(store, _jwt(exp=time.time() - 10))
    monkeypatch.setenv("AGENTOS_AUTH_STORE", str(store))
    monkeypatch.setenv("XAI_API_KEY", "xai-key")
    search = _import_search()
    client = _FakeClient([_Response(payload=_X_PAYLOAD)])
    monkeypatch.setattr(search, "_client", lambda: client)

    payload = search.search_all(query="q", engines=["x"], limit=3, strict=False)

    assert payload["errors"] == []
    assert client.calls[0]["headers"]["Authorization"] == "Bearer xai-key"


def test_x_search_without_any_credential_fails_soft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_engine_keys(monkeypatch)
    monkeypatch.setenv("AGENTOS_AUTH_STORE", str(tmp_path / "missing.json"))
    search = _import_search()
    payload = search.search_all(query="q", engines=["x"], limit=3, strict=False)
    assert payload["results"] == []
    assert any("XAI_API_KEY" in e["reason"] for e in payload["errors"])


# --- auto -----------------------------------------------------------------------


def test_auto_without_keys_is_duckduckgo_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_engine_keys(monkeypatch)
    monkeypatch.setenv("AGENTOS_AUTH_STORE", str(tmp_path / "missing.json"))
    search = _import_search()
    assert search.resolve_engines(["auto"]) == ["duckduckgo"]


def test_auto_adds_keyed_engines_and_x_when_credentialed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_engine_keys(monkeypatch)
    monkeypatch.setenv("SERPAPI_API_KEY", "serp-key")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-key")
    store = tmp_path / "auth.json"
    _write_auth_store(store, _jwt(exp=time.time() + 3600))
    monkeypatch.setenv("AGENTOS_AUTH_STORE", str(store))
    search = _import_search()
    assert search.resolve_engines(["auto"]) == ["duckduckgo", "serpapi", "firecrawl", "x"]
    # Explicit names mix with auto and are deduplicated in order.
    assert search.resolve_engines(["brave", "auto", "x"]) == [
        "brave",
        "duckduckgo",
        "serpapi",
        "firecrawl",
        "x",
    ]


def test_search_all_reports_resolved_engines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_engine_keys(monkeypatch)
    monkeypatch.setenv("AGENTOS_AUTH_STORE", str(tmp_path / "missing.json"))
    search = _import_search()
    client = _FakeClient([_Response(text=_DDG_HTML)])
    monkeypatch.setattr(search, "_client", lambda: client)

    payload = search.search_all(query="q", engines=["auto"], limit=3, strict=False)

    assert payload["engines"] == ["duckduckgo"]
    assert payload["errors"] == []


def test_ddg_search_returns_empty_list_for_zero_or_negative_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search = _import_search()

    def _fail_client():
        raise AssertionError("network call must not be made when limit <= 0")

    monkeypatch.setattr(search, "_client", _fail_client)

    assert search._ddg_search("query", 0) == []
    assert search._ddg_search("query", -1) == []
