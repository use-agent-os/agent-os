"""A skill body names its own files; only the runtime knows where they are.

``{baseDir}`` is how every bundled SKILL.md refers to its own scripts. Handed to
the model unexpanded it reads as a directory that does not exist, and the model
concludes the skill is not installed rather than that it cannot see the path.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agentos.skills.loader import SkillLoader
from agentos.skills.resources import skill_python
from agentos.tools.builtin import skill_tools as skill_tools_module
from agentos.tools.registry import get_default_registry

BUNDLED_BODY = """Run it with:

```bash
{python} {baseDir}/scripts/run.py --check
```

## Usage

Point it at a deck: `{python} {baseDir}/scripts/run.py deck.pptx`.
"""

WORKSPACE_BODY = "Notes helper. Scripts live under {baseDir}/scripts.\n"


async def _skill_view(
    name: str,
    file_path: str | None = None,
    section: str | None = None,
) -> str:
    registered = get_default_registry().get("skill_view")
    assert registered is not None
    return await registered.handler(name=name, file_path=file_path, section=section)


async def _skill_edit(name: str, description: str) -> str:
    registered = get_default_registry().get("skill_edit")
    assert registered is not None
    return await registered.handler(name=name, description=description)


@pytest.fixture()
def skill_loader(tmp_path: Path) -> Iterator[SkillLoader]:
    bundled_root = tmp_path / "bundled"
    deck_dir = bundled_root / "deck"
    (deck_dir / "scripts").mkdir(parents=True)
    (deck_dir / "SKILL.md").write_text(
        "---\nname: deck\ndescription: Deck helper\n---\n" + BUNDLED_BODY,
        encoding="utf-8",
    )
    (deck_dir / "scripts" / "run.py").write_text(
        "# lives at {baseDir}/scripts/run.py\nprint('script body')\n",
        encoding="utf-8",
    )

    workspace_root = tmp_path / "workspace"
    notes_dir = workspace_root / "notes"
    notes_dir.mkdir(parents=True)
    (notes_dir / "SKILL.md").write_text(
        "---\nname: notes\ndescription: Notes helper\n---\n" + WORKSPACE_BODY,
        encoding="utf-8",
    )

    loader = SkillLoader(
        bundled_dir=bundled_root,
        workspace_dir=workspace_root,
        managed_dir=tmp_path / "managed",
        personal_agents_dir=tmp_path / "personal",
        project_agents_dir=tmp_path / "project",
        snapshot_path=tmp_path / "skills.snapshot.json",
    )
    previous_loader = skill_tools_module._loader
    skill_tools_module.create_skill_tools(loader)
    try:
        yield loader
    finally:
        skill_tools_module._loader = previous_loader


def _base_dir(loader: SkillLoader, name: str) -> str:
    skill = loader.get_by_name(name)
    assert skill is not None
    return skill.base_dir


@pytest.mark.asyncio
async def test_skill_view_expands_base_dir_in_body(skill_loader: SkillLoader) -> None:
    base_dir = _base_dir(skill_loader, "deck")

    result = await _skill_view("deck")

    assert "{baseDir}" not in result
    assert "{python}" not in result
    assert f"{skill_python()} {base_dir}/scripts/run.py --check" in result


@pytest.mark.asyncio
async def test_skill_view_states_the_skill_directory(skill_loader: SkillLoader) -> None:
    base_dir = _base_dir(skill_loader, "deck")

    result = await _skill_view("deck")

    assert result.startswith(
        f"[Skill directory: {base_dir}]\n[Skill interpreter: {skill_python()} — "
    )


@pytest.mark.asyncio
async def test_skill_view_section_expands_and_states_directory(
    skill_loader: SkillLoader,
) -> None:
    base_dir = _base_dir(skill_loader, "deck")

    result = await _skill_view("deck", section="Usage")

    assert result.startswith(
        f"[Skill directory: {base_dir}]\n[Skill interpreter: {skill_python()} — "
    )
    assert f"{skill_python()} {base_dir}/scripts/run.py deck.pptx" in result
    assert "{baseDir}" not in result


@pytest.mark.asyncio
async def test_skill_view_expands_explicit_skill_md_request(skill_loader: SkillLoader) -> None:
    base_dir = _base_dir(skill_loader, "deck")

    result = await _skill_view("deck", file_path="SKILL.md")

    assert f"{skill_python()} {base_dir}/scripts/run.py --check" in result
    assert "{baseDir}" not in result


@pytest.mark.asyncio
async def test_skill_view_leaves_script_files_verbatim(skill_loader: SkillLoader) -> None:
    result = await _skill_view("deck", file_path="scripts/run.py")

    # Source, not a playbook. What a script says about a placeholder is the
    # script's own business, and rewriting it would corrupt the file the model
    # is reading.
    assert "# lives at {baseDir}/scripts/run.py" in result


@pytest.mark.asyncio
async def test_skill_edit_does_not_bake_expanded_path_into_the_file(
    skill_loader: SkillLoader,
) -> None:
    skill_file = skill_loader.workspace_dir / "notes" / "SKILL.md"

    await _skill_edit("notes", description="Notes helper, revised")

    written = skill_file.read_text(encoding="utf-8")
    assert "{baseDir}/scripts" in written
    assert str(skill_loader.workspace_dir) not in written


@pytest.mark.asyncio
async def test_skill_edit_preserves_existing_frontmatter_metadata(
    skill_loader: SkillLoader,
) -> None:
    skill_dir = skill_loader.workspace_dir / "custom-helper"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        """---
name: custom-helper
description: Initial description
metadata:
  agentos:
    emoji: 💡
    category: productivity
    requires:
      env:
        - name: HELPER_API_KEY
          description: API key for helper
    install:
      - kind: uv
        package: httpx
custom_setting: custom_value
triggers:
  - run helper
---

# Custom Helper
Body content here.
""",
        encoding="utf-8",
    )
    skill_loader.invalidate_cache()

    registered = get_default_registry().get("skill_edit")
    assert registered is not None
    await registered.handler(name="custom-helper", description="Updated description")

    updated_skill = skill_loader.get_by_name("custom-helper")
    assert updated_skill is not None
    assert updated_skill.description == "Updated description"
    assert updated_skill.triggers == ["run helper"]
    assert updated_skill.metadata is not None
    assert updated_skill.metadata.emoji == "💡"
    assert updated_skill.metadata.category == "productivity"
    assert updated_skill.metadata.requires is not None
    assert len(updated_skill.metadata.requires.env) == 1
    assert updated_skill.metadata.requires.env[0].name == "HELPER_API_KEY"
    assert len(updated_skill.metadata.install) == 1
    assert updated_skill.metadata.install[0].kind == "uv"

    raw_text = skill_file.read_text(encoding="utf-8")
    assert "custom_setting: custom_value" in raw_text
    assert "# Custom Helper\nBody content here." in raw_text


@pytest.mark.asyncio
async def test_skill_edit_partial_updates_content_only(
    skill_loader: SkillLoader,
) -> None:
    skill_dir = skill_loader.workspace_dir / "partial-helper"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        """---
name: partial-helper
description: Retained description
metadata:
  agentos:
    emoji: ⚡
triggers:
  - trigger 1
---

Original body
""",
        encoding="utf-8",
    )
    skill_loader.invalidate_cache()

    registered = get_default_registry().get("skill_edit")
    assert registered is not None
    await registered.handler(name="partial-helper", content="New updated body")

    updated_skill = skill_loader.get_by_name("partial-helper")
    assert updated_skill is not None
    assert updated_skill.description == "Retained description"
    assert updated_skill.triggers == ["trigger 1"]
    assert updated_skill.metadata is not None
    assert updated_skill.metadata.emoji == "⚡"

    raw_text = skill_file.read_text(encoding="utf-8")
    assert "description: Retained description" in raw_text
    assert "New updated body" in raw_text


@pytest.mark.asyncio
async def test_skill_edit_clearing_triggers_and_special_characters(
    skill_loader: SkillLoader,
) -> None:
    skill_dir = skill_loader.workspace_dir / "special-helper"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        """---
name: special-helper
description: Initial description
triggers:
  - old trigger
---

# Helper
""",
        encoding="utf-8",
    )
    skill_loader.invalidate_cache()

    registered = get_default_registry().get("skill_edit")
    assert registered is not None
    # Provide special characters in description and empty triggers list to clear
    await registered.handler(
        name="special-helper",
        description="Helper: forecast & alerts (v2)",
        triggers=[],
    )

    updated_skill = skill_loader.get_by_name("special-helper")
    assert updated_skill is not None
    assert updated_skill.description == "Helper: forecast & alerts (v2)"
    assert updated_skill.triggers == []

    raw_text = skill_file.read_text(encoding="utf-8")
    assert "triggers:" not in raw_text
    assert "Helper: forecast & alerts (v2)" in raw_text
    assert "# Helper" in raw_text


def test_update_skill_md_fallback_when_raw_lacks_frontmatter() -> None:
    raw = "# Just Markdown\nNo frontmatter here."
    rendered = skill_tools_module._update_skill_md(
        raw,
        name="raw-notes",
        description="Added description",
        triggers=["run notes"],
    )
    assert "name: raw-notes" in rendered
    assert "description: Added description" in rendered
    assert "triggers:\n  - run notes" in rendered
    assert "# Just Markdown\nNo frontmatter here." in rendered
