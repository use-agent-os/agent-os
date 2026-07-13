from __future__ import annotations

from pathlib import Path


def test_skills_view_exposes_direct_github_install_control() -> None:
    view = Path("src/agentos/gateway/static/js/views/skills.js").read_text(encoding="utf-8")

    assert 'id="skills-github-url"' in view
    assert 'class="btn btn--primary" id="skills-github-install"' in view
    assert "_installSkill(githubInput.value.trim(), 'github'," in view


def test_skills_view_search_stays_clawhub_only() -> None:
    view = Path("src/agentos/gateway/static/js/views/skills.js").read_text(encoding="utf-8")

    assert 'id="skills-registry-source"' not in view
    assert "Searching ClawHub" in view
    assert "skills.search', { query: query.trim(), limit: 20 }" in view


def test_skills_view_distinguishes_bundled_from_local_layers() -> None:
    view = Path("src/agentos/gateway/static/js/views/skills.js").read_text(encoding="utf-8")

    assert "Bundled skills ship with AgentOS." in view
    assert "Managed skills are locally installed into AgentOS state." in view
    assert "Personal skills are local user installs, not bundled." in view

