"""Skills domain RPC handlers (Tier 3 stubs)."""

from __future__ import annotations

import asyncio
import shutil
import weakref
from pathlib import Path
from typing import Any

from agentos.gateway.access import CONTROL_AND_CHANNEL, CONTROL_AND_NODE
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.skills.availability import SkillAvailability
from agentos.skills.eligibility import (
    EligibilityContext,
    EligibilityReport,
    diagnose_eligibility,
)
from agentos.skills.hub.defaults import (
    build_default_skill_installer,
    get_default_skill_router,
    installed_skill_identifiers,
    installed_skill_names,
)
from agentos.skills.hub.deps import install_deps
from agentos.skills.hub.lockfile import LockEntry, Lockfile, default_lockfile_path
from agentos.skills.inventory import (
    SkillRow,
    acquisition_payload,
    availability_payload,
    build_skill_inventory,
    lock_key_for_skill,
    publisher_payload,
)
from agentos.skills.loader import SkillLoader
from agentos.skills.publishers import resolve_publisher
from agentos.skills.types import SkillAcquisition, SkillPublisher
from agentos.tools.registry import get_default_registry

_d = get_dispatcher()

# Per-(name, install_id) install serialization. WeakValueDictionary prevents
# unbounded growth: once all coroutines release a lock it gets GC'd.
_deps_locks: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def _deps_lock_for(name: str, install_id: str) -> asyncio.Lock:
    key = (name, install_id)
    lock = _deps_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _deps_locks[key] = lock
    return lock


def _get_loader(ctx: RpcContext) -> SkillLoader | None:
    return getattr(ctx, "skill_loader", None)


def _loader_managed_dir(ctx: RpcContext) -> Path | None:
    loader = _get_loader(ctx)
    return getattr(loader, "managed_dir", None) if loader is not None else None


def _available_tool_names(ctx: RpcContext) -> set[str]:
    """Return the tool surface availability is answered against.

    Same resolution order as ``rpc_tools``: the connection's registry when it
    has one, else the process-wide default the gateway registers builtins into.
    A per-turn profile can still narrow this, so the answer is "what this
    install can offer the agent", which is the question an Installed row asks —
    not "what that one turn saw".

    The exception is ``tools.enabled = false``, which narrows every turn to no
    tools at all. Reporting the full registry there would show a
    ``requires_tools`` skill as available on the Skills page while chat withholds
    it — the disagreement this whole change exists to remove.
    """
    tools_cfg = getattr(getattr(ctx, "config", None), "tools", None)
    if tools_cfg is not None and not getattr(tools_cfg, "enabled", True):
        return set()
    registry = getattr(ctx, "tool_registry", None) or get_default_registry()
    return set(registry.list_names())


def _inventory(ctx: RpcContext) -> list[SkillRow]:
    """Build the one row set every skills RPC renders from."""
    loader = _get_loader(ctx)
    if loader is None:
        return []
    return build_skill_inventory(
        loader,
        config=getattr(ctx, "config", None),
        available_tools=_available_tool_names(ctx),
    )


def _status_from_report(report: EligibilityReport) -> str:
    """Map an EligibilityReport to a tri-state status string.

    Wire contract: one of ``"ready" | "needs_setup" | "not_declared"``.
    """
    if not report.eligible:
        return "needs_setup"
    if report.declared:
        return "ready"
    return "not_declared"


def _status_detail(spec: Any, report: EligibilityReport) -> str:
    """Human-readable tooltip detail for the skill status dot/chip."""
    if not report.eligible:
        if report.disabled:
            # Not "Needs setup — disabled": there is no setup to do. The skill
            # was switched off in config, and the fix is a config line, not an
            # install.
            return "Disabled in config"
        if report.wrong_os:
            meta = getattr(spec, "metadata", None)
            os_list = list(meta.os) if meta and meta.os else []
            return f"Needs setup — wrong OS (requires: {', '.join(os_list)})"
        missing = list(report.missing_bins) + list(report.missing_env)
        if missing:
            return f"Needs setup — missing: {', '.join(missing)}"
        return "Needs setup"
    if not report.declared:
        return "Ready — no dependencies declared"
    meta = getattr(spec, "metadata", None)
    requires = meta.requires if meta is not None else None
    if requires is None:
        total = 0
    else:
        total = len(requires.bins) + (1 if requires.any_bins else 0) + len(requires.env)
    return f"Ready — {total}/{total} dependencies satisfied"


def _requirements_item(
    name: str,
    source: str,
    spec: Any | None,
    report: EligibilityReport | None,
) -> dict[str, Any]:
    """Build a compact dependency-readiness row for the Skill dialog."""
    if spec is None or report is None:
        return {
            "name": name,
            "source": source,
            "status": "missing_skill",
            "requires_bins": [],
            "requires_any_bins": [],
            "requires_env": [],
            "missing_bins": [],
            "missing_env": [],
        }

    meta = getattr(spec, "metadata", None)
    requires = meta.requires if meta is not None else None
    return {
        "name": name,
        "source": source,
        "status": _status_from_report(report),
        "requires_bins": list(requires.bins) if requires else [],
        "requires_any_bins": list(requires.any_bins) if requires else [],
        # env_names, not env: the latter is a list of SkillEnvVar dataclasses, which the
        # UI would render as "[object Object]". Only the names cross the wire here.
        "requires_env": list(requires.env_names) if requires else [],
        "missing_bins": list(report.missing_bins),
        "missing_env": list(report.missing_env),
    }


def _requirements_summary(items: list[dict[str, Any]]) -> str:
    if not items:
        return "not_declared"
    statuses = {str(item.get("status", "")) for item in items}
    if "needs_setup" in statuses or "missing_skill" in statuses:
        return "needs_setup"
    if "ready" in statuses:
        return "ready"
    return "not_declared"


def _requirements_payload(
    spec: Any,
    report: EligibilityReport,
    sub_skills: list[str],
    *,
    skill_index: dict[str, Any] | None = None,
    eligibility_ctx: EligibilityContext | None = None,
) -> dict[str, Any]:
    """Return the current skill's declared requirements."""
    items: list[dict[str, Any]] = []
    if report.declared:
        items.append(_requirements_item(spec.name, "self", spec, report))

    return {"summary": _requirements_summary(items), "items": items}


def _skill_to_dict(
    spec: Any,
    report: EligibilityReport,
    os_name: str = "",
    *,
    skill_index: dict[str, Any] | None = None,
    eligibility_ctx: EligibilityContext | None = None,
    acquisition: SkillAcquisition | None = None,
    publisher: SkillPublisher | None = None,
    availability: SkillAvailability | None = None,
) -> dict[str, Any]:
    """Convert a SkillSpec to a dict with eligibility diagnostics.

    Install options are filtered against ``os_name`` before serialization.
    An install entry is kept when its ``os`` list is empty (treated as
    "any OS") or contains the current ``os_name``. This applies the two-layer
    OS filter (skill-level ``metadata.os`` + per-install ``os``), and keeps the
    wire payload narrow (no ``os`` field per entry).
    Passing an empty ``os_name`` disables per-entry filtering (backward compat).

    ``acquisition``/``publisher``/``availability`` come from
    :func:`~agentos.skills.inventory.build_skill_inventory`. The first two have
    empty defaults, so they are always on the wire. ``availability`` has no
    honest default — it needs a tool surface — so the key is omitted when it was
    not computed rather than carrying a made-up verdict. Every RPC handler here
    passes one, so it is always present on a real row.
    """
    meta = getattr(spec, "metadata", None)
    install_entries: list[dict[str, Any]] = []
    if meta is not None:
        for ispec in meta.install:
            spec_os = list(getattr(ispec, "os", []) or [])
            if spec_os and os_name and os_name not in spec_os:
                continue
            install_entries.append(
                {
                    "id": ispec.id,
                    "kind": ispec.kind,
                    "label": ispec.label,
                    "bins": list(ispec.bins),
                }
            )

    d: dict[str, Any] = {
        "name": spec.name,
        "description": spec.description,
        "layer": str(spec.layer),
        "always": spec.always,
        "triggers": spec.triggers,
        "eligible": report.eligible,
        "emoji": meta.emoji if meta else "",
        "category": meta.category if meta else "",
        "primary_env": meta.primary_env if meta else "",
        "homepage": meta.homepage if meta else getattr(spec, "homepage", ""),
        "file_path": getattr(spec, "file_path", ""),
        "os": list(meta.os) if meta else [],
        "disabled": report.disabled,
        "install": install_entries,
        "requirements": _requirements_payload(
            spec,
            report,
            [],
            skill_index=skill_index,
            eligibility_ctx=eligibility_ctx,
        ),
    }
    provenance = getattr(spec, "provenance", None)
    d["provenance"] = {
        "origin": provenance.origin if provenance else "unknown",
        "license": provenance.license if provenance else "unknown",
        "upstream_url": provenance.upstream_url if provenance else "",
        "maintained_by": provenance.maintained_by if provenance else "AgentOS",
    }
    d["publisher"] = publisher_payload(publisher)
    d["acquisition"] = acquisition_payload(acquisition)
    if availability is not None:
        d["availability"] = availability_payload(availability)
    d["declared"] = report.declared
    d["status"] = _status_from_report(report)
    d["status_detail"] = _status_detail(spec, report)
    if not report.eligible:
        d["reasons"] = report.reasons
        d["missing_bins"] = report.missing_bins
        d["missing_env"] = report.missing_env
        # Names alone tell an operator that something is missing but not what
        # it is or where to get it. The detail form carries whatever the
        # manifest declared so a UI can offer a real fix next to the name,
        # the way it already does for a missing binary.
        d["missing_env_detail"] = [e.to_dict() for e in report.missing_env_detail]
    return d


def _row_to_dict(
    row: SkillRow,
    os_name: str,
    skill_index: dict[str, Any],
    eligibility_ctx: EligibilityContext | None,
) -> dict[str, Any]:
    """Render one inventory row as the wire payload."""
    return _skill_to_dict(
        row.spec,
        row.eligibility,
        os_name,
        skill_index=skill_index,
        eligibility_ctx=eligibility_ctx,
        acquisition=row.acquisition,
        publisher=row.publisher,
        availability=row.availability,
    )


def _rows_payload(rows: list[SkillRow], *, index_from: list[SkillRow] | None = None) -> list[dict]:
    """Render rows, resolving cross-skill lookups against ``index_from``.

    ``skills.list`` hides non-user-invocable skills but must still index every
    loaded skill, so the rendered set and the index are separate arguments.
    """
    ctx_eligible = EligibilityContext.auto()
    skill_index = {row.spec.name: row.spec for row in (index_from if index_from else rows)}
    return [_row_to_dict(row, ctx_eligible.os_name, skill_index, ctx_eligible) for row in rows]


@_d.method("skills.status")
async def _handle_skills_status(params: dict | None, ctx: RpcContext) -> list[dict[str, Any]]:
    """Return all skills with their eligibility status.

    A published alias of ``skills.list`` with no known caller. It is kept
    because removing a published method is a breaking change, but it shares the
    one row builder so it can no longer drift into a second answer.
    """
    return _rows_payload(_inventory(ctx))


@_d.method("skills.list", CONTROL_AND_CHANNEL)
async def _handle_skills_list(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """List installed skills."""
    all_rows = _inventory(ctx)
    rows = [row for row in all_rows if row.spec.user_invocable]
    return {"skills": _rows_payload(rows, index_from=all_rows)}


@_d.method("skills.bins", CONTROL_AND_NODE)
async def _handle_skills_bins(params: dict | None, ctx: RpcContext) -> dict[str, bool]:
    """Return the availability status of required bins across all skills."""
    loader = _get_loader(ctx)
    if loader is None:
        return {}

    bins_status: dict[str, bool] = {}
    skills = loader.load_all()

    for skill in skills:
        if skill.metadata and skill.metadata.requires:
            for bin_name in skill.metadata.requires.bins:
                if bin_name not in bins_status:
                    bins_status[bin_name] = shutil.which(bin_name) is not None
            for bin_name in skill.metadata.requires.any_bins:
                if bin_name not in bins_status:
                    bins_status[bin_name] = shutil.which(bin_name) is not None

    return bins_status


@_d.method("skills.get")
async def _handle_skills_get(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Get a single skill by name, including its full content."""
    if not isinstance(params, dict) or "name" not in params:
        raise ValueError("params.name is required")

    loader = _get_loader(ctx)
    if loader is None:
        raise KeyError("No skill loader available")

    all_rows = _inventory(ctx)
    row = next((item for item in all_rows if item.spec.name == params["name"]), None)
    if row is None:
        raise KeyError(f"Skill not found: {params['name']}")

    result = _rows_payload([row], index_from=all_rows)[0]
    result["content"] = row.spec.content
    result["file_path"] = row.spec.file_path
    result["base_dir"] = row.spec.base_dir
    return result


def _installed_names() -> set[str]:
    """Return the set of skill names currently recorded in the lockfile.

    Lockfile is the authoritative "installed via Community source" record —
    bundled or workspace skills with colliding names won't be mis-flagged
    as installed-from-ClawHub. Missing/corrupt lockfile returns an empty
    set (treat everything as not-yet-installed).
    """
    return installed_skill_names()


def _lock_key(ctx: RpcContext, name: str) -> str:
    """Translate a skill's displayed name into the key its lockfile entry uses.

    Both actions below address the installer, which is keyed by the install
    directory, while every surface addresses a skill by the name its ``SKILL.md``
    declares. Those differ whenever a published skill's manifest names itself
    something other than its directory — ``ytdlp-transcript`` ships a manifest
    named ``youtube-transcript`` — and without this translation Remove and
    Update reported "not found" for an install sitting right there on disk.

    The name is returned unchanged when no skill loads under it, so cleaning up
    a stale lockfile entry by its own key still works.
    """
    loader = _get_loader(ctx)
    spec = loader.get_by_name(name) if loader is not None else None
    if spec is None:
        return name
    return lock_key_for_skill(spec, Lockfile.load(default_lockfile_path()))


def _installed_lock_entries() -> dict[str, LockEntry]:
    """Return the lockfile's installed entries, keyed by installed skill name."""
    return Lockfile.load(default_lockfile_path()).installed


def _synthesized_installed_rows(
    results: list[Any],
    *,
    source_id: str | None,
    query: str,
    loader: SkillLoader | None,
) -> list[dict[str, Any]]:
    """Rows for lockfile installs the catalog did not return.

    A browse (empty query) hits a source's catalog, and a catalog is free not to
    list something the user already installed — a GitHub install by URL is never
    in any catalog, and Bankr's browse omits rows it has retired. The Community
    tab then shows no trace of a skill the user installed minutes ago, which
    reads as the install having been lost. The lockfile already knows every
    field a card needs, so this is a local join, not another network call.

    Synthesized rows are appended: they carry no relevance score and no catalog
    metadata, so putting them first would push richer rows off the top of the
    grid, and they are the least urgent thing on the page — they are already
    installed.
    """
    seen_identifiers = {r.identifier for r in results if getattr(r, "identifier", "")}
    seen_names = {r.name for r in results if getattr(r, "name", "")}
    needle = query.strip().lower()

    rows: list[dict[str, Any]] = []
    for name, entry in sorted(_installed_lock_entries().items()):
        # Browsing one source must not surface another source's installs.
        if source_id is not None and entry.source != source_id:
            continue
        if name in seen_names or (entry.identifier and entry.identifier in seen_identifiers):
            continue

        spec = loader.get_by_name(name) if loader is not None else None
        description = getattr(spec, "description", "") if spec is not None else ""
        if needle and needle not in name.lower() and needle not in description.lower():
            continue

        meta = getattr(spec, "metadata", None)
        # The lockfile's ``publisher_name`` is the raw string a catalog claimed;
        # only the allowlisted record is ever rendered as a brand.
        publisher = resolve_publisher(entry.publisher_id)
        rows.append(
            {
                "name": name,
                "description": description,
                "version": entry.version,
                "author": "",
                "source": entry.source,
                "trust_level": entry.source_trust,
                "identifier": entry.identifier,
                "provider": publisher.name,
                "logo": publisher.logo,
                "emoji": meta.emoji if meta else "",
                # No catalog metadata means no category. Left empty so the row
                # joins the existing "other" bucket the browse UI already has,
                # rather than inventing a chip that would appear on catalogs
                # which currently show no chips at all.
                "category": "",
                "setup": [],
                "demo": {},
                "homepage": entry.upstream_url,
                "installed": True,
            }
        )
    return rows


@_d.method("skills.search")
async def _handle_skills_search(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Search for skills across Community sources."""
    if not isinstance(params, dict) or "query" not in params:
        raise ValueError("params.query is required")

    router = getattr(ctx, "_skill_router", None)
    if router is None:
        router = _get_default_router()
    if router is None:
        return {"results": [], "message": "No skill sources configured"}

    query = params["query"]
    try:
        # The browse gallery requests whole catalogs (Bankr alone is ~100
        # skills), so the cap must comfortably exceed catalog sizes — a cap
        # sized for paged search results silently truncates browse.
        limit = min(int(params.get("limit", 20)), 500)
    except (TypeError, ValueError):
        limit = 20
    source_id = params.get("source")
    if source_id is not None and not isinstance(source_id, str):
        source_id = None
    results = await router.search(query, limit=limit, source_id=source_id)
    # Match a browse result to a lockfile install by name against names and by
    # identifier against identifiers — never across the two. Lockfile keys are
    # the installer's name (SKILL.md frontmatter), which may be neither the
    # ``displayName`` a source returns as ``SkillMeta.name`` nor the catalog
    # slug — e.g. Bankr's ``bankr-token-scam-analysis`` slug installs under the
    # name ``token-scam-analysis``. The identifier (the source URL) is the
    # reliable join key across a reload; comparing a name against the pooled
    # union of both would flag a catalog row whose name happens to equal some
    # other skill's identifier.
    installed_names = _installed_names()
    installed_identifiers = installed_skill_identifiers()
    rows = [
        {
            "name": r.name,
            "description": r.description,
            "version": r.version,
            "author": r.author,
            "source": r.source_id,
            "trust_level": r.trust_level,
            "identifier": r.identifier,
            "provider": r.provider,
            "logo": r.logo,
            "emoji": r.emoji,
            "category": r.category,
            "setup": r.setup,
            "demo": r.demo,
            "homepage": r.homepage,
            "installed": (bool(r.identifier) and r.identifier in installed_identifiers)
            or r.name in installed_names,
        }
        for r in results
    ]
    rows.extend(
        _synthesized_installed_rows(
            results,
            source_id=source_id,
            query=query if isinstance(query, str) else "",
            loader=_get_loader(ctx),
        )
    )
    return {"results": rows}


def _invalidate_loader(ctx: RpcContext) -> None:
    """Drop the loader's in-memory cache so the next read re-scans disk.

    The disk snapshot has its own mtime/size manifest check, but the
    in-memory ``_cached`` field is populated at boot and would otherwise
    mask newly-installed (or removed) managed skills until the next
    restart.
    """
    loader = _get_loader(ctx)
    if loader is not None:
        loader.invalidate_cache()


@_d.method("skills.install")
async def _handle_skills_install(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Install a skill from a Community source."""
    if not isinstance(params, dict) or "identifier" not in params:
        raise ValueError("params.identifier is required")
    loader = _get_loader(ctx)
    if loader is None:
        return {"success": False, "message": "No skill loader configured"}

    installer = _get_default_installer(managed_dir=loader.managed_dir)
    if installer is None:
        return {"success": False, "message": "No skill installer configured"}

    identifier = params["identifier"]
    source_id = params.get("source", "clawhub")
    force = params.get("force", False)
    result = await installer.install(identifier, source_id, force=force)
    if result.success:
        _invalidate_loader(ctx)
    resp: dict[str, Any] = {
        "success": result.success,
        "name": result.name,
        "message": result.message,
    }
    if result.path:
        resp["path"] = result.path
    if result.scan:
        resp["scan_verdict"] = result.scan.verdict
        resp["scan_findings"] = [finding.__dict__ for finding in result.scan.findings]
    return resp


@_d.method("skills.update")
async def _handle_skills_update(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Update installed skills from lockfile."""
    loader = _get_loader(ctx)
    if loader is None:
        return {
            "results": [],
            "success": False,
            "message": "No skill loader configured",
        }
    installer = _get_default_installer(managed_dir=loader.managed_dir)
    if installer is None:
        return {"success": False, "message": "No skill installer configured"}

    name = (params or {}).get("name")
    try:
        results = await installer.update(_lock_key(ctx, name) if name else None)
    except OSError as exc:
        return {
            "results": [],
            "success": False,
            "message": f"Skill update unavailable: {exc}",
        }
    if any(r.success for r in results):
        _invalidate_loader(ctx)
    result_list = []
    for r in results:
        item: dict[str, Any] = {
            "success": r.success,
            "name": r.name,
            "message": r.message,
        }
        if r.scan:
            item["scan_verdict"] = r.scan.verdict
            item["scan_findings"] = [finding.__dict__ for finding in r.scan.findings]
        result_list.append(item)
    return {"results": result_list}


@_d.method("skills.uninstall")
async def _handle_skills_uninstall(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Uninstall a managed skill."""
    if not isinstance(params, dict) or "name" not in params:
        raise ValueError("params.name is required")

    installer = _get_default_installer(managed_dir=_loader_managed_dir(ctx))
    if installer is None:
        return {"success": False, "message": "No skill installer configured"}

    result = await installer.uninstall(_lock_key(ctx, params["name"]))
    if result.success:
        _invalidate_loader(ctx)
    return {"success": result.success, "name": result.name, "message": result.message}


@_d.method("skills.deps.install")
async def _handle_skills_deps_install(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Install runtime dependencies for an already-loaded skill.

    Looks up the skill by name, finds the matching SkillInstallSpec by id in
    `metadata.install`, runs it via `install_deps`, then re-runs
    `diagnose_eligibility` and returns `missing_still` reflecting post-install state.

    Note: `kind == "download"` is non-idempotent — re-running re-downloads.
    Callers should consult `missing_still` before retrying.
    """
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")
    if "name" not in params:
        raise ValueError("params.name is required")
    if "install_id" not in params:
        raise ValueError("params.install_id is required")

    name = params["name"]
    install_id = params["install_id"]
    loader = _get_loader(ctx)
    if loader is None:
        raise KeyError("No skill loader available")
    skill = loader.get_by_name(name)
    if skill is None:
        raise KeyError(f"Skill not found: {name}")

    specs = skill.metadata.install if skill.metadata else []
    spec = next((s for s in specs if s.id == install_id), None)
    if spec is None:
        raise KeyError(f"Install spec not found: {install_id}")

    ctx_eligible = EligibilityContext.auto()
    if spec.os and ctx_eligible.os_name and ctx_eligible.os_name not in spec.os:
        raise ValueError(
            f"Install spec {install_id!r} not supported on "
            f"{ctx_eligible.os_name} (requires: {', '.join(spec.os)})"
        )

    async with _deps_lock_for(name, install_id):
        results = await install_deps([spec])
        r = results[0]
        report = diagnose_eligibility(skill, ctx_eligible)

    return {
        "success": r.success,
        "kind": r.kind,
        "message": r.message,
        "missing_still": {
            "bins": list(report.missing_bins),
            "env": list(report.missing_env),
        },
    }


# ---------------------------------------------------------------------------
# Default router/installer (lazy init)
# ---------------------------------------------------------------------------


def _get_default_router():
    return get_default_skill_router()


def _get_default_installer(*, managed_dir=None):
    return build_default_skill_installer(managed_dir=managed_dir)
