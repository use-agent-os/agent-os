from __future__ import annotations

from pathlib import Path


def test_skills_view_exposes_direct_github_install_control() -> None:
    view = Path("src/agentos/gateway/static/js/views/skills.js").read_text(encoding="utf-8")

    assert 'id="skills-github-url"' in view
    assert 'class="btn btn--primary" id="skills-github-install"' in view
    assert "_installSkill(githubInput.value.trim(), 'github'," in view


def test_skills_view_browses_community_catalog_without_source_picker() -> None:
    view = Path("src/agentos/gateway/static/js/views/skills.js").read_text(encoding="utf-8")

    # No redundant source dropdown — sources are aggregated by the router.
    assert 'id="skills-registry-source"' not in view
    # Registry search aggregates across community sources (no ClawHub-only copy).
    assert "Searching ClawHub" not in view
    assert "community skills" in view
    # Opening a browse tab loads the full catalog (empty-query search).
    assert "_browse(tab)" in view
    # Typed community queries reach the server (the snapshot only covers the
    # first page of each source), and stale responses are dropped.
    assert "_searchCommunity" in view
    assert "_communitySeq" in view


def test_skills_view_has_dedicated_bankr_tab() -> None:
    view = Path("src/agentos/gateway/static/js/views/skills.js").read_text(encoding="utf-8")

    # A dedicated Bankr partner tab, distinct from generic Community.
    assert 'data-tab="bankr"' in view
    assert 'id="skills-tab-bankr"' in view
    assert "Bankr partner catalog" in view
    # Bankr browse pins source=bankr; community filters bankr out.
    assert "params.source = 'bankr'" in view
    assert "results.filter(r => r.source !== 'bankr')" in view


def test_skills_view_has_dedicated_robinhood_tab() -> None:
    view = Path("src/agentos/gateway/static/js/views/skills.js").read_text(encoding="utf-8")

    # A dedicated Robinhood partner tab with its brand logo.
    assert 'data-tab="robinhood"' in view
    assert 'id="skills-tab-robinhood"' in view
    assert "Robinhood partner catalog" in view
    assert "robinhood-symbol.png" in view


def test_skills_view_robinhood_tab_lists_installed_robinhood_skills() -> None:
    view = Path("src/agentos/gateway/static/js/views/skills.js").read_text(encoding="utf-8")

    # The tab renders installed Robinhood-family skills (name prefix or homepage),
    # falling back to a coming-soon empty state when none are installed.
    assert "_renderRobinhood" in view
    assert "_isRobinhoodSkill" in view
    assert "robinhood.com" in view
    assert "Robinhood skills are on the way" in view


def test_skills_view_renders_registry_cards_with_provider_and_logo() -> None:
    view = Path("src/agentos/gateway/static/js/views/skills.js").read_text(encoding="utf-8")

    # Community/Bankr browse uses a card gallery, not a table.
    assert "sk-grid--registry" in view
    assert "_renderRegistryCard" in view
    assert "sk-rcard__logo" in view
    # Falls back to initials when a skill has no logo asset; the fallback is a
    # hidden sibling revealed by a static onerror (no data in inline JS).
    assert "_logoBadge" in view
    assert "${cls}--initials" in view


def test_skills_view_registry_detail_shows_demo_and_setup() -> None:
    view = Path("src/agentos/gateway/static/js/views/skills.js").read_text(encoding="utf-8")

    # Detail dialog surfaces catalog demo + setup before install.
    assert "_openRegistryDialog" in view
    assert "sk-dialog__setup" in view
    assert "sk-dialog__code" in view


def test_skills_view_has_category_filter_chips() -> None:
    view = Path("src/agentos/gateway/static/js/views/skills.js").read_text(encoding="utf-8")

    assert "data-cat-chip" in view
    assert "_catFilter" in view


def test_skills_view_offers_force_install_on_security_block() -> None:
    view = Path("src/agentos/gateway/static/js/views/skills.js").read_text(encoding="utf-8")

    # A dangerous security verdict is surfaced, not shown as a generic failure,
    # and the button offers an explicit force-install override.
    assert "scan_verdict === 'dangerous'" in view
    assert "Force install" in view
    assert "skills.install', { identifier, source, force }" in view
    # The delegated click re-sends with force after the user confirms.
    assert "installBtn.dataset.force === '1'" in view
    # The armed state lives in view state, so grid re-renders keep it armed.
    assert "_forceArmed" in view


def test_skills_view_sanitizes_remote_urls() -> None:
    view = Path("src/agentos/gateway/static/js/views/skills.js").read_text(encoding="utf-8")

    # Catalog-supplied URLs (homepage, logo) must be scheme-checked before
    # landing in href/src — a javascript: providerUrl would otherwise run as
    # operator-context XSS from a third-party catalog.json.
    assert "_safeUrl" in view
    assert "/^https?:\\/\\//i" in view
    assert "_safeUrl(r.homepage)" in view
    assert "_safeUrl(r.logo)" in view
    assert "_safeUrl(skill.homepage)" in view


def test_skills_view_distinguishes_bundled_from_local_layers() -> None:
    view = Path("src/agentos/gateway/static/js/views/skills.js").read_text(encoding="utf-8")

    assert "Bundled skills ship with AgentOS." in view
    assert "Managed skills are locally installed into AgentOS state." in view
    assert "Personal skills are local user installs, not bundled." in view

