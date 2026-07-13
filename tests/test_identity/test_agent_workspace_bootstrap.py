from __future__ import annotations

from agentos.identity.bootstrap import ensure_agent_workspace


def test_fresh_workspace_seeds_agents_template(tmp_path) -> None:
    result = ensure_agent_workspace(tmp_path)

    assert (tmp_path / "AGENTS.md").is_file()
    assert (tmp_path / "SOUL.md").is_file()
    assert (tmp_path / "USER.md").is_file()
    assert (tmp_path / "MEMORY.md").is_file()
    assert (tmp_path / "BOOTSTRAP.md").is_file()
    assert (tmp_path / "memory").is_dir()
    assert "AGENTS.md" in result.created_files
    assert "MEMORY.md" in result.created_files


def test_existing_workspace_backfills_missing_agents_template(tmp_path) -> None:
    (tmp_path / "SOUL.md").write_text("custom soul\n", encoding="utf-8")
    (tmp_path / "USER.md").write_text("custom user\n", encoding="utf-8")

    result = ensure_agent_workspace(tmp_path)

    assert (tmp_path / "AGENTS.md").is_file()
    assert "AGENTS.md" in result.created_files
    assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == "custom soul\n"
    assert (tmp_path / "USER.md").read_text(encoding="utf-8") == "custom user\n"


def test_seed_templates_false_does_not_create_agents_template(tmp_path) -> None:
    result = ensure_agent_workspace(tmp_path, seed_templates=False)

    assert not (tmp_path / "AGENTS.md").exists()
    assert result.created_files == ()
