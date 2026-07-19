from __future__ import annotations

from pathlib import Path

import pytest

from agentos.skills.hub.installer import SkillInstaller
from agentos.skills.hub.source import SkillBundle, SkillMeta


def _skill_md(body: str) -> str:
    return f"---\nname: demo\ndescription: Use when testing.\n---\n\n# Demo\n{body}\n"


class MutableRouter:
    """Router whose fetched bundle content can change between fetches, to
    simulate a source (git branch) that has moved on."""

    def __init__(self, body: str) -> None:
        self._body = body

    def set_body(self, body: str) -> None:
        self._body = body

    async def fetch(self, identifier: str, source_id: str) -> SkillBundle | None:
        return SkillBundle(name="demo", files={"SKILL.md": _skill_md(self._body)}, meta=None)

    async def inspect(self, identifier: str, source_id: str) -> SkillMeta | None:
        return None


def _installer(router: MutableRouter, tmp_path: Path) -> SkillInstaller:
    return SkillInstaller(
        router=router,
        managed_dir=tmp_path / "managed",
        quarantine_dir=tmp_path / "quarantine",
        lockfile_path=tmp_path / "lock.json",
    )


@pytest.mark.asyncio
async def test_update_reports_already_up_to_date_when_unchanged(tmp_path: Path) -> None:
    router = MutableRouter("v1")
    installer = _installer(router, tmp_path)

    installed = await installer.install(
        "https://github.com/BankrBot/skills/tree/main/demo", "bankr"
    )
    assert installed.success is True

    # Re-fetch returns identical content → hash matches → no-op update.
    results = await installer.update("demo")

    assert len(results) == 1
    assert results[0].success is True
    assert "already up to date" in results[0].message


@pytest.mark.asyncio
async def test_update_pulls_new_code_and_reports_updated(tmp_path: Path) -> None:
    router = MutableRouter("v1")
    installer = _installer(router, tmp_path)

    await installer.install("https://github.com/BankrBot/skills/tree/main/demo", "bankr")

    # Source moves on: the branch tip now has new content.
    router.set_body("v2-new-code")
    results = await installer.update("demo")

    assert len(results) == 1
    assert results[0].success is True
    assert "Updated" in results[0].message
    # Managed dir is overwritten with the freshly-fetched code.
    installed_md = (tmp_path / "managed" / "demo" / "SKILL.md").read_text(encoding="utf-8")
    assert "v2-new-code" in installed_md


@pytest.mark.asyncio
async def test_update_unknown_skill_reports_not_in_lockfile(tmp_path: Path) -> None:
    installer = _installer(MutableRouter("v1"), tmp_path)

    results = await installer.update("does-not-exist")

    assert len(results) == 1
    assert results[0].success is False
    assert "Not in lockfile" in results[0].message
