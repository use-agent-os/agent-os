from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agentos.cli.skills_cmd import skills_app


@pytest.fixture
def temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fixture to isolate default_agentos_home and Path.home()."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    agentos_home = tmp_path / "agentos_home"
    agentos_home.mkdir()

    monkeypatch.setattr(Path, "home", lambda: home_dir)
    monkeypatch.setattr("agentos.paths.default_agentos_home", lambda: agentos_home)
    return tmp_path


def test_init_invalid_skill_name() -> None:
    runner = CliRunner()

    # Invalid characters (spaces, stars)
    for invalid_name in ["my skill", "my*skill", "../traversal"]:
        result = runner.invoke(skills_app, ["init", invalid_name])
        assert result.exit_code == 1
        assert "Invalid skill name" in result.output


def test_init_success_default_scaffold(temp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Set CWD to a temp directory to control Path.cwd()
    cwd_dir = temp_home / "cwd"
    cwd_dir.mkdir()
    monkeypatch.setattr(Path, "cwd", lambda: cwd_dir)

    runner = CliRunner()

    # We will pass target-dir explicitly for simplicity
    target = cwd_dir / "target_skills"
    target.mkdir()

    result = runner.invoke(
        skills_app,
        [
            "init",
            "my-skill",
            "--target-dir",
            str(target),
            "--description",
            "My customized description",
            "-t",
            "trigger1",
            "-t",
            "trigger2",
        ],
    )

    assert result.exit_code == 0
    assert "Initialized custom skill template" in result.output

    skill_dir = target / "my-skill"
    assert skill_dir.is_dir()

    skill_md = skill_dir / "SKILL.md"
    assert skill_md.is_file()

    # Scripts/run.py should not be created by default
    assert not (skill_dir / "scripts").exists()

    # Verify SKILL.md contents
    content = skill_md.read_text(encoding="utf-8")

    # Body must be >= 20 chars
    parts = content.split("---")
    assert len(parts) >= 3
    body = parts[2].strip()
    assert len(body) >= 20

    # Frontmatter validation
    frontmatter = yaml.safe_load(parts[1])
    assert frontmatter["name"] == "my-skill"
    assert frontmatter["description"] == "My customized description"
    assert frontmatter["always"] is False
    assert frontmatter["triggers"] == ["trigger1", "trigger2"]
    assert frontmatter["provenance"]["origin"] == "local"
    assert frontmatter["metadata"]["agentos"]["emoji"] == "💡"


def test_init_success_with_script(temp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cwd_dir = temp_home / "cwd"
    cwd_dir.mkdir()
    monkeypatch.setattr(Path, "cwd", lambda: cwd_dir)

    runner = CliRunner()
    target = cwd_dir / "target_skills"
    target.mkdir()

    result = runner.invoke(
        skills_app,
        [
            "init",
            "my-script-skill",
            "--target-dir",
            str(target),
            "--with-script",
        ],
    )

    assert result.exit_code == 0

    skill_dir = target / "my-script-skill"
    skill_md = skill_dir / "SKILL.md"
    run_py = skill_dir / "scripts" / "run.py"

    assert skill_md.is_file()
    assert run_py.is_file()

    # Verify entrypoint is in the frontmatter
    parts = skill_md.read_text(encoding="utf-8").split("---")
    frontmatter = yaml.safe_load(parts[1])
    assert "entrypoint" in frontmatter
    assert frontmatter["entrypoint"]["command"] == "{python} {baseDir}/scripts/run.py"

    # Verify run.py is executable boilerplate
    script_content = run_py.read_text(encoding="utf-8")
    assert "import argparse" in script_content
    assert "print(json.dumps(result))" in script_content


def test_init_fails_on_existing_files_without_force(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd_dir = temp_home / "cwd"
    cwd_dir.mkdir()
    monkeypatch.setattr(Path, "cwd", lambda: cwd_dir)

    runner = CliRunner()
    target = cwd_dir / "target_skills"
    target.mkdir()

    # 1. Initialize first time
    result = runner.invoke(
        skills_app,
        [
            "init",
            "my-skill",
            "--target-dir",
            str(target),
        ],
    )
    assert result.exit_code == 0

    # 2. Try initializing again (should fail)
    result2 = runner.invoke(
        skills_app,
        [
            "init",
            "my-skill",
            "--target-dir",
            str(target),
        ],
    )
    assert result2.exit_code == 1
    # The console wraps the long tmp path, which can split "already exists".
    assert "already exists" in " ".join(result2.output.split())


def test_init_overwrites_with_force(temp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cwd_dir = temp_home / "cwd"
    cwd_dir.mkdir()
    monkeypatch.setattr(Path, "cwd", lambda: cwd_dir)

    runner = CliRunner()
    target = cwd_dir / "target_skills"
    target.mkdir()

    # Create an existing directory with an unrelated user file
    skill_dir = target / "force-skill"
    skill_dir.mkdir(parents=True)
    unrelated_file = skill_dir / "keep_me.txt"
    unrelated_file.write_text("user content", encoding="utf-8")

    # Try creating with force
    result = runner.invoke(
        skills_app,
        [
            "init",
            "force-skill",
            "--target-dir",
            str(target),
            "--force",
        ],
    )
    assert result.exit_code == 0
    assert "Initialized" in result.output

    # Verify both generated file and unrelated file exist
    assert (skill_dir / "SKILL.md").is_file()
    assert unrelated_file.is_file()
    assert unrelated_file.read_text(encoding="utf-8") == "user content"


def test_init_resolves_target_dir_fallback_order(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd_dir = temp_home / "cwd"
    cwd_dir.mkdir()
    monkeypatch.setattr(Path, "cwd", lambda: cwd_dir)

    runner = CliRunner()

    # Candidates list (precedence high to low):
    # 1. workspace_root / "skills"
    # 2. workspace_root / ".agents" / "skills"
    # 3. Path.home() / ".agents" / "skills"
    # 4. default_agentos_home() / "skills"

    # Let's create candidate #3 (Path.home() / ".agents" / "skills")
    personal_skills = Path.home() / ".agents" / "skills"
    personal_skills.mkdir(parents=True)

    # Stub GatewayConfig
    class StubGatewayConfig:
        workspace_dir = str(cwd_dir)

    monkeypatch.setattr(
        "agentos.gateway.config.GatewayConfig.load", lambda path: StubGatewayConfig()
    )

    result = runner.invoke(skills_app, ["init", "resolved-skill"])
    assert result.exit_code == 0

    # Since only candidate #3 exists (high priority candidate #1 and #2 don't exist yet),
    # it should be picked!
    assert (personal_skills / "resolved-skill" / "SKILL.md").is_file()


def test_init_success_with_underscore(temp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cwd_dir = temp_home / "cwd"
    cwd_dir.mkdir()
    monkeypatch.setattr(Path, "cwd", lambda: cwd_dir)

    runner = CliRunner()
    target = cwd_dir / "target_skills"
    target.mkdir()

    result = runner.invoke(
        skills_app,
        [
            "init",
            "my_skill",
            "--target-dir",
            str(target),
        ],
    )
    assert result.exit_code == 0
    assert "Initialized custom skill template" in result.output
    assert (target / "my_skill" / "SKILL.md").is_file()


def test_init_roundtrip_loader(temp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentos.skills.loader import SkillLoader

    cwd_dir = temp_home / "cwd"
    cwd_dir.mkdir()
    monkeypatch.setattr(Path, "cwd", lambda: cwd_dir)

    runner = CliRunner()
    target = cwd_dir / "target_skills"
    target.mkdir()

    result = runner.invoke(
        skills_app,
        [
            "init",
            "roundtrip-skill",
            "--target-dir",
            str(target),
            "--description",
            "Roundtrip test description",
            "-t",
            "trigger1",
        ],
    )
    assert result.exit_code == 0

    # Initialize loader with target as workspace_dir
    loader = SkillLoader(workspace_dir=target)
    skills = loader.load_all()

    # Find the skill in loaded list
    skill_spec = next((s for s in skills if s.name == "roundtrip-skill"), None)
    assert skill_spec is not None
    assert skill_spec.description == "Roundtrip test description"
    assert skill_spec.triggers == ["trigger1"]
    assert skill_spec.always is False
    assert skill_spec.metadata is not None
    assert skill_spec.metadata.emoji == "💡"

    # Now let's test editing the SKILL.md to add metadata.requires in the YAML frontmatter
    skill_md = target / "roundtrip-skill" / "SKILL.md"
    content = skill_md.read_text(encoding="utf-8")

    # Split frontmatter and body
    parts = content.split("---")
    assert len(parts) >= 3

    # Load YAML frontmatter
    frontmatter = yaml.safe_load(parts[1])

    # Add requires under metadata
    frontmatter["metadata"]["requires"] = {
        "bins": ["curl"],
        "env": ["API_KEY"],
    }

    # Dump it back
    parts[1] = "\n" + yaml.safe_dump(frontmatter, sort_keys=False)
    new_content = "---".join(parts)
    skill_md.write_text(new_content, encoding="utf-8")

    # Invalidate cache and reload
    loader.invalidate_cache()
    skills = loader.load_all()

    skill_spec = next((s for s in skills if s.name == "roundtrip-skill"), None)
    assert skill_spec is not None
    assert skill_spec.metadata is not None
    assert skill_spec.metadata.requires is not None
    assert skill_spec.metadata.requires.bins == ["curl"]
    # Coerced to list of SkillEnvVar
    assert skill_spec.metadata.requires.env_names == ["API_KEY"]
