"""SkillSource ABC and Community source data models."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SkillMeta:
    """Metadata for a skill in a Community source listing."""

    name: str
    description: str = ""
    version: str = ""
    author: str = ""
    source_id: str = ""
    trust_level: str = "community"  # "builtin" | "trusted" | "community"
    identifier: str = ""  # source-specific ID (e.g. slug@version)
    homepage: str = ""
    license: str = ""
    tags: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    provider: str = ""  # publisher/brand (e.g. Bankr catalog "provider")
    logo: str = ""  # raw URL to a logo asset, or "" for an initials fallback
    category: str = ""  # coarse grouping for browse filters (e.g. "defi")
    setup: list[str] = field(default_factory=list)  # ordered setup steps, if any
    demo: dict[str, Any] = field(default_factory=dict)  # {title, language, code}


@dataclass
class SkillBundle:
    """Downloaded skill ready for installation."""

    name: str
    files: dict[str, str | bytes] = field(default_factory=dict)  # relative_path → content
    meta: SkillMeta | None = None

    @property
    def skill_md(self) -> str | None:
        content = self.files.get("SKILL.md")
        if isinstance(content, str):
            return content
        if isinstance(content, bytes):
            try:
                return content.decode("utf-8")
            except UnicodeDecodeError:
                return None
        return None


class SkillSource(ABC):
    """Abstract base class for skill Community sources."""

    @abstractmethod
    async def search(self, query: str, limit: int = 20) -> list[SkillMeta]:
        """Search for skills matching query."""

    @abstractmethod
    async def fetch(self, identifier: str) -> SkillBundle | None:
        """Download a skill by its source-specific identifier."""

    @abstractmethod
    async def inspect(self, identifier: str) -> SkillMeta | None:
        """Get metadata for a skill without downloading."""

    @property
    @abstractmethod
    def source_id(self) -> str:
        """Unique identifier for this source (e.g. 'clawhub', 'github')."""

    @property
    @abstractmethod
    def trust_level(self) -> str:
        """Trust level: 'builtin', 'trusted', or 'community'."""
