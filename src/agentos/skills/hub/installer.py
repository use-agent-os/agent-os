"""Skill installer — fetch → quarantine → scan → policy → install → lockfile."""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import structlog

from agentos.paths import default_agentos_home
from agentos.skills.hub.install_policy import (
    InstallPolicy,
    PolicyDecision,
    build_policy_request,
    evaluate_install_policy,
)
from agentos.skills.hub.lockfile import (
    LockEntry,
    Lockfile,
    compute_sha256,
    default_lockfile_path,
)
from agentos.skills.hub.router import SourceRouter
from agentos.skills.hub.scanner import ScanResult, scan_skill_bundle
from agentos.skills.hub.source import SkillMeta
from agentos.skills.loader import _parse_frontmatter
from agentos.skills.paths import default_bundled_skills_dir, default_managed_skills_dir

log = structlog.get_logger(__name__)

# Path traversal protection: only allow safe skill names
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")

_SLUG_SEPARATORS_RE = re.compile(r"[^a-z0-9]+")


def _default_managed_dir() -> Path:
    return default_managed_skills_dir()


def _default_quarantine_dir() -> Path:
    return default_agentos_home() / "quarantine"


def _default_lockfile() -> Path:
    return default_lockfile_path()


def bundled_skill_names() -> set[str]:
    """Names of the skills that ship with AgentOS.

    Read off disk rather than through :class:`~agentos.skills.loader.SkillLoader`
    so the installer stays usable from the CLI, where no loader has been built.
    Only directory names are needed — a shadow is decided by name, and the
    loader resolves layers by name too.
    """
    try:
        bundled = default_bundled_skills_dir()
        return {
            path.name
            for path in bundled.iterdir()
            if path.is_dir() and (path / "SKILL.md").is_file()
        }
    except OSError:  # pragma: no cover — a missing bundled dir must not block installs
        log.debug("installer.bundled_names_unavailable", exc_info=True)
        return set()


def _publisher_slug(meta: SkillMeta | None, source_id: str) -> str:
    """Return the publisher slug to record for a hub install.

    A hub-installed skill has no ``publisher:`` block in its frontmatter — the
    catalog row that installed it is the only thing that knew the brand. Prefer
    the row's ``provider`` and fall back to the source itself, so a Bankr skill
    with no declared provider still shows as published by Bankr.

    This is only a *selector*; the allowlist decides whether it means anything.
    """
    raw = (meta.provider if meta else "") or source_id
    return _SLUG_SEPARATORS_RE.sub("-", raw.strip().lower()).strip("-")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


@dataclass
class InstallResult:
    """Result of a skill installation."""

    success: bool
    name: str = ""
    message: str = ""
    scan: ScanResult | None = None
    path: str = ""
    sha256: str = ""
    #: The registry's own review of the skill, ``""`` when it published none.
    registry_verdict: str = ""
    #: The ``[skills.install_policy]`` answer, ``None`` when no policy ran.
    policy: PolicyDecision | None = None
    #: A refusal that ``force`` would have let through. False for the ones
    #: nothing overrides: a registry ``malicious`` verdict, a policy ``block``.
    overridable: bool = False


def install_security_fields(result: InstallResult) -> dict[str, Any]:
    """The security half of an install result, as RPC, CLI and tool report it."""
    fields: dict[str, Any] = {}
    if result.scan:
        fields["scan_verdict"] = result.scan.verdict
        fields["scan_findings"] = [asdict(finding) for finding in result.scan.findings]
    if result.registry_verdict:
        fields["registry_verdict"] = result.registry_verdict
    if result.policy is not None:
        fields["policy_decision"] = result.policy.decision
        fields["policy_reason"] = result.policy.reason
        fields["policy_findings"] = result.policy.findings
    if result.overridable:
        fields["overridable"] = True
    return fields


class SkillInstaller:
    """Manages the full skill install/uninstall lifecycle."""

    def __init__(
        self,
        router: SourceRouter,
        managed_dir: Path | None = None,
        quarantine_dir: Path | None = None,
        lockfile_path: Path | None = None,
        install_policy: InstallPolicy | None = None,
    ) -> None:
        self._router = router
        self._install_policy = install_policy
        self._managed_dir = managed_dir if managed_dir is not None else _default_managed_dir()
        self._quarantine_dir = (
            quarantine_dir if quarantine_dir is not None else _default_quarantine_dir()
        )
        self._lockfile_path = lockfile_path if lockfile_path is not None else _default_lockfile()

    async def install(
        self,
        identifier: str,
        source_id: str,
        force: bool = False,
        *,
        mode: str = "install",
    ) -> InstallResult:
        """Full install lifecycle: fetch → quarantine → scan → install → lockfile."""
        # 1. Fetch
        bundle = await self._router.fetch(identifier, source_id)
        if bundle is None:
            return InstallResult(
                success=False,
                message=(
                    f"Failed to fetch '{identifier}' from {source_id}. "
                    "The skill may not exist or the source is rate-limited. "
                    "Try again later."
                ),
            )

        name = bundle.name
        if not _SAFE_NAME_RE.match(name):
            return InstallResult(success=False, name=name, message=f"Invalid skill name: {name}")

        # A managed install outranks a bundled one (SkillLayer precedence), so a
        # same-named community skill replaces the shipped one for every session
        # without touching it on disk — nothing in the old flow said so. Refuse
        # the *first* such install; a reinstall or update of an already-shadowing
        # skill is not a new decision and passes through.
        if not force and name in bundled_skill_names() and not (self._managed_dir / name).is_dir():
            return InstallResult(
                success=False,
                name=name,
                message=(
                    f"'{name}' already ships with AgentOS. Installing this one writes to "
                    f"the managed layer, which outranks bundled, so it would silently "
                    f"replace the built-in skill everywhere. If the built-in shows as "
                    f"unavailable it is missing a binary or an environment variable, not "
                    f"an install — run 'agentos skills list' to see which. Re-run with "
                    f"force once the user has confirmed they want this one instead."
                ),
            )

        skill_md = bundle.skill_md
        if not skill_md:
            return InstallResult(success=False, name=name, message="Bundle has no SKILL.md")

        # The loader skips a skill it cannot read, so installing one would report
        # success and leave nothing for the user or the agent to find.
        if "name" not in _parse_frontmatter(skill_md)[0]:
            return InstallResult(
                success=False,
                name=name,
                message=(
                    f"'{name}' cannot be loaded: its SKILL.md has no readable frontmatter "
                    f"with a name. This needs fixing by the skill's publisher."
                ),
            )

        bundle_meta = bundle.meta
        if bundle_meta is None:
            inspect = getattr(self._router, "inspect", None)
            if inspect is not None:
                try:
                    bundle_meta = await inspect(identifier, source_id)
                except Exception:  # pragma: no cover - source adapters are best-effort here
                    bundle_meta = None

        # The registry has already reviewed this skill; its answer costs
        # nothing to honour and is better informed than the regex scan below.
        # ``malicious`` is the registry saying it found malware, so no flag
        # overrides it — ``force`` is one argument away for a model holding
        # ``skill_install_community``.
        registry_verdict = bundle_meta.registry_verdict if bundle_meta else ""
        if registry_verdict == "malicious" or (registry_verdict == "suspicious" and not force):
            summary = bundle_meta.registry_summary if bundle_meta else ""
            detail = f" ({summary})" if summary else ""
            overridable = registry_verdict == "suspicious"
            return InstallResult(
                success=False,
                name=name,
                message=(
                    f"{source_id} flags '{name}' as {registry_verdict}{detail}. "
                    + (
                        "Re-run with force only after the user has reviewed the skill "
                        "and confirmed."
                        if overridable
                        else "This cannot be overridden."
                    )
                ),
                registry_verdict=registry_verdict,
                overridable=overridable,
            )

        # 2. Quarantine — write to temp dir with Zip Slip protection
        q_dir = self._quarantine_dir / name
        if q_dir.exists():
            shutil.rmtree(q_dir)
        q_dir.mkdir(parents=True, exist_ok=True)
        q_dir_resolved = q_dir.resolve()
        for rel_path, content in bundle.files.items():
            file_path = (q_dir / rel_path).resolve()
            if not _is_relative_to(file_path, q_dir_resolved):
                log.warning("installer.zip_slip_blocked", rel_path=rel_path)
                continue
            file_path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                file_path.write_bytes(content)
            else:
                file_path.write_text(content, encoding="utf-8")

        # 3. Security scan
        scan_result = scan_skill_bundle(bundle.files)
        if scan_result.verdict == "dangerous" and not force:
            shutil.rmtree(q_dir, ignore_errors=True)
            return InstallResult(
                success=False,
                name=name,
                message=(
                    f"Security scan: {scan_result.verdict} "
                    f"({len(scan_result.findings)} findings). "
                    "Use force=True to override."
                ),
                scan=scan_result,
                registry_verdict=registry_verdict,
                overridable=True,
            )

        # 3b. Operator install policy — an external command reviews the staged
        # bundle. Runs under ``force`` too: ``force`` answers a ``warn``, and
        # nothing answers a ``block``.
        policy_decision: PolicyDecision | None = None
        if self._install_policy is not None and self._install_policy.enabled:
            policy_decision = await evaluate_install_policy(
                self._install_policy,
                build_policy_request(
                    name=name,
                    source_path=str(q_dir_resolved),
                    source_id=source_id,
                    identifier=identifier,
                    version=bundle_meta.version if bundle_meta else "",
                    mode=mode,
                ),
            )
            blocked = policy_decision.decision == "block"
            if blocked or (policy_decision.decision == "warn" and not force):
                shutil.rmtree(q_dir, ignore_errors=True)
                log.warning(
                    "skill.install_policy_refused",
                    name=name,
                    source=source_id,
                    decision=policy_decision.decision,
                )
                return InstallResult(
                    success=False,
                    name=name,
                    message=(
                        f"Install policy: {policy_decision.decision} — "
                        f"{policy_decision.reason.rstrip('.')}. "
                        + (
                            "This cannot be overridden."
                            if blocked
                            else "Re-run with force only after the user has confirmed."
                        )
                    ),
                    scan=scan_result,
                    registry_verdict=registry_verdict,
                    policy=policy_decision,
                    overridable=not blocked,
                )

        # 4. Install — move from quarantine to managed dir
        install_dir = self._managed_dir / name
        if install_dir.exists():
            if not _is_relative_to(install_dir, self._managed_dir):
                shutil.rmtree(q_dir, ignore_errors=True)
                return InstallResult(
                    success=False,
                    name=name,
                    message=f"Existing install path escapes managed dir: {name}",
                    scan=scan_result,
                )
            shutil.rmtree(install_dir)
        self._managed_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(q_dir), str(install_dir))

        # 5. Update lockfile
        sha = compute_sha256(install_dir)
        new_entry = LockEntry(
            source=source_id,
            identifier=identifier,
            # The field has existed since the lockfile did and was never
            # written, so every install reported an empty version — now
            # that `acquisition.version` is on the wire, that is visible.
            version=bundle_meta.version if bundle_meta else "",
            installed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            path=str(install_dir),
            sha256=sha,
            license=bundle_meta.license if bundle_meta else "",
            upstream_url=bundle_meta.homepage if bundle_meta else "",
            publisher_id=_publisher_slug(bundle_meta, source_id),
            # The row's author credit when it has one, else the brand it
            # named. Both are untrusted free text and neither is resolved as
            # identity — only ``publisher_id`` is (see
            # ``agentos.skills.publishers``) — so recording the more
            # specific of the two costs nothing and keeps the human who
            # wrote a brand-distributed skill visible after install.
            publisher_name=(bundle_meta.author or bundle_meta.provider) if bundle_meta else "",
            source_trust=bundle_meta.trust_level if bundle_meta else "",
            scan_verdict=scan_result.verdict,
            scan_strategy=scan_result.strategy,
            scan_findings=[asdict(finding) for finding in scan_result.findings],
            registry_verdict=registry_verdict,
            registry_summary=bundle_meta.registry_summary if bundle_meta else "",
            upstream_scanners=dict(bundle_meta.upstream_scanners) if bundle_meta else {},
            policy_decision=policy_decision.decision if policy_decision else "",
            policy_reason=policy_decision.reason if policy_decision else "",
        )
        Lockfile.update(self._lockfile_path, lambda lockfile: lockfile.add(name, new_entry))

        log.info("skill.installed", name=name, source=source_id, verdict=scan_result.verdict)
        return InstallResult(
            success=True,
            name=name,
            message=f"Installed '{name}' from {source_id}",
            scan=scan_result,
            path=str(install_dir),
            sha256=sha,
            registry_verdict=registry_verdict,
            policy=policy_decision,
        )

    async def uninstall(self, name: str) -> InstallResult:
        """Remove an installed skill and its lockfile entry."""
        if not _SAFE_NAME_RE.match(name):
            return InstallResult(success=False, name=name, message=f"Invalid skill name: {name}")

        # Remove from disk (only within managed dir)
        install_dir = (self._managed_dir / name).resolve()
        managed_root = self._managed_dir.resolve()
        if install_dir.exists() and _is_relative_to(install_dir, managed_root):
            shutil.rmtree(install_dir)

        # Remove from lockfile
        removed = Lockfile.update(self._lockfile_path, lambda lockfile: lockfile.remove(name))

        if not install_dir.exists() and not removed:
            return InstallResult(success=False, name=name, message=f"Skill '{name}' not found")

        log.info("skill.uninstalled", name=name)
        return InstallResult(success=True, name=name, message=f"Uninstalled '{name}'")

    async def update(self, name: str | None = None) -> list[InstallResult]:
        """Re-install skills from lockfile (re-fetches the latest source code).

        If ``name`` is None, update all. The message distinguishes a genuine
        update from a no-op by comparing the content hash before and after: the
        source identifier tracks a branch (e.g. ``.../tree/main/bankr``), so a
        re-fetch pulls whatever the branch tip is now.
        """
        lockfile = Lockfile.load(self._lockfile_path)
        results = []
        entries = {name: lockfile.get(name)} if name else lockfile.installed
        for skill_name, entry in entries.items():
            if entry is None:
                results.append(
                    InstallResult(success=False, name=skill_name, message="Not in lockfile")
                )
                continue
            old_sha = entry.sha256
            result = await self.install(entry.identifier, entry.source, force=False, mode="update")
            if result.success:
                if old_sha and result.sha256 == old_sha:
                    result.message = f"'{result.name}' is already up to date"
                else:
                    result.message = f"Updated '{result.name}' to the latest version"
            results.append(result)
        return results
