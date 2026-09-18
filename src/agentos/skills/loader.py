"""SKILL.md frontmatter parser and multi-layer skill loader."""

from __future__ import annotations

import codecs
import re
from pathlib import Path
from typing import Any, cast

import structlog
import yaml

from agentos.paths import default_agentos_home
from agentos.skills.publishers import resolve_declared_publisher
from agentos.skills.types import (
    SkillInstallSpec,
    SkillLayer,
    SkillPlatformMeta,
    SkillProvenance,
    SkillPublisher,
    SkillRequires,
    SkillSpec,
)

log = structlog.get_logger(__name__)

MAX_SKILL_FILE_BYTES = 256_000  # 256KB per SKILL.md
MAX_SKILLS_PER_SOURCE = 200  # per layer cap

# Bump when on-disk snapshot fields change so stale caches are invalidated
# instead of silently losing new fields.
# 9: requires_env carries declared descriptions/URLs instead of bare names,
#    and config_vars was added. A version-8 snapshot would parse without error
#    and silently strip both.
# 10: publisher was added. A version-9 snapshot parses cleanly and would restore
#    every skill without a publisher, so partner skills would silently lose
#    their brand grouping until something else invalidated the cache.
_SNAPSHOT_SCHEMA_VERSION = 11


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _read_skill_md(skill_file: Path) -> str:
    """Read a SKILL.md, honouring a byte-order mark if the file starts with one.

    Windows tools write one by default: PowerShell 5.1's ``Set-Content
    -Encoding UTF8`` prefixes a UTF-8 BOM, and its ``>`` / ``Out-File`` write
    UTF-16. Read as plain ``utf-8``, the first put ``\ufeff`` in front of
    ``---`` so the frontmatter did not match and the skill was dropped without
    a word; the second failed to decode. ``utf-8-sig`` reads a BOM-less file
    exactly as ``utf-8`` does.
    """
    with skill_file.open("rb") as handle:
        head = handle.read(2)
    if head in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
        return skill_file.read_text(encoding="utf-16")
    return skill_file.read_text(encoding="utf-8-sig")


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from SKILL.md.

    Returns (frontmatter_dict, body_content).
    Handles both simple and nested metadata formats.
    """
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not match:
        return {}, text

    fm_text, body = match.groups()
    try:
        frontmatter = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return {}, text

    if not isinstance(frontmatter, dict):
        return {}, text

    return frontmatter, body.strip()


def _resolve_metadata(frontmatter: dict) -> SkillPlatformMeta | None:
    """Extract platform metadata from frontmatter."""
    raw_meta = frontmatter.get("metadata", {})
    if isinstance(raw_meta, dict):
        # Namespace fallback: platform > openclaw > clawdbot > top-level.
        # `agentos` overlays advisory fields such as risk/capabilities
        # without erasing platform dependency metadata kept at the top level
        # or in an upstream namespace.
        base_meta = raw_meta.get(
            "platform",
            raw_meta.get("openclaw", raw_meta.get("clawdbot", raw_meta)),
        )
        if not isinstance(base_meta, dict):
            base_meta = {}
        merged_meta = dict(base_meta)
        agentos_meta = raw_meta.get("agentos", {})
        if isinstance(agentos_meta, dict):
            for key in (
                "emoji",
                "category",
                "skillKey",
                "primaryEnv",
                "homepage",
                "always",
                "os",
                "requires",
                "config",
                "install",
                "risk",
                "risk_level",
                "riskLevel",
                "capabilities",
            ):
                if key in agentos_meta:
                    merged_meta[key] = agentos_meta[key]
        raw_meta = merged_meta
    if not isinstance(raw_meta, dict):
        return None

    requires = None
    raw_req = raw_meta.get("requires", {})
    if isinstance(raw_req, dict):
        # ClawHub frontmatter sometimes uses requires.commands instead of requires.bins.
        bins_value = raw_req.get("bins")
        if bins_value is None:
            bins_value = raw_req.get("commands", [])
        requires = SkillRequires(
            bins=bins_value if isinstance(bins_value, list) else [],
            any_bins=raw_req.get("anyBins", []),
            env=raw_req.get("env", []),
            config=raw_req.get("config", []),
        )

    install_specs: list[SkillInstallSpec] = []
    for item in raw_meta.get("install", []):
        if isinstance(item, dict):
            install_specs.append(
                SkillInstallSpec(
                    kind=item.get("kind", ""),
                    id=item.get("id", ""),
                    label=item.get("label", ""),
                    bins=item.get("bins", []),
                    os=item.get("os", []),
                    formula=item.get("formula", ""),
                    package=item.get("package", ""),
                    module=item.get("module", ""),
                    url=item.get("url", ""),
                )
            )

    always_val = raw_meta.get("always")
    raw_config = raw_meta.get("config", [])
    config_vars = raw_config if isinstance(raw_config, list) else [raw_config]
    return SkillPlatformMeta(
        emoji=raw_meta.get("emoji", ""),
        category=str(raw_meta.get("category", "")).strip().lower(),
        skill_key=raw_meta.get("skillKey", ""),
        primary_env=raw_meta.get("primaryEnv", ""),
        homepage=raw_meta.get("homepage", ""),
        always=bool(always_val) if always_val is not None else None,
        os=_string_list(raw_meta.get("os", [])),
        requires=requires,
        install=install_specs,
        risk_level=str(
            raw_meta.get("risk") or raw_meta.get("risk_level") or raw_meta.get("riskLevel") or ""
        )
        .strip()
        .lower(),
        capabilities=_string_list(raw_meta.get("capabilities", [])),
        config_vars=config_vars,
    )


def _resolve_provenance(frontmatter: dict) -> SkillProvenance:
    """Extract provenance metadata from top-level frontmatter."""
    raw = frontmatter.get("provenance", {})
    if not isinstance(raw, dict):
        raw = {}
    return SkillProvenance(
        origin=str(raw.get("origin") or "unknown"),
        license=str(raw.get("license") or "unknown"),
        upstream_url=str(raw.get("upstream_url") or ""),
        maintained_by=str(raw.get("maintained_by") or "AgentOS"),
    )


def _resolve_skill_publisher(frontmatter: dict, layer: SkillLayer) -> SkillPublisher:
    """Extract the publisher a top-level ``publisher:`` block selects.

    The frontmatter only picks an id; the allowlist supplies the displayed
    fields, and only a bundled manifest may pick one at all, so a directory
    dropped into a writable skills path cannot claim a partner's brand. No block
    at all means no publisher.
    """
    return resolve_declared_publisher(frontmatter.get("publisher"), layer)


def _snapshot_provenance(raw: object) -> SkillProvenance:
    if not isinstance(raw, dict):
        return SkillProvenance()
    return SkillProvenance(
        origin=str(raw.get("origin") or "unknown"),
        license=str(raw.get("license") or "unknown"),
        upstream_url=str(raw.get("upstream_url") or ""),
        maintained_by=str(raw.get("maintained_by") or "AgentOS"),
    )


class SkillLoader:
    """Loads and manages skills from multiple layered directories."""

    def __init__(
        self,
        bundled_dir: Path | None = None,
        workspace_dir: Path | None = None,
        managed_dir: Path | None = None,
        personal_agents_dir: Path | None = None,
        project_agents_dir: Path | None = None,
        extra_dirs: list[Path] | None = None,
        snapshot_path: Path | None = None,
    ) -> None:
        self._bundled_dir = bundled_dir
        self._workspace_dir = workspace_dir
        self._managed_dir = managed_dir
        self._personal_agents_dir = personal_agents_dir
        self._project_agents_dir = project_agents_dir
        self._extra_dirs = extra_dirs or []
        self._snapshot_path = (
            snapshot_path or default_agentos_home() / "cache" / "skills_snapshot.json"
        )
        self._cached: list[SkillSpec] | None = None
        #: Manifest the in-memory cache was built from. Compared on every
        #: ``load_all()`` so a write nobody told us about still lands.
        self._cached_manifest: dict[str, dict[str, float | int]] | None = None

    @property
    def workspace_dir(self) -> Path | None:
        """Public accessor for workspace skill directory."""
        return self._workspace_dir

    @property
    def managed_dir(self) -> Path | None:
        """Public accessor for managed Community-installed skills."""
        return self._managed_dir

    def invalidate_cache(self) -> None:
        """Clear cached skills so next load_all() re-scans from disk."""
        self._cached = None
        self._cached_manifest = None

    def _get_layer_dirs(self) -> list[tuple[Path, SkillLayer]]:
        """Return the layer directories in low → high precedence order.

        This order is the precedence: ``load_all()`` walks it and lets each
        later layer overwrite a same-named skill from an earlier one.
        """
        layer_dirs: list[tuple[Path, SkillLayer]] = []
        for d in self._extra_dirs:
            layer_dirs.append((d, SkillLayer.EXTRA))
        if self._bundled_dir:
            layer_dirs.append((self._bundled_dir, SkillLayer.BUNDLED))
        if self._managed_dir:
            layer_dirs.append((self._managed_dir, SkillLayer.MANAGED))
        if self._personal_agents_dir:
            layer_dirs.append((self._personal_agents_dir, SkillLayer.PERSONAL))
        if self._project_agents_dir:
            layer_dirs.append((self._project_agents_dir, SkillLayer.PROJECT))
        if self._workspace_dir:
            layer_dirs.append((self._workspace_dir, SkillLayer.WORKSPACE))
        return layer_dirs

    def _build_manifest(self) -> dict[str, dict[str, float | int]]:
        """Build a manifest of all SKILL.md files with mtime and size."""
        manifest: dict[str, dict[str, float | int]] = {}
        for dir_path, _layer in self._get_layer_dirs():
            if dir_path.exists():
                for skill_dir in sorted(dir_path.iterdir()):
                    if skill_dir.is_dir() and not skill_dir.name.startswith("."):
                        skill_file = skill_dir / "SKILL.md"
                        if skill_file.exists():
                            stat = skill_file.stat()
                            manifest[str(skill_file)] = {
                                "mtime": stat.st_mtime,
                                "size": stat.st_size,
                            }
        return manifest

    def save_snapshot(self) -> None:
        """Save loaded skills to disk cache for fast cold starts."""
        import json

        skills = self.load_all()
        manifest = self._build_manifest()
        data = {
            "version": _SNAPSHOT_SCHEMA_VERSION,
            "manifest": manifest,
            "skills": [
                {
                    "name": s.name,
                    "description": s.description,
                    "layer": s.layer.value,
                    "always": s.always,
                    "triggers": s.triggers,
                    "content": s.content,
                    "file_path": s.file_path,
                    "base_dir": s.base_dir,
                    "user_invocable": s.user_invocable,
                    "disable_model_invocation": s.disable_model_invocation,
                    "homepage": s.homepage,
                    "provenance": {
                        "origin": s.provenance.origin,
                        "license": s.provenance.license,
                        "upstream_url": s.provenance.upstream_url,
                        "maintained_by": s.provenance.maintained_by,
                    },
                    "publisher": {
                        "id": s.publisher.id,
                        "name": s.publisher.name,
                        "url": s.publisher.url,
                        "logo": s.publisher.logo,
                    },
                    "metadata": {
                        "os": s.metadata.os if s.metadata else [],
                        "emoji": s.metadata.emoji if s.metadata else "",
                        "category": s.metadata.category if s.metadata else "",
                        "skill_key": s.metadata.skill_key if s.metadata else "",
                        "primary_env": s.metadata.primary_env if s.metadata else "",
                        "homepage": s.metadata.homepage if s.metadata else "",
                        "always": s.metadata.always if s.metadata else None,
                        "risk_level": s.metadata.risk_level if s.metadata else "",
                        "capabilities": s.metadata.capabilities if s.metadata else [],
                        "requires_bins": s.metadata.requires.bins
                        if s.metadata and s.metadata.requires
                        else [],
                        "requires_any_bins": s.metadata.requires.any_bins
                        if s.metadata and s.metadata.requires
                        else [],
                        # Serialized as dicts, not names: the cache has to carry
                        # the description and source URL a manifest declared, or
                        # a cache hit would silently downgrade what surfaces can
                        # tell the operator about a missing variable.
                        "requires_env": [e.to_dict() for e in s.metadata.requires.env]
                        if s.metadata and s.metadata.requires
                        else [],
                        "config_vars": [c.to_dict() for c in s.metadata.config_vars]
                        if s.metadata
                        else [],
                        "install": [
                            {
                                "kind": i.kind,
                                "id": i.id,
                                "label": i.label,
                                "bins": i.bins,
                                "os": i.os,
                                "formula": i.formula,
                                "package": i.package,
                                "module": i.module,
                                "url": i.url,
                            }
                            for i in (s.metadata.install if s.metadata else [])
                        ],
                    }
                    if s.metadata
                    else None,
                    "requires_tools": s.requires_tools,
                    "fallback_for_toolsets": s.fallback_for_toolsets,
                }
                for s in skills
            ],
        }
        self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        self._snapshot_path.write_text(json.dumps(data), encoding="utf-8")

    def load_snapshot(
        self, manifest: dict[str, dict[str, float | int]] | None = None
    ) -> list[SkillSpec] | None:
        """Load from snapshot if manifest matches. Returns None on miss.

        ``manifest`` lets a caller that has already stat-ed the layer dirs pass
        the result in rather than pay for a second sweep.
        """
        import json

        if not self._snapshot_path.exists():
            return None
        try:
            data = json.loads(self._snapshot_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        if data.get("version") != _SNAPSHOT_SCHEMA_VERSION:
            return None
        saved_manifest = data.get("manifest", {})
        current_manifest = manifest if manifest is not None else self._build_manifest()
        if saved_manifest != current_manifest:
            return None

        skills = []
        for s in data.get("skills", []):
            # Restore metadata from snapshot
            meta = None
            raw_meta = s.get("metadata")
            if raw_meta:
                from agentos.skills.types import (
                    SkillInstallSpec,
                    SkillPlatformMeta,
                    SkillRequires,
                )

                install_specs = [
                    SkillInstallSpec(
                        kind=i.get("kind", ""),
                        id=i.get("id", ""),
                        label=i.get("label", ""),
                        bins=i.get("bins", []),
                        os=i.get("os", []),
                        formula=i.get("formula", ""),
                        package=i.get("package", ""),
                        module=i.get("module", ""),
                        url=i.get("url", ""),
                    )
                    for i in raw_meta.get("install", [])
                ]
                meta = SkillPlatformMeta(
                    emoji=raw_meta.get("emoji", ""),
                    category=str(raw_meta.get("category", "")).strip().lower(),
                    skill_key=raw_meta.get("skill_key", ""),
                    primary_env=raw_meta.get("primary_env", ""),
                    homepage=raw_meta.get("homepage", ""),
                    always=raw_meta.get("always"),
                    os=raw_meta.get("os", []),
                    requires=SkillRequires(
                        bins=raw_meta.get("requires_bins", []),
                        any_bins=raw_meta.get("requires_any_bins", []),
                        env=raw_meta.get("requires_env", []),
                    ),
                    install=install_specs,
                    risk_level=str(raw_meta.get("risk_level", "")).strip().lower(),
                    capabilities=raw_meta.get("capabilities", []),
                    config_vars=raw_meta.get("config_vars", []),
                )

            layer = SkillLayer(s.get("layer", "bundled"))
            skills.append(
                SkillSpec(
                    name=s["name"],
                    description=s.get("description", ""),
                    layer=layer,
                    always=s.get("always", False),
                    triggers=s.get("triggers", []),
                    content=s.get("content", ""),
                    path=Path(s.get("base_dir", "")),
                    file_path=s.get("file_path", ""),
                    base_dir=s.get("base_dir", ""),
                    user_invocable=s.get("user_invocable", True),
                    disable_model_invocation=s.get("disable_model_invocation", False),
                    homepage=s.get("homepage", ""),
                    metadata=meta,
                    provenance=_snapshot_provenance(s.get("provenance")),
                    # Re-resolved rather than trusted: the cache is a writable
                    # file on disk, so it gets the same allowlist *and* layer
                    # check the manifest got.
                    publisher=resolve_declared_publisher(s.get("publisher"), layer),
                    requires_tools=s.get("requires_tools", []),
                    fallback_for_toolsets=s.get("fallback_for_toolsets", []),
                )
            )
        return skills

    def load_all(self) -> list[SkillSpec]:
        """Load all skills with layer precedence (high overrides low).

        Tries snapshot cache first for fast cold starts.

        The in-memory cache is validated against the on-disk manifest rather
        than trusted outright. ``invalidate_cache()`` is only reached through
        AgentOS's own install/update/remove paths, but the skill directories are
        not exclusively ours: ``agentos skills install`` runs in a separate
        process, and ``.agents/skills`` is shared with every other agent on the
        machine. A skill written by any of them used to stay invisible to a
        running gateway until it restarted, with nothing said about it.
        """
        manifest = self._build_manifest()
        if self._cached is not None and self._cached_manifest == manifest:
            return list(self._cached)

        # Try snapshot cache
        cached = self.load_snapshot(manifest)
        if cached is not None:
            self._cached = cached
            self._cached_manifest = manifest
            return list(cached)

        # Full scan: load in low→high order; higher layers override by name
        merged: dict[str, SkillSpec] = {}
        for dir_path, layer in self._get_layer_dirs():
            if dir_path.exists():
                layer_count = 0
                for skill_dir in sorted(dir_path.iterdir()):
                    if layer_count >= MAX_SKILLS_PER_SOURCE:
                        log.warning(
                            "layer %s has %d+ skills, truncating",
                            layer.value,
                            MAX_SKILLS_PER_SOURCE,
                        )
                        break
                    if skill_dir.is_dir() and not skill_dir.name.startswith("."):
                        spec = self._load_skill(skill_dir, layer, root=dir_path)
                        if spec:
                            merged[spec.name] = spec
                            layer_count += 1

        skills = list(merged.values())
        self._cached = list(skills)
        # Set before save_snapshot(), which calls back into load_all(): with the
        # manifest already recorded that call is a cache hit rather than a
        # second full scan.
        self._cached_manifest = manifest

        # Save snapshot for next cold start
        try:
            self.save_snapshot()
        except OSError:
            pass

        return skills

    def _load_skill(
        self, skill_dir: Path, layer: SkillLayer, root: Path | None = None
    ) -> SkillSpec | None:
        """Load a single skill from its directory."""
        # Symlink containment: reject skills that escape the layer root
        if root is not None:
            try:
                real = skill_dir.resolve()
                if not real.is_relative_to(root.resolve()):
                    log.warning("skill %s escapes root %s, skipping", skill_dir.name, root)
                    return None
            except (OSError, ValueError):
                return None

        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            return None

        # File size guard
        try:
            if skill_file.stat().st_size > MAX_SKILL_FILE_BYTES:
                log.warning(
                    "skill %s exceeds %d bytes, skipping",
                    skill_dir.name,
                    MAX_SKILL_FILE_BYTES,
                )
                return None
        except OSError:
            return None

        try:
            text = _read_skill_md(skill_file)
            frontmatter, body = _parse_frontmatter(text)

            if not frontmatter or "name" not in frontmatter:
                return None

            name = frontmatter["name"]
            description = frontmatter.get("description", "")

            # Simple fields
            always_raw = frontmatter.get("always", False)
            always = bool(always_raw) if always_raw is not None else False

            triggers = frontmatter.get("triggers", [])
            if not isinstance(triggers, list):
                triggers = [str(triggers)]

            # Platform metadata fields
            metadata = _resolve_metadata(frontmatter)
            provenance = _resolve_provenance(frontmatter)
            publisher = _resolve_skill_publisher(frontmatter, layer)
            # metadata.always overrides top-level always if set
            if metadata and metadata.always is not None:
                always = metadata.always

            user_invocable = frontmatter.get("user-invocable", True)
            disable_model_invocation = frontmatter.get(
                "disable-model-invocation",
                False,
            )
            homepage = frontmatter.get("homepage", "")

            # Conditional activation fields
            activation_meta: dict[str, Any] = {}
            raw_meta_dict = frontmatter.get("metadata", {})
            if isinstance(raw_meta_dict, dict):
                raw_activation_meta = raw_meta_dict.get("agentos", {})
                if isinstance(raw_activation_meta, dict):
                    activation_meta = cast(dict[str, Any], raw_activation_meta)
            requires_tools = activation_meta.get("requires_tools", [])
            fallback_for_toolsets = activation_meta.get("fallback_for_toolsets", [])

            return SkillSpec(
                name=name,
                description=description,
                layer=layer,
                always=always,
                triggers=triggers,
                content=body,
                path=skill_dir,
                metadata=metadata,
                provenance=provenance,
                publisher=publisher,
                user_invocable=user_invocable,
                disable_model_invocation=disable_model_invocation,
                homepage=homepage,
                file_path=str(skill_file.resolve()),
                base_dir=str(skill_dir.resolve()),
                requires_tools=requires_tools if isinstance(requires_tools, list) else [],
                fallback_for_toolsets=fallback_for_toolsets
                if isinstance(fallback_for_toolsets, list)
                else [],
            )
        except Exception as exc:
            log.debug("skill.load_failed", dir=str(skill_dir), error=str(exc))
            return None

    def filter_by_tools(self, available_tools: set[str]) -> list[SkillSpec]:
        """Return skills whose requires_tools are all present in available_tools.

        Skills with no requires_tools pass unconditionally.
        """
        result = []
        for s in self.load_all():
            if s.requires_tools and not all(t in available_tools for t in s.requires_tools):
                continue
            result.append(s)
        return result

    def find_by_trigger(self, text: str) -> list[SkillSpec]:
        """Find skills matching triggers in the given text."""
        text_lower = text.lower()
        matches: list[SkillSpec] = []
        for skill in self.load_all():
            for trigger in skill.triggers:
                if trigger.lower() in text_lower:
                    matches.append(skill)
                    break
        return matches

    def get_always_skills(self) -> list[SkillSpec]:
        """Get all skills with always=True."""
        return [skill for skill in self.load_all() if skill.always]

    def get_user_invocable(self) -> list[SkillSpec]:
        """Get all skills that are user-invocable."""
        return [skill for skill in self.load_all() if skill.user_invocable]

    def get_by_name(self, name: str) -> SkillSpec | None:
        """Get a skill by exact name."""
        for skill in self.load_all():
            if skill.name == name:
                return skill
        return None
