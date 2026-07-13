"""ClawHub Community source adapter - connects to clawhub.ai API."""

from __future__ import annotations

import structlog

from agentos.env import trust_env as _trust_env
from agentos.skills.hub.source import SkillBundle, SkillMeta, SkillSource

log = structlog.get_logger(__name__)

_DEFAULT_BASE_URL = "https://clawhub.ai"


class ClawHubSource(SkillSource):
    """Skill source backed by the ClawHub community registry."""

    def __init__(self, base_url: str = _DEFAULT_BASE_URL, token: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token

    @property
    def source_id(self) -> str:
        return "clawhub"

    @property
    def trust_level(self) -> str:
        return "community"

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Accept": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def search(self, query: str, limit: int = 20) -> list[SkillMeta]:
        import httpx

        url = f"{self._base_url}/api/v1/search"
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=_trust_env()) as client:
                resp = await client.get(
                    url, params={"q": query, "limit": limit}, headers=self._headers()
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            log.warning("clawhub.search_failed", error=str(exc))
            return []

        # Handle rate limit / error disguised as 200
        if isinstance(data, str) or (isinstance(data, dict) and "error" in data):
            log.warning("clawhub.search_error", data=str(data)[:100])
            return []

        results = []
        for item in data if isinstance(data, list) else data.get("results", data.get("skills", [])):
            results.append(
                SkillMeta(
                    name=item.get("displayName", item.get("name", item.get("slug", ""))),
                    description=item.get("summary", item.get("description", "")),
                    version=item.get("version", ""),
                    author=item.get("author", ""),
                    source_id=self.source_id,
                    trust_level=self.trust_level,
                    identifier=item.get("slug", item.get("name", "")),
                    homepage=item.get("homepage", ""),
                    license=item.get("license", ""),
                    tags=item.get("tags", []),
                )
            )
        return results[:limit]

    async def fetch(self, identifier: str) -> SkillBundle | None:
        import io
        import zipfile

        import httpx

        url = f"{self._base_url}/api/v1/download"
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=_trust_env()) as client:
                resp = await client.get(url, params={"slug": identifier}, headers=self._headers())
                resp.raise_for_status()
        except Exception as exc:
            log.warning("clawhub.fetch_failed", identifier=identifier, error=str(exc))
            return None

        # Detect error responses disguised as 200 (e.g. rate limiting)
        if (
            len(resp.content) < 50
            and not resp.content.startswith(b"PK")
            and not resp.content.startswith(b"---")
        ):
            text = resp.text.strip()
            if (
                "rate limit" in text.lower()
                or "error" in text.lower()
                or "not found" in text.lower()
            ):
                log.warning("clawhub.fetch_error_response", identifier=identifier, body=text[:100])
                return None

        files: dict[str, str | bytes] = {}
        try:
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                import posixpath

                for name in zf.namelist():
                    if name.endswith("/"):
                        continue
                    parts = name.split("/", 1)
                    rel = parts[1] if len(parts) > 1 else parts[0]
                    rel = posixpath.normpath(rel)
                    if rel.startswith("..") or rel.startswith("/"):
                        continue
                    try:
                        raw = zf.read(name)
                        if rel == "SKILL.md":
                            files[rel] = raw.decode("utf-8")
                        else:
                            try:
                                files[rel] = raw.decode("utf-8")
                            except UnicodeDecodeError:
                                files[rel] = raw
                    except UnicodeDecodeError:
                        log.warning("clawhub.fetch_bad_skill_encoding", identifier=identifier)
                        return None
        except zipfile.BadZipFile:
            # Might be raw SKILL.md content — validate it has frontmatter
            if resp.text.strip().startswith("---"):
                files["SKILL.md"] = resp.text
            else:
                log.warning(
                    "clawhub.fetch_invalid_content", identifier=identifier, size=len(resp.content)
                )
                return None

        if "SKILL.md" not in files:
            return None

        return SkillBundle(name=identifier, files=files)

    async def inspect(self, identifier: str) -> SkillMeta | None:
        import httpx

        url = f"{self._base_url}/api/v1/skills/{identifier}"
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=_trust_env()) as client:
                resp = await client.get(url, headers=self._headers())
                resp.raise_for_status()
                item = resp.json()
        except Exception as exc:
            log.warning("clawhub.inspect_failed", identifier=identifier, error=str(exc))
            return None

        return SkillMeta(
            name=item.get("name", item.get("slug", identifier)),
            description=item.get("description", ""),
            version=item.get("version", ""),
            author=item.get("author", ""),
            source_id=self.source_id,
            trust_level=self.trust_level,
            identifier=identifier,
            homepage=item.get("homepage", ""),
            license=item.get("license", ""),
            tags=item.get("tags", []),
        )
