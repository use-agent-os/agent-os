from __future__ import annotations

import json
from typing import Any

import pytest

from agentos.skills.hub.bankr import BankrSource


def _catalog(slug: str, *, install_type: str = "bankr", logo: str | None = None) -> bytes:
    return json.dumps(
        {
            "schemaVersion": 1,
            "slug": slug,
            "provider": slug.title(),
            "providerUrl": f"https://{slug}.example",
            "logo": logo,
            "setup": [f"Install {slug}", "Set env var"],
            "demo": {"title": f"{slug}.sh", "language": "bash", "code": f"{slug} run"},
            "install": {"type": install_type, "repoPath": slug},
        }
    ).encode("utf-8")


class _Response:
    def __init__(
        self,
        *,
        json_data: dict[str, Any] | None = None,
        content: bytes = b"",
        status_code: int = 200,
    ) -> None:
        self._json_data = json_data or {}
        self.content = content
        self.text = content.decode("utf-8", errors="replace")
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _AsyncClient:
    """Mocks the BankrBot/skills tree + catalog.json fetches."""

    tree_entries = [
        {"path": "alchemy/catalog.json", "type": "blob"},
        {"path": "alchemy/SKILL.md", "type": "blob"},
        {"path": "bankr/catalog.json", "type": "blob"},
        {"path": "extern/catalog.json", "type": "blob"},
        {"path": "broken/catalog.json", "type": "blob"},
        {"path": ".github/workflows/ci.yml", "type": "blob"},
        {"path": "nested/dir/catalog.json", "type": "blob"},  # too deep — ignored
    ]
    catalogs = {
        "alchemy": _catalog("alchemy", logo="alchemy.svg"),
        "bankr": _catalog("bankr", logo=None),
        "extern": _catalog("extern", install_type="external"),
        "broken": b"{ not json",
    }
    tree_calls = 0
    catalog_calls = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _AsyncClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _Response:
        if "/git/trees/" in url:
            type(self).tree_calls += 1
            return _Response(json_data={"tree": self.tree_entries, "truncated": False})
        marker = "raw.githubusercontent.com/BankrBot/skills/main/"
        if marker in url:
            type(self).catalog_calls += 1
            slug = url.split(marker, 1)[1].split("/", 1)[0]
            return _Response(content=self.catalogs.get(slug, b"{}"))
        raise AssertionError(f"unexpected URL: {url}")


@pytest.fixture(autouse=True)
def _reset_client_counters() -> None:
    _AsyncClient.tree_calls = 0
    _AsyncClient.catalog_calls = 0


@pytest.mark.asyncio
async def test_search_empty_query_lists_all_bankr_skills(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    results = await BankrSource().search("")

    names = {r.name for r in results}
    # bankr + alchemy kept; external skipped; broken JSON skipped; nested ignored.
    assert names == {"alchemy", "bankr"}
    assert all(r.source_id == "bankr" for r in results)
    assert all(r.trust_level == "community" for r in results)


@pytest.mark.asyncio
async def test_search_builds_provider_logo_and_identifier(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    results = await BankrSource().search("")
    by_name = {r.name: r for r in results}

    alchemy = by_name["alchemy"]
    assert alchemy.provider == "Alchemy"
    assert alchemy.logo == (
        "https://raw.githubusercontent.com/BankrBot/skills/main/alchemy/alchemy.svg"
    )
    assert alchemy.identifier == "https://github.com/BankrBot/skills/tree/main/alchemy"

    # Null logo in the catalog → empty logo (UI renders initials).
    assert by_name["bankr"].logo == ""


@pytest.mark.asyncio
async def test_search_carries_catalog_setup_demo_and_category(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    results = await BankrSource().search("")
    bankr = next(r for r in results if r.name == "bankr")

    assert bankr.setup == ["Install bankr", "Set env var"]
    assert bankr.demo == {"title": "bankr.sh", "language": "bash", "code": "bankr run"}
    # Category is inferred from slug/provider keywords; always non-empty.
    assert bankr.category
    from agentos.skills.hub.bankr import _infer_category

    assert _infer_category("uniswap", "Uniswap") == "trading"
    assert _infer_category("aeon-defi-monitor", "Aeon") == "defi"
    assert _infer_category("zzz-unknown", "Nobody") == "other"


@pytest.mark.asyncio
async def test_external_install_type_is_excluded(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    results = await BankrSource().search("extern")

    assert results == []


@pytest.mark.asyncio
async def test_search_filters_by_query(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    results = await BankrSource().search("alche")

    assert [r.name for r in results] == ["alchemy"]


@pytest.mark.asyncio
async def test_catalog_is_cached_across_searches(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    src = BankrSource()
    await src.search("")
    first_tree = _AsyncClient.tree_calls
    first_catalog = _AsyncClient.catalog_calls
    assert first_tree == 1

    await src.search("bankr")

    # Second search hits the cache — no additional network calls.
    assert _AsyncClient.tree_calls == first_tree
    assert _AsyncClient.catalog_calls == first_catalog


class _FailingTreeClient(_AsyncClient):
    async def get(self, url: str, **kwargs: Any) -> _Response:
        if "/git/trees/" in url:
            raise RuntimeError("boom")
        return await super().get(url, **kwargs)


@pytest.mark.asyncio
async def test_tree_failure_returns_empty_without_raising(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FailingTreeClient)

    results = await BankrSource().search("")

    assert results == []


@pytest.mark.asyncio
async def test_fetch_and_inspect_delegate_to_github(monkeypatch) -> None:
    calls: dict[str, str] = {}

    async def _fake_fetch(self: Any, identifier: str) -> str:
        calls["fetch"] = identifier
        return "bundle"

    async def _fake_inspect(self: Any, identifier: str) -> str:
        calls["inspect"] = identifier
        return "meta"

    from agentos.skills.hub.github import GitHubSource

    monkeypatch.setattr(GitHubSource, "fetch", _fake_fetch)
    monkeypatch.setattr(GitHubSource, "inspect", _fake_inspect)

    src = BankrSource()
    ident = "https://github.com/BankrBot/skills/tree/main/alchemy"
    assert await src.fetch(ident) == "bundle"
    assert await src.inspect(ident) == "meta"
    assert calls == {"fetch": ident, "inspect": ident}


def test_default_router_exposes_bankr_source(monkeypatch) -> None:
    from agentos.skills.hub import defaults

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    defaults._default_router = None
    try:
        router = defaults.get_default_skill_router()
        assert "bankr" in router.source_ids
    finally:
        defaults._default_router = None
