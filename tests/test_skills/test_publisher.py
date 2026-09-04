"""Tests for agentos.skills.hub.publisher — validate_skill_dir + publish_skill."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from agentos.skills.hub.publisher import publish_skill, validate_skill_dir

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def valid_skill(tmp_path: Path) -> Path:
    """Create a minimal valid skill directory and return its path."""
    d = tmp_path / "demo-skill"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\n"
        "name: demo-skill\n"
        "description: A test skill\n"
        "---\n"
        "\n"
        "Body text that is long enough to pass 20-char minimum.\n",
        encoding="utf-8",
    )
    return d


@pytest.fixture
def skill_dir_missing_frontmatter(tmp_path: Path) -> Path:
    """Skill with no YAML frontmatter."""
    d = tmp_path / "no-frontmatter"
    d.mkdir()
    (d / "SKILL.md").write_text("Just some text without frontmatter.\n", encoding="utf-8")
    return d


@pytest.fixture
def skill_dir_missing_name(tmp_path: Path) -> Path:
    """Skill with frontmatter but missing 'name' field."""
    d = tmp_path / "no-name"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\n"
        "description: Missing name field\n"
        "---\n"
        "\n"
        "Body text that is long enough.\n",
        encoding="utf-8",
    )
    return d


@pytest.fixture
def skill_dir_short_body(tmp_path: Path) -> Path:
    """Skill whose body is shorter than 20 characters."""
    d = tmp_path / "short-body"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\n"
        "name: short-body\n"
        "description: Too short\n"
        "---\n"
        "\n"
        "Tiny.\n",
        encoding="utf-8",
    )
    return d


# ── validate_skill_dir ──────────────────────────────────────────────────────


def test_validate_valid_skill_dir(valid_skill: Path) -> None:
    errors = validate_skill_dir(valid_skill)
    assert errors == []


def test_validate_nonexistent_dir(tmp_path: Path) -> None:
    path = tmp_path / "does-not-exist"
    errors = validate_skill_dir(path)
    assert len(errors) == 1
    assert "Not a directory" in errors[0]


def test_validate_missing_skill_file(tmp_path: Path) -> None:
    d = tmp_path / "empty-dir"
    d.mkdir()
    errors = validate_skill_dir(d)
    assert len(errors) == 1
    assert "Missing SKILL.md" in errors[0]


def test_validate_missing_frontmatter(skill_dir_missing_frontmatter: Path) -> None:
    errors = validate_skill_dir(skill_dir_missing_frontmatter)
    assert any("missing YAML frontmatter" in e for e in errors)


def test_validate_missing_name(skill_dir_missing_name: Path) -> None:
    errors = validate_skill_dir(skill_dir_missing_name)
    assert any("name" in e and "required" in e for e in errors)


def test_validate_short_body(skill_dir_short_body: Path) -> None:
    errors = validate_skill_dir(skill_dir_short_body)
    assert any("too short" in e.lower() for e in errors)


# ── publish_skill — validation path ──────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_validation_failure(tmp_path: Path) -> None:
    result = await publish_skill(tmp_path / "does-not-exist")
    assert result.success is False
    assert "Not a directory" in result.message


@pytest.mark.asyncio
async def test_publish_validation_passes_no_repo(valid_skill: Path) -> None:
    result = await publish_skill(valid_skill)
    assert result.success is True
    assert "--repo" in result.message


# ── publish_skill — fork / CLI path ─────────────────────────────────────


class FakePopen:
    """Minimal stand-in for asyncio.subprocess.Process."""

    def __init__(self, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = asyncio.StreamReader()
        self._stderr = asyncio.StreamReader()
        self._stderr.feed_data(stderr)
        self._stderr.feed_eof()
        self._stdout.feed_eof()

    async def communicate(self) -> tuple[bytes, bytes]:
        out = b""
        while not self._stdout.at_eof():
            out += await self._stdout.read(4096)
        err = b""
        while not self._stderr.at_eof():
            err += await self._stderr.read(4096)
        return out, err

    async def wait(self) -> int:
        return self.returncode


@pytest.mark.asyncio
async def test_publish_fork_success(valid_skill: Path) -> None:
    async def fake_exec(*args: object, **kwargs: object) -> FakePopen:
        return FakePopen(returncode=0)

    with patch.object(asyncio, "create_subprocess_exec", fake_exec):
        result = await publish_skill(valid_skill, target_repo="use-agent-os/agent-os")
    assert result.success is True
    assert "ready for PR" in result.message
    assert result.skill_name == "demo-skill"


@pytest.mark.asyncio
async def test_publish_fork_failure(valid_skill: Path) -> None:
    async def fake_exec(*args: object, **kwargs: object) -> FakePopen:
        return FakePopen(returncode=1, stderr=b"HTTP 404: Not Found")

    with patch.object(asyncio, "create_subprocess_exec", fake_exec):
        result = await publish_skill(valid_skill, target_repo="use-agent-os/does-not-exist")
    assert result.success is False
    assert "Fork failed" in result.message
    assert "404" in result.message


@pytest.mark.asyncio
async def test_publish_fork_failure_no_stderr(valid_skill: Path) -> None:
    """When gh fails with no stderr, we fall back to a returncode message."""

    async def fake_exec(*args: object, **kwargs: object) -> FakePopen:
        return FakePopen(returncode=2)

    with patch.object(asyncio, "create_subprocess_exec", fake_exec):
        result = await publish_skill(valid_skill, target_repo="use-agent-os/ghost")
    assert result.success is False
    assert "Fork failed" in result.message
    assert "exited with code 2" in result.message


@pytest.mark.asyncio
async def test_publish_fork_file_not_found(valid_skill: Path) -> None:
    """When `gh` CLI is not installed."""

    async def fake_exec(*args: object, **kwargs: object) -> FakePopen:
        raise FileNotFoundError("gh not found")

    with patch.object(asyncio, "create_subprocess_exec", fake_exec):
        result = await publish_skill(valid_skill, target_repo="use-agent-os/agent-os")
    assert result.success is False
    assert "GitHub CLI (gh) not found" in result.message


@pytest.mark.asyncio
async def test_publish_fork_invalid_repo(valid_skill: Path) -> None:
    """Repo without owner/repo format."""
    result = await publish_skill(valid_skill, target_repo="invalid-repo-format")
    assert result.success is False
    assert "Invalid repo format" in result.message
