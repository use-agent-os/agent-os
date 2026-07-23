from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SURFACES = [
    ROOT / "src" / "agentos" / "cli" / "skills_cmd.py",
    ROOT / "frontend" / "src" / "views" / "skills" / "SkillsPage.tsx",
    ROOT / "src" / "agentos" / "skills" / "hub" / "__init__.py",
    ROOT / "src" / "agentos" / "skills" / "hub" / "clawhub.py",
    ROOT / "src" / "agentos" / "skills" / "hub" / "source.py",
]


def test_clawhub_copy_uses_community_source_language() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in SURFACES)
    lower = combined.lower()

    assert "official marketplace" not in lower
    assert "agentos marketplace" not in lower
    assert "marketplace" not in lower
    assert "community" in lower
    assert "clawhub community source" in lower
