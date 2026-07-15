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
async def test_skills_search_limit_accommodates_full_catalog_browse(monkeypatch) -> None:
    """Browse requests whole catalogs (Bankr is ~100 skills); a cap sized for
    paged search results would silently truncate them."""
    monkeypatch.setattr(rpc_skills, "_installed_names", lambda: set())
    metas = [SkillMeta(name=f"skill-{i:03d}", source_id="bankr") for i in range(150)]
    router = _StubRouter(metas)

    res = await rpc_skills._handle_skills_search({"query": "", "limit": 200}, _Ctx(router))

    assert router.calls[0]["limit"] == 200
    assert len(res["results"]) == 150
