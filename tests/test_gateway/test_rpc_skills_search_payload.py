from __future__ import annotations

import pytest

from agentos.gateway import rpc_skills
from agentos.skills.hub.source import SkillMeta


class _StubRouter:
    def __init__(self, results: list[SkillMeta]) -> None:
        self._results = results
        self.calls: list[dict] = []

    async def search(self, query: str, limit: int = 20, source_id: str | None = None):
        self.calls.append({"query": query, "limit": limit, "source_id": source_id})
        return self._results[:limit]


class _Ctx:
    def __init__(self, router: _StubRouter) -> None:
        self._skill_router = router


@pytest.mark.asyncio
async def test_skills_search_payload_carries_catalog_fields(monkeypatch) -> None:
    """The browse UI reads provider/logo/category/setup/demo/homepage off every
    result row — dropping any of them silently breaks the registry cards and
    the detail dialog."""
    monkeypatch.setattr(rpc_skills, "_installed_names", lambda: set())
    monkeypatch.setattr(rpc_skills, "installed_skill_identifiers", lambda: set())
    meta = SkillMeta(
        name="alchemy",
        source_id="bankr",
        identifier="https://github.com/BankrBot/skills/tree/main/alchemy",
        homepage="https://alchemy.com",
        provider="Alchemy",
        logo="https://raw.githubusercontent.com/BankrBot/skills/main/alchemy/alchemy.svg",
        category="data",
        setup=["Install SDK"],
        demo={"title": "demo.sh", "language": "bash", "code": "alchemy run"},
    )
    router = _StubRouter([meta])

    res = await rpc_skills._handle_skills_search(
        {"query": "", "source": "bankr", "limit": 200}, _Ctx(router)
    )

    row = res["results"][0]
    assert row["provider"] == "Alchemy"
    assert row["logo"].endswith("alchemy.svg")
    assert row["category"] == "data"
    assert row["setup"] == ["Install SDK"]
    assert row["demo"]["code"] == "alchemy run"
    assert row["homepage"] == "https://alchemy.com"
    assert row["installed"] is False


@pytest.mark.asyncio
async def test_skills_search_marks_installed_by_identifier(monkeypatch) -> None:
    """A skill whose lockfile name differs from its catalog slug must still show
    as installed. Bankr's ``bankr-token-scam-analysis`` slug installs under the
    name ``token-scam-analysis``, so name-only matching misses it — the source
    identifier is the reliable join key across a page reload."""
    # Lockfile records only the installed *name*, which does not match the
    # browse card's name; the identifier is what lines up.
    monkeypatch.setattr(rpc_skills, "_installed_names", lambda: {"token-scam-analysis"})
    monkeypatch.setattr(
        rpc_skills,
        "installed_skill_identifiers",
        lambda: {"https://github.com/BankrBot/skills/tree/main/bankr-token-scam-analysis"},
    )
    meta = SkillMeta(
        name="bankr-token-scam-analysis",
        source_id="bankr",
        identifier="https://github.com/BankrBot/skills/tree/main/bankr-token-scam-analysis",
    )
    router = _StubRouter([meta])

    res = await rpc_skills._handle_skills_search({"query": "", "source": "bankr"}, _Ctx(router))

    assert res["results"][0]["installed"] is True


@pytest.mark.asyncio
async def test_skills_search_limit_accommodates_full_catalog_browse(monkeypatch) -> None:
    """Browse requests whole catalogs (Bankr is ~100 skills); a cap sized for
    paged search results would silently truncate them."""
    monkeypatch.setattr(rpc_skills, "_installed_names", lambda: set())
    monkeypatch.setattr(rpc_skills, "installed_skill_identifiers", lambda: set())
    metas = [SkillMeta(name=f"skill-{i:03d}", source_id="bankr") for i in range(150)]
    router = _StubRouter(metas)

    res = await rpc_skills._handle_skills_search({"query": "", "limit": 200}, _Ctx(router))

    assert router.calls[0]["limit"] == 200
    assert len(res["results"]) == 150
