"""Regression tests for skills publish and hub publisher (#1050)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from agentos.cli.skills_cmd import skills_app
from agentos.skills.hub.publisher import PublishResult, publish_skill

runner = CliRunner()


class _MockProcess:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def _create_valid_skill(tmp_path: Path, name: str = "demo-skill") -> Path:
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test description.\n---\n\n# Skill\nContent here.\n",
        encoding="utf-8",
    )
    return skill_dir


@pytest.mark.asyncio
async def test_publish_skill_fork_success(tmp_path: Path) -> None:
    skill_dir = _create_valid_skill(tmp_path)
    captured: dict[str, Any] = {}

    async def fake_exec(*args: Any, **kwargs: Any) -> _MockProcess:
        captured["args"] = args
        return _MockProcess(returncode=0)

    with patch.object(asyncio, "create_subprocess_exec", side_effect=fake_exec):
        result = await publish_skill(skill_dir, target_repo="owner/repo")

    assert result.success is True
    assert "Fork of owner/repo created via GitHub CLI" in result.message
    assert "skill/demo-skill" in result.message
    assert captured["args"] == ("gh", "repo", "fork", "owner/repo", "--clone=false")


@pytest.mark.asyncio
async def test_publish_skill_fork_failure_nonzero_exit(tmp_path: Path) -> None:
    skill_dir = _create_valid_skill(tmp_path)

    async def fake_exec(*args: Any, **kwargs: Any) -> _MockProcess:
        return _MockProcess(
            returncode=1,
            stderr=b"GraphQL: resource not accessible (http 403): repository not forkable",
        )

    with patch.object(asyncio, "create_subprocess_exec", side_effect=fake_exec):
        result = await publish_skill(skill_dir, target_repo="owner/repo")

    assert result.success is False
    assert "Failed to fork owner/repo" in result.message
    assert "repository not forkable" in result.message


@pytest.mark.asyncio
async def test_publish_skill_fork_failure_stdout_fallback(tmp_path: Path) -> None:
    skill_dir = _create_valid_skill(tmp_path)

    async def fake_exec(*args: Any, **kwargs: Any) -> _MockProcess:
        return _MockProcess(returncode=2, stdout=b"HTTP 404: Not Found")

    with patch.object(asyncio, "create_subprocess_exec", side_effect=fake_exec):
        result = await publish_skill(skill_dir, target_repo="owner/repo")

    assert result.success is False
    assert "Failed to fork owner/repo: HTTP 404: Not Found" in result.message


@pytest.mark.asyncio
async def test_publish_skill_gh_not_found(tmp_path: Path) -> None:
    skill_dir = _create_valid_skill(tmp_path)

    def fake_exec(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("gh command not found")

    with patch.object(asyncio, "create_subprocess_exec", side_effect=fake_exec):
        result = await publish_skill(skill_dir, target_repo="owner/repo")

    assert result.success is False
    assert "GitHub CLI (gh) not found" in result.message


@pytest.mark.asyncio
async def test_publish_skill_invalid_repo_format(tmp_path: Path) -> None:
    skill_dir = _create_valid_skill(tmp_path)
    result = await publish_skill(skill_dir, target_repo="invalid-repo-without-slash")
    assert result.success is False
    assert "Invalid repo format" in result.message


def test_cli_skills_publish_success_exits_zero(tmp_path: Path) -> None:
    skill_dir = _create_valid_skill(tmp_path)
    mock_res = PublishResult(success=True, message="Fork created successfully.")

    with patch(
        "agentos.skills.hub.publisher.publish_skill",
        new=AsyncMock(return_value=mock_res),
    ):
        result = runner.invoke(skills_app, ["publish", str(skill_dir), "--repo", "owner/repo"])

    assert result.exit_code == 0
    assert "OK:" in result.output
    assert "Fork created successfully." in result.output


def test_cli_skills_publish_failure_exits_one(tmp_path: Path) -> None:
    skill_dir = _create_valid_skill(tmp_path)
    mock_res = PublishResult(success=False, message="Failed to fork owner/repo: HTTP 404.")

    with patch(
        "agentos.skills.hub.publisher.publish_skill",
        new=AsyncMock(return_value=mock_res),
    ):
        result = runner.invoke(skills_app, ["publish", str(skill_dir), "--repo", "owner/repo"])

    assert result.exit_code == 1
    assert "Failed:" in result.output
    assert "HTTP 404" in result.output


def test_cli_skills_publish_validation_failure_exits_one(tmp_path: Path) -> None:
    missing_dir = tmp_path / "does-not-exist"
    result = runner.invoke(skills_app, ["publish", str(missing_dir)])

    assert result.exit_code == 1
    assert "Failed:" in result.output
    assert "Not a directory" in result.output
