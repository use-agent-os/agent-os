"""A SKILL.md saved with a byte-order mark still loads.

PowerShell 5.1, the shell Windows ships with, writes a UTF-8 BOM for
``Set-Content -Encoding UTF8`` and UTF-16 for ``>`` / ``Out-File``. The loader
read every SKILL.md as plain ``utf-8``: the UTF-8 BOM hid the ``---`` from the
frontmatter match and the skill vanished without a log line, and UTF-16 did not
decode at all. Either way ``skill_view`` answered that the skill was not
installed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agentos.skills.loader import SkillLoader
from agentos.tools.builtin import skill_tools as skill_tools_module
from agentos.tools.registry import get_default_registry

_SKILL_MD = "---\nname: {name}\ndescription: Notes helper — café\n---\nUse the notes, naïvely.\n"


def _skill_bytes(name: str, encoding: str, newline: str = "\n") -> bytes:
    return _SKILL_MD.format(name=name).replace("\n", newline).encode(encoding)


VARIANTS = {
    "plain-utf8": _skill_bytes("plain-utf8", "utf-8"),
    "utf8-bom": _skill_bytes("utf8-bom", "utf-8-sig"),
    "utf8-bom-crlf": _skill_bytes("utf8-bom-crlf", "utf-8-sig", "\r\n"),
    # PowerShell 5.1 ``>`` writes UTF-16 LE with CRLF line endings.
    "utf16-le": b"\xff\xfe" + _skill_bytes("utf16-le", "utf-16-le", "\r\n"),
    "utf16-be": b"\xfe\xff" + _skill_bytes("utf16-be", "utf-16-be"),
}


@pytest.fixture()
def loader(tmp_path: Path) -> Iterator[SkillLoader]:
    workspace = tmp_path / "workspace"
    for name, data in VARIANTS.items():
        skill_dir = workspace / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_bytes(data)
    skill_loader = SkillLoader(
        bundled_dir=tmp_path / "bundled",
        workspace_dir=workspace,
        managed_dir=tmp_path / "managed",
        personal_agents_dir=tmp_path / "personal",
        project_agents_dir=tmp_path / "project",
        snapshot_path=tmp_path / "skills.snapshot.json",
    )
    previous = skill_tools_module._loader
    skill_tools_module.create_skill_tools(skill_loader)
    try:
        yield skill_loader
    finally:
        skill_tools_module._loader = previous


async def _skill_view(name: str) -> str:
    registered = get_default_registry().get("skill_view")
    assert registered is not None
    return await registered.handler(name=name)


@pytest.mark.parametrize(
    "name", ["plain-utf8", "utf8-bom", "utf8-bom-crlf", "utf16-le", "utf16-be"]
)
def test_skill_loads_whatever_mark_it_starts_with(loader: SkillLoader, name: str) -> None:
    skill = loader.get_by_name(name)

    assert skill is not None
    assert skill.description == "Notes helper — café"
    assert skill.content == "Use the notes, naïvely."


@pytest.mark.parametrize("name", ["utf8-bom", "utf16-le"])
async def test_skill_view_reads_a_skill_saved_with_a_mark(loader: SkillLoader, name: str) -> None:
    result = await _skill_view(name)

    assert "not installed" not in result
    assert "Use the notes, naïvely." in result
    assert "﻿" not in result


def test_every_variant_is_listed(loader: SkillLoader) -> None:
    assert sorted(skill.name for skill in loader.load_all()) == sorted(VARIANTS)
