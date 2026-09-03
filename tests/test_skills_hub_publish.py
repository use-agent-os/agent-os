"""CLI/hub tests for ``agentos skills publish``."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from agentos.cli.skills_cmd import skills_app
from agentos.skills.hub.publisher import publish_skill

runner = CliRunner()


def _write_skill(root: Path) -> Path:
    skill_dir = root / "demo-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: demo-skill\n"
        "description: Reproduce skills publish false success\n"
        "---\n\n"
        "# Demo\n\n"
        "Body long enough to pass validation.\n",
        encoding="utf-8",
    )
    return skill_dir


@pytest.mark.asyncio
async def test_publish_skill_fails_when_gh_fork_returns_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_dir = _write_skill(tmp_path)

    class FakeProc:
        def __init__(self) -> None:
            self.returncode = 1

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"failed to fork: HTTP 404: Not Found\n"

    async def fake_exec(*_args, **_kwargs):
        return FakeProc()

    monkeypatch.setattr(
        "asyncio.create_subprocess_exec",
        fake_exec,
    )

    result = await publish_skill(skill_dir, target_repo="use-agent-os/missing-repo")
    assert result.success is False
    assert "Failed to fork" in result.message
    assert "404" in result.message


@pytest.mark.asyncio
async def test_publish_skill_succeeds_when_gh_fork_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_dir = _write_skill(tmp_path)

    class FakeProc:
        def __init__(self) -> None:
            self.returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def fake_exec(*_args, **_kwargs):
        return FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    result = await publish_skill(skill_dir, target_repo="use-agent-os/agent-os")
    assert result.success is True
    assert "Fork created" in result.message


def test_skills_publish_cli_exits_nonzero_on_validation_failure(tmp_path: Path) -> None:
    missing = tmp_path / "missing-dir"
    result = runner.invoke(skills_app, ["publish", str(missing)])
    assert result.exit_code == 1
    assert "Failed:" in result.output
    assert "Not a directory" in result.output


def test_skills_publish_cli_exits_nonzero_when_fork_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_dir = _write_skill(tmp_path)

    async def fake_publish(_path, target_repo=None):
        return SimpleNamespace(
            success=False,
            message=f"Failed to fork {target_repo}: HTTP 404",
            skill_name="demo-skill",
        )

    monkeypatch.setattr("agentos.skills.hub.publisher.publish_skill", fake_publish)
    result = runner.invoke(
        skills_app,
        ["publish", str(skill_dir), "--repo", "use-agent-os/missing-repo"],
    )
    assert result.exit_code == 1
    assert "Failed:" in result.output
