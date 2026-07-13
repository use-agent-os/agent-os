"""SourceRouter — aggregates search/fetch across multiple SkillSource adapters."""

from __future__ import annotations

import asyncio

import structlog

from agentos.skills.hub.source import SkillBundle, SkillMeta, SkillSource

log = structlog.get_logger(__name__)


class SourceRouter:
    """Routes skill operations to the appropriate source adapter."""

    def __init__(self, sources: list[SkillSource] | None = None) -> None:
        self._sources: dict[str, SkillSource] = {}
        for s in sources or []:
            self._sources[s.source_id] = s

    def add_source(self, source: SkillSource) -> None:
        self._sources[source.source_id] = source

    def get_source(self, source_id: str) -> SkillSource | None:
        return self._sources.get(source_id)

    @property
    def source_ids(self) -> list[str]:
        return list(self._sources.keys())

    async def search(
        self, query: str, limit: int = 20, source_id: str | None = None
    ) -> list[SkillMeta]:
        """Search across all sources (or a specific one). Returns merged results."""
        if source_id:
            src = self._sources.get(source_id)
            if src is None:
                return []
            return await src.search(query, limit=limit)

        # Search all sources in parallel
        tasks = [src.search(query, limit=limit) for src in self._sources.values()]
        if not tasks:
            return []

        all_results: list[SkillMeta] = []
        for result_list in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result_list, list):
                all_results.extend(result_list)
            else:
                log.warning("router.search_source_failed", error=str(result_list))

        # Deduplicate by name (first occurrence wins — ordered by source registration)
        seen: set[str] = set()
        deduped: list[SkillMeta] = []
        for r in all_results:
            if r.name not in seen:
                seen.add(r.name)
                deduped.append(r)
        return deduped[:limit]

    async def fetch(self, identifier: str, source_id: str) -> SkillBundle | None:
        """Fetch a skill from a specific source."""
        src = self._sources.get(source_id)
        if src is None:
            log.warning("router.fetch_unknown_source", source_id=source_id)
            return None
        return await src.fetch(identifier)

    async def inspect(self, identifier: str, source_id: str) -> SkillMeta | None:
        """Get metadata from a specific source."""
        src = self._sources.get(source_id)
        if src is None:
            return None
        return await src.inspect(identifier)
