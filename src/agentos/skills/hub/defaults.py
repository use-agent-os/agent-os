"""Shared defaults for Community skill sources and installer wiring."""

from __future__ import annotations

import os
from pathlib import Path

from agentos.paths import default_agentos_home
from agentos.skills.hub.clawhub import ClawHubSource
from agentos.skills.hub.github import GitHubSource
from agentos.skills.hub.installer import SkillInstaller
from agentos.skills.hub.lockfile import Lockfile
from agentos.skills.hub.router import SourceRouter
from agentos.skills.hub.source import SkillSource

_default_router: SourceRouter | None = None


def get_default_skill_router() -> SourceRouter:
    """Return the default Community source router shared by CLI, RPC, and tools."""

    global _default_router
    if _default_router is None:
        sources: list[SkillSource] = [
            ClawHubSource(token=os.environ.get("CLAWHUB_TOKEN")),
            GitHubSource(token=os.environ.get("GITHUB_TOKEN")),
        ]
        _default_router = SourceRouter(sources)
    return _default_router


def build_default_skill_installer(*, managed_dir: Path | None = None) -> SkillInstaller:
    """Build a default installer, optionally aligned to the active loader layer."""

    return SkillInstaller(router=get_default_skill_router(), managed_dir=managed_dir)


def installed_skill_names() -> set[str]:
    """Return skill names recorded as Community installs in the lockfile."""

    lockfile_path = default_agentos_home() / "skills-lock.json"
    return set(Lockfile.load(lockfile_path).installed.keys())
