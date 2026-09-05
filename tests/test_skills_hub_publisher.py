"""Regression tests for skills hub publisher — gh fork exit code handling."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentos.skills.hub.publisher import publish_skill


class _FakeProc:
    def __init__(self, returncode: int, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stderr = stderr

    async def wait(self) -> int:
        return self.returncode

    async def read(self) -> bytes:
        return self._stderr

    @property
    def stderr(self) -> _FakeProc:
        return self


@pytest.mark.asyncio
async def test_publish_skill_forks_and_returns_success_on_zero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    async def fake_subprocess(*args: str, **kwargs: object) -> _FakeProc:
        captured["argv"] = args
        return _FakeProc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)

    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: A test skill.\n---\n\n# My Skill\nSome content here.",
        encoding="utf-8",
    )

    result = await publish_skill(skill_dir, target_repo="owner/repo")

    assert result.success is True
    assert "ready for PR" in result.message
    assert captured.get("argv") == ("gh", "repo", "fork", "owner/repo", "--clone=false")


@pytest.mark.asyncio
async def test_publish_skill_reports_failure_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_subprocess(*args: str, **kwargs: object) -> _FakeProc:
        return _FakeProc(1, b"GraphQL: resource not accessible (http 403): repository not forkable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)

    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: A test skill.\n---\n\n# My Skill\nSome content here.",
        encoding="utf-8",
    )

    result = await publish_skill(skill_dir, target_repo="owner/repo")

    assert result.success is False
    assert "Failed to fork" in result.message
    assert "owner/repo" in result.message
    assert "http 403" in result.message
    assert "gh auth status" in result.message.lower()


@pytest.mark.asyncio
async def test_publish_skill_reports_failure_on_fork_rate_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_subprocess(*args: str, **kwargs: object) -> _FakeProc:
        return _FakeProc(1, b"API rate limit exceeded. Please retry in 60 seconds.")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)

    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: A test skill.\n---\n\n# My Skill\nSome content here.",
        encoding="utf-8",
    )

    result = await publish_skill(skill_dir, target_repo="owner/repo")

    assert result.success is False
    assert "rate limit" in result.message.lower()


@pytest.mark.asyncio
async def test_publish_skill_reports_failure_when_gh_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_subprocess(*args: str, **kwargs: object) -> None:
        raise FileNotFoundError("gh not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)

    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: A test skill.\n---\n\n# My Skill\nSome content here.",
        encoding="utf-8",
    )

    result = await publish_skill(skill_dir, target_repo="owner/repo")

    assert result.success is False
    assert "gh" in result.message.lower()
    assert "not found" in result.message.lower()
