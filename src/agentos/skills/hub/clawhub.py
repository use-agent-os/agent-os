"""ClawHub Community source adapter - connects to clawhub.ai API."""

from __future__ import annotations

import io
import posixpath
import zipfile

import structlog

from agentos.env import trust_env as _trust_env
from agentos.skills.hub.source import SkillBundle, SkillMeta, SkillSource

log = structlog.get_logger(__name__)

_DEFAULT_BASE_URL = "https://clawhub.ai"

# Decompression security caps for untrusted zip downloads (prevent zip-bomb DoS).
MAX_ZIP_ENTRIES: int = 500
MAX_ZIP_ENTRY_BYTES: int = 5 * 1024 * 1024  # 5 MB per entry
MAX_ZIP_TOTAL_BYTES: int = 25 * 1024 * 1024  # 25 MB total uncompressed
_CHUNK_SIZE: int = 64 * 1024


def _detect_root_prefix(infolist: list[zipfile.ZipInfo]) -> str:
    """If all non-directory files are nested inside a single top-level folder,
    return that folder prefix (e.g. 'slug/'). Otherwise return empty string."""
    names = [
        info.filename.replace("\\", "/").strip()
        for info in infolist
        if not info.is_dir() and not info.filename.replace("\\", "/").strip().endswith("/")
    ]
    if not names or "SKILL.md" in names:
        return ""
    first_parts = {name.split("/", 1)[0] for name in names if "/" in name}
    if len(first_parts) == 1 and all("/" in name for name in names):
        top = next(iter(first_parts))
        if top and top != "." and top != "..":
            return f"{top}/"
    return ""


def _normalize_zip_entry_path(name: str, root_prefix: str = "") -> str | None:
    """Normalize and validate a zip entry path against zip-slip/traversal.

    Handles POSIX and Windows separators, drive letters, and relative path escape.
    Returns normalized relative path or None if unsafe.
    """
    clean = name.replace("\\", "/").strip()
    if not clean or clean.endswith("/"):
        return None

    # Block absolute paths and Windows drive letters (e.g. /etc/passwd, C:\foo)
    if clean.startswith("/"):
        return None
    segments = [s for s in clean.split("/") if s]
    if any(len(s) >= 2 and s[1] == ":" and s[0].isalpha() for s in segments):
        return None
    # Block any directory traversal segments
    if ".." in segments:
        return None

    # Strip single top-level archive directory if present
    if root_prefix and clean.startswith(root_prefix):
        clean = clean[len(root_prefix) :]
    elif not root_prefix and "/" in clean and "SKILL.md" not in clean.split("/"):
        # Fallback if single wrapper directory was not detected globally
        parts = clean.split("/", 1)
        if len(parts) > 1:
            clean = parts[1]

    normalized = posixpath.normpath(clean)
    if (
        normalized.startswith("..")
        or normalized.startswith("/")
        or ".." in normalized.split("/")
        or normalized == "."
    ):
        return None
    return normalized


def _read_zip_entry_bounded(
    zf: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    max_entry_bytes: int,
    current_total_bytes: int,
    max_total_bytes: int,
) -> bytes | None:
    """Read a zip entry with strict per-entry and total decompression ceilings.

    Returns None if declared or streaming decompressed size exceeds the limits.
    """
    if info.file_size > max_entry_bytes:
        return None
    if current_total_bytes + info.file_size > max_total_bytes:
        return None

    chunks: list[bytes] = []
    entry_read = 0
    with zf.open(info) as stream:
        while True:
            chunk = stream.read(_CHUNK_SIZE)
            if not chunk:
                break
            entry_read += len(chunk)
            if entry_read > max_entry_bytes:
                return None
            if current_total_bytes + entry_read > max_total_bytes:
                return None
            chunks.append(chunk)
    return b"".join(chunks)


class ClawHubSource(SkillSource):
    """Skill source backed by the ClawHub community registry."""

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        token: str | None = None,
        *,
        max_zip_entries: int = MAX_ZIP_ENTRIES,
        max_zip_entry_bytes: int = MAX_ZIP_ENTRY_BYTES,
        max_zip_total_bytes: int = MAX_ZIP_TOTAL_BYTES,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self.max_zip_entries = max_zip_entries
        self.max_zip_entry_bytes = max_zip_entry_bytes
        self.max_zip_total_bytes = max_zip_total_bytes

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
                    # `or ""` (not a .get default): the API may send an explicit
                    # null, which .get returns as None and would break the str
                    # contract downstream (e.g. CLI description slicing).
                    description=item.get("summary") or item.get("description") or "",
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
                infolist = zf.infolist()
                if len(infolist) > self.max_zip_entries:
                    log.warning(
                        "clawhub.fetch_zip_entries_exceeded",
                        identifier=identifier,
                        count=len(infolist),
                        max_entries=self.max_zip_entries,
                    )
                    return None

                declared_total = sum(info.file_size for info in infolist if not info.is_dir())
                if declared_total > self.max_zip_total_bytes:
                    log.warning(
                        "clawhub.fetch_declared_size_exceeded",
                        identifier=identifier,
                        declared_size=declared_total,
                        max_size=self.max_zip_total_bytes,
                    )
                    return None

                root_prefix = _detect_root_prefix(infolist)
                total_decompressed = 0
                for info in infolist:
                    if info.is_dir() or info.filename.endswith("/") or info.filename.endswith("\\"):
                        continue

                    rel = _normalize_zip_entry_path(info.filename, root_prefix=root_prefix)
                    if rel is None:
                        log.warning(
                            "clawhub.fetch_unsafe_path_skipped",
                            identifier=identifier,
                            filename=info.filename,
                        )
                        continue

                    raw = _read_zip_entry_bounded(
                        zf,
                        info,
                        max_entry_bytes=self.max_zip_entry_bytes,
                        current_total_bytes=total_decompressed,
                        max_total_bytes=self.max_zip_total_bytes,
                    )
                    if raw is None:
                        log.warning(
                            "clawhub.fetch_decompression_limit_exceeded",
                            identifier=identifier,
                            filename=info.filename,
                        )
                        return None

                    total_decompressed += len(raw)

                    if rel == "SKILL.md":
                        try:
                            files[rel] = raw.decode("utf-8")
                        except UnicodeDecodeError:
                            log.warning("clawhub.fetch_bad_skill_encoding", identifier=identifier)
                            return None
                    else:
                        try:
                            files[rel] = raw.decode("utf-8")
                        except UnicodeDecodeError:
                            files[rel] = raw
        except zipfile.BadZipFile:
            # Might be raw SKILL.md content — validate it has frontmatter
            if resp.text.strip().startswith("---"):
                if len(resp.content) > self.max_zip_entry_bytes:
                    log.warning(
                        "clawhub.fetch_raw_skill_too_large",
                        identifier=identifier,
                        size=len(resp.content),
                    )
                    return None
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
            description=item.get("description") or "",
            version=item.get("version", ""),
            author=item.get("author", ""),
            source_id=self.source_id,
            trust_level=self.trust_level,
            identifier=identifier,
            homepage=item.get("homepage", ""),
            license=item.get("license", ""),
            tags=item.get("tags", []),
        )
