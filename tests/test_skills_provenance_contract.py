from __future__ import annotations

from pathlib import Path

from agentos.skills.loader import SkillLoader

ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = ROOT / "src" / "agentos" / "skills"
BUNDLED = SKILLS_DIR / "bundled"
EXP = SKILLS_DIR / "exp"
DEFAULTS = {"pptx", "memory", "cron", "github"}


def test_default_bundled_skills_have_release_provenance(tmp_path: Path) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")
    skills = {skill.name: skill for skill in loader.load_all()}

    for name in DEFAULTS:
        provenance = skills[name].provenance
        assert provenance.origin in {
            "agentos-original",
            "bundled-derived",
            "openclaw-derived",
            "clawhub-mit0",
        }
        assert provenance.maintained_by == "AgentOS"
        if provenance.origin == "bundled-derived":
            assert provenance.upstream_url.startswith("https://")
            assert provenance.license == "MIT"
        elif provenance.origin == "openclaw-derived":
            assert provenance.upstream_url == "https://github.com/openclaw/openclaw"
            assert provenance.license == "MIT"
        elif provenance.origin == "clawhub-mit0":
            assert provenance.upstream_url.startswith("https://clawhub.ai/")
            assert provenance.license == "MIT-0"
        else:
            assert provenance.license == "MIT"


def test_provenance_survives_snapshot_roundtrip(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.json"
    loader = SkillLoader(bundled_dir=BUNDLED, extra_dirs=[EXP], snapshot_path=snapshot)
    first = {skill.name: skill.provenance for skill in loader.load_all()}
    loader.save_snapshot()

    reloaded = SkillLoader(bundled_dir=BUNDLED, extra_dirs=[EXP], snapshot_path=snapshot)
    second = {skill.name: skill.provenance for skill in reloaded.load_all()}

    for name in DEFAULTS:
        assert second[name] == first[name]


def test_capability_risk_metadata_survives_snapshot_roundtrip(tmp_path: Path) -> None:
    """Auto-enable decisions must see the same risk manifest after cold-start."""

    skill_root = tmp_path / "skills"
    skill_dir = skill_root / "writes-files"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: writes-files
description: Synthetic skill with manifest risk metadata.
metadata:
  agentos:
    risk: medium
    capabilities: [filesystem-write]
---

# body
""",
        encoding="utf-8",
    )

    snapshot = tmp_path / "snapshot.json"
    loader = SkillLoader(bundled_dir=skill_root, snapshot_path=snapshot)
    fresh = loader.get_by_name("writes-files")
    assert fresh is not None
    loader.save_snapshot()

    reloaded = SkillLoader(bundled_dir=skill_root, snapshot_path=snapshot)
    from_snapshot = reloaded.get_by_name("writes-files")

    assert from_snapshot is not None
    assert from_snapshot.metadata is not None
    assert from_snapshot.metadata.risk_level == "medium"
    assert from_snapshot.metadata.capabilities == ["filesystem-write"]
