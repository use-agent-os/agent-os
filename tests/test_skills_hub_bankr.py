from __future__ import annotations

import json
from typing import Any

import pytest

from agentos.skills.hub.bankr import _ALLOWED_SLUGS, BankrSource

# The fake catalog exercises the filtering paths (installable / external /
# malformed). We hand BankrSource this slug set via ``allowlist=`` so the source
# fetches exactly these directly — no repo tree crawl.
_FIXTURE_SLUGS = ("alchemy", "bankr", "extern", "broken")


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
    """Mocks the per-skill BankrBot/skills catalog.json + SKILL.md fetches.

    The source no longer crawls the git tree, so hitting the trees API here is a
    regression — it raises instead.
    """

    catalogs = {
        "alchemy": _catalog("alchemy", logo="alchemy.svg"),
        "bankr": _catalog("bankr", logo=None),
        "extern": _catalog("extern", install_type="external"),
        "broken": b"{ not json",
    }
    skill_mds = {
        "alchemy": b"---\nname: alchemy\ndescription: On-chain data APIs\n---\n# Alchemy\n",
        "bankr": b"---\nname: bankr\ndescription: AI-powered crypto trading agent\n---\n# Bankr\n",
    }
    catalog_calls = 0
    skill_md_calls = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _AsyncClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _Response:
        if "/git/trees/" in url:
            raise AssertionError(f"tree API must not be called: {url}")
        marker = "raw.githubusercontent.com/BankrBot/skills/main/"
        if marker in url:
            slug = url.split(marker, 1)[1].split("/", 1)[0]
            if url.endswith("/SKILL.md"):
                type(self).skill_md_calls += 1
                content = self.skill_mds.get(slug)
                if content is None:
                    return _Response(status_code=404)
                return _Response(content=content)
            type(self).catalog_calls += 1
            return _Response(content=self.catalogs.get(slug, b"{}"))
        raise AssertionError(f"unexpected URL: {url}")


@pytest.fixture(autouse=True)
def _reset_client_counters() -> None:
    _AsyncClient.catalog_calls = 0
    _AsyncClient.skill_md_calls = 0


def _source() -> BankrSource:
    return BankrSource(allowlist=_FIXTURE_SLUGS)


@pytest.mark.asyncio
async def test_search_empty_query_lists_all_bankr_skills(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    results = await _source().search("")

    names = {r.name for r in results}
    # bankr + alchemy kept; external skipped; broken JSON skipped.
    assert names == {"alchemy", "bankr"}
    assert all(r.source_id == "bankr" for r in results)
    assert all(r.trust_level == "community" for r in results)


@pytest.mark.asyncio
async def test_search_builds_provider_logo_and_identifier(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    results = await _source().search("")
    by_name = {r.name: r for r in results}

    alchemy = by_name["alchemy"]
    assert alchemy.provider == "Alchemy"
    assert alchemy.logo == (
        "https://raw.githubusercontent.com/BankrBot/skills/main/alchemy/alchemy.svg"
    )
    assert alchemy.identifier == "https://github.com/BankrBot/skills/tree/main/alchemy"

    # Null logo in the catalog → empty logo, but the Bankr brand emoji fills in
    # as the avatar so cards never render a bare initials box.
    assert by_name["bankr"].logo == ""
    assert by_name["bankr"].emoji == "📺"
    assert by_name["alchemy"].emoji == "📺"


@pytest.mark.asyncio
async def test_search_carries_catalog_setup_demo_and_category(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    results = await _source().search("")
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
async def test_search_fills_description_from_skill_md_frontmatter(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    results = await _source().search("")
    by_name = {r.name: r for r in results}

    assert by_name["bankr"].description == "AI-powered crypto trading agent"
    assert by_name["alchemy"].description == "On-chain data APIs"
    # SKILL.md is fetched only for installable skills — external installs and
    # broken catalogs never trigger a description fetch.
    assert _AsyncClient.skill_md_calls == 2


class _MissingSkillMdClient(_AsyncClient):
    skill_mds: dict[str, bytes] = {}


@pytest.mark.asyncio
async def test_missing_skill_md_keeps_skill_with_empty_description(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _MissingSkillMdClient)

    results = await _source().search("")
    by_name = {r.name: r for r in results}

    # A failed SKILL.md fetch must not drop the skill from the listing.
    assert set(by_name) == {"alchemy", "bankr"}
    assert by_name["bankr"].description == ""


@pytest.mark.asyncio
async def test_external_install_type_is_excluded(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    # "extern" is allowlisted but its catalog declares install.type == external,
    # so it is dropped and never matches a query.
    results = await _source().search("extern")

    assert results == []


@pytest.mark.asyncio
async def test_search_filters_by_query(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    results = await _source().search("alche")

    assert [r.name for r in results] == ["alchemy"]


@pytest.mark.asyncio
async def test_catalog_is_cached_across_searches(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    src = _source()
    await src.search("")
    first_catalog = _AsyncClient.catalog_calls
    # One catalog.json fetch per allowlisted slug — no tree crawl.
    assert first_catalog == len(_FIXTURE_SLUGS)

    await src.search("bankr")

    # Second search hits the cache — no additional network calls.
    assert _AsyncClient.catalog_calls == first_catalog


class _FailingCatalogClient(_AsyncClient):
    async def get(self, url: str, **kwargs: Any) -> _Response:
        if url.endswith("/catalog.json"):
            raise RuntimeError("boom")
        return await super().get(url, **kwargs)


@pytest.mark.asyncio
async def test_all_entries_failing_returns_empty_without_raising(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FailingCatalogClient)

    results = await _source().search("")

    assert results == []


class _DefaultAllowlistClient(_AsyncClient):
    """Serves only the two real default slugs."""

    catalogs = {
        "bankr": _catalog("bankr", logo=None),
        "bankr-token-scam-analysis": _catalog("bankr-token-scam-analysis", logo=None),
    }
    skill_mds = {
        "bankr": b"---\nname: bankr\ndescription: Trading agent\n---\n# Bankr\n",
        "bankr-token-scam-analysis": (
            b"---\nname: scam\ndescription: Scans tokens for scams\n---\n# Scan\n"
        ),
    }


@pytest.mark.asyncio
async def test_default_allowlist_loads_only_two_skills(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _DefaultAllowlistClient)

    # Default (no allowlist arg) → exactly the two Bankr slugs, nothing else.
    assert _ALLOWED_SLUGS == ("bankr", "bankr-token-scam-analysis")

    results = await BankrSource().search("")

    assert {r.name for r in results} == {"bankr", "bankr-token-scam-analysis"}
    # Two skills → two catalog.json + two SKILL.md fetches, and no tree crawl.
    assert _DefaultAllowlistClient.catalog_calls == 2
    assert _DefaultAllowlistClient.skill_md_calls == 2


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
    ident = "https://github.com/BankrBot/skills/tree/main/bankr"
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
