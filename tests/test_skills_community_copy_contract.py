from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS_SECTION = (
    ROOT / "frontend" / "src" / "web" / "features" / "settings" / "sections" / "skills.tsx"
)
UI_CATALOG = ROOT / "frontend" / "src" / "i18n" / "en" / "webSetCapabilities.ts"
CLI = ROOT / "src" / "agentos" / "cli" / "skills_cmd.py"
HUB = [
    ROOT / "src" / "agentos" / "skills" / "hub" / "__init__.py",
    ROOT / "src" / "agentos" / "skills" / "hub" / "clawhub.py",
    ROOT / "src" / "agentos" / "skills" / "hub" / "source.py",
]
SURFACES = [CLI, SKILLS_SECTION, UI_CATALOG, *HUB]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_no_surface_calls_the_community_source_a_marketplace() -> None:
    for path in SURFACES:
        lower = _read(path).lower()
        assert "marketplace" not in lower, path.name


def test_the_web_console_names_clawhub_a_community_source() -> None:
    # Checked on the UI copy alone: the hub modules say "ClawHub community
    # source" too, so a check over every surface at once could pass with the
    # console saying something else entirely.
    catalog = _read(UI_CATALOG)
    assert "skillsTabCommunity: 'Community'" in catalog
    assert "the ClawHub community source" in catalog
    section = _read(SKILLS_SECTION)
    # The copy above is what the Community tab actually shows.
    assert "t('webSetCapabilities.skillsTabCommunity')" in section
    assert "t('webSetCapabilities.skillsCommunityBody')" in section


def test_the_cli_offers_community_sources() -> None:
    lower = _read(CLI).lower()
    assert "community source" in lower
