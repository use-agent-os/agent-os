from __future__ import annotations

from pathlib import Path

from agentos.skills.loader import SkillLoader

ROOT = Path(__file__).resolve().parents[1]
BUNDLED_SKILLS = ROOT / "src" / "agentos" / "skills" / "bundled"
MEMORY_SKILL = BUNDLED_SKILLS / "memory" / "SKILL.md"


def test_memory_skill_is_parseable_and_gated_on_read_tools(tmp_path: Path) -> None:
    loader = SkillLoader(
        bundled_dir=BUNDLED_SKILLS,
        snapshot_path=tmp_path / "skills_snapshot.json",
    )

    skill = next(s for s in loader.load_all() if s.name == "memory")

    assert skill.description.startswith("Use when")
    assert skill.requires_tools == ["memory_search", "memory_get"]
    assert skill.disable_model_invocation is False
    assert skill.provenance.origin == "agentos-original"
    assert skill.provenance.maintained_by == "AgentOS"


def test_memory_skill_documents_usable_write_and_forget_paths() -> None:
    text = MEMORY_SKILL.read_text(encoding="utf-8")
    lower = text.lower()

    assert "origin: agentos-original" in text
    assert "Use only tools that are visible in the current tool list" in text
    assert "memory_save" in text
    assert "if `memory_save` is available" in lower
    assert "`MEMORY.md`" in text and "mode='replace'" in text
    assert "memory/**/*.md" in text
    assert "memory_delete" in text
    assert "If no write or delete tool is available" in text
    assert "Only confirm memory was updated after the write or delete succeeds" in text
