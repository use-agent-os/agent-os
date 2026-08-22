"""Eligibility filtering — checks if a skill is usable in the current environment."""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass, field

from agentos.skills.install_kinds import normalize_install_kind, render_install_command
from agentos.skills.types import SkillEnvVar, SkillInstallSpec, SkillSpec


@dataclass
class EligibilityContext:
    """Environment context for eligibility checks."""

    os_name: str = ""
    has_bin_cache: dict[str, bool] = field(default_factory=dict)
    env_cache: dict[str, str | None] = field(default_factory=dict)
    enabled_set: set[str] | None = None  # None = all enabled
    disabled_set: set[str] = field(default_factory=set)

    @staticmethod
    def auto(
        enabled_set: set[str] | None = None,
        disabled_set: set[str] | None = None,
    ) -> EligibilityContext:
        """Build context from the current environment."""
        return EligibilityContext(
            os_name=platform.system().lower(),
            enabled_set=enabled_set,
            disabled_set=disabled_set or set(),
        )


def _has_bin(name: str, ctx: EligibilityContext) -> bool:
    if name in ctx.has_bin_cache:
        return ctx.has_bin_cache[name]
    result = shutil.which(name) is not None
    ctx.has_bin_cache[name] = result
    return result


def _has_env(name: str, ctx: EligibilityContext) -> bool:
    """Return whether a required environment variable is usably set.

    An empty or whitespace-only value counts as missing. Declaring a variable
    under ``requires.env`` says the skill cannot run without it — an API key, a
    token, a path — and none of those work when blank. Accepting a bare
    ``export FOO=`` reported the skill as Ready right up until it failed at
    runtime, which is the one place the check exists to prevent.
    """
    if name in ctx.env_cache:
        return bool((ctx.env_cache[name] or "").strip())
    val = os.environ.get(name)
    ctx.env_cache[name] = val
    return bool((val or "").strip())


def check_eligibility(spec: SkillSpec, ctx: EligibilityContext) -> bool:
    """Check if a skill is eligible in the current environment.

    Returns False if any hard requirement is not met.
    """
    # 1. Explicitly disabled
    if spec.name in ctx.disabled_set:
        return False

    # 2. Explicitly enabled (whitelist mode)
    if ctx.enabled_set is not None and spec.name not in ctx.enabled_set:
        return False

    meta = spec.metadata
    if meta is None:
        return True  # No requirements → always eligible

    # 3. OS check
    if meta.os and ctx.os_name and ctx.os_name not in meta.os:
        return False

    # 4. Required bins (all must exist)
    if meta.requires:
        for b in meta.requires.bins:
            if not _has_bin(b, ctx):
                return False

        # 5. anyBins (at least one must exist)
        if meta.requires.any_bins:
            if not any(_has_bin(b, ctx) for b in meta.requires.any_bins):
                return False

        # 6. Required env vars
        for name in meta.requires.env_names:
            if not _has_env(name, ctx):
                return False

    return True


# ---------------------------------------------------------------------------
# Diagnostic report — detailed "why ineligible" + install hints
# ---------------------------------------------------------------------------


@dataclass
class InstallHint:
    """Display-only install command, decoupled from dependency execution logic."""

    #: Canonical kind — see :data:`agentos.skills.install_kinds.INSTALL_KINDS`.
    kind: str
    label: str  # "Install himalaya (brew)"
    command: str  # "brew install himalaya"


@dataclass
class EligibilityReport:
    """Structured diagnosis of why a skill is or isn't eligible."""

    eligible: bool
    reasons: list[str] = field(default_factory=list)
    missing_bins: list[str] = field(default_factory=list)
    missing_env: list[str] = field(default_factory=list)
    #: The same missing variables as :attr:`missing_env`, carrying whatever the
    #: manifest declared about them. Surfaces use this to explain what a
    #: variable is for and where to obtain it, instead of printing a bare name
    #: the operator has to go research. ``missing_env`` stays a plain list of
    #: names so existing callers keep working.
    missing_env_detail: list[SkillEnvVar] = field(default_factory=list)
    install_hints: list[InstallHint] = field(default_factory=list)
    disabled: bool = False
    wrong_os: bool = False
    declared: bool = False


def _is_declared(spec: SkillSpec) -> bool:
    """Return True when the skill's frontmatter declares runtime requirements.

    Frontmatter with only ``metadata.emoji`` and no ``requires.*`` is not a
    declaration. ``requires.config`` is excluded — reserved/future,
    doesn't currently affect eligibility.
    """
    return (
        spec.metadata is not None
        and spec.metadata.requires is not None
        and bool(
            spec.metadata.requires.bins
            or spec.metadata.requires.any_bins
            or spec.metadata.requires.env
        )
    )


def _render_install_command(spec: SkillInstallSpec) -> str:
    """Render a display-only shell command from an install spec.

    Thin alias for :func:`~agentos.skills.install_kinds.render_install_command`
    — the hints and the executors read one builder, so what the operator is
    shown is what would run.
    """
    return render_install_command(spec)


def diagnose_eligibility(spec: SkillSpec, ctx: EligibilityContext) -> EligibilityReport:
    """Detailed diagnosis: calls check_eligibility for the gate, then collects reasons.

    The boolean in the report is always authoritative (from check_eligibility).
    The detail fields explain *why* the skill is ineligible.
    """
    eligible = check_eligibility(spec, ctx)
    if eligible:
        return EligibilityReport(eligible=True, declared=_is_declared(spec))

    reasons: list[str] = []
    missing_bins: list[str] = []
    missing_env: list[str] = []
    missing_env_detail: list[SkillEnvVar] = []
    disabled = False
    wrong_os = False

    # Walk each check category to collect detail
    if spec.name in ctx.disabled_set:
        disabled = True
        reasons.append(f"Skill '{spec.name}' is disabled")

    if ctx.enabled_set is not None and spec.name not in ctx.enabled_set:
        disabled = True
        reasons.append(f"Skill '{spec.name}' not in enabled set")

    meta = spec.metadata
    if meta:
        if meta.os and ctx.os_name and ctx.os_name not in meta.os:
            wrong_os = True
            reasons.append(f"OS mismatch: requires {', '.join(meta.os)}, running {ctx.os_name}")

        if meta.requires:
            for b in meta.requires.bins:
                if not _has_bin(b, ctx):
                    missing_bins.append(b)
                    reasons.append(f"Missing binary: {b}")

            if meta.requires.any_bins:
                if not any(_has_bin(b, ctx) for b in meta.requires.any_bins):
                    for b in meta.requires.any_bins:
                        if not _has_bin(b, ctx):
                            missing_bins.append(b)
                    reasons.append(f"Need one of: {', '.join(meta.requires.any_bins)}")

            for declared in meta.requires.env:
                if not _has_env(declared.name, ctx):
                    missing_env.append(declared.name)
                    missing_env_detail.append(declared)
                    reasons.append(f"Missing env var: {declared.name}")

    # Match missing bins against install specs to produce hints
    install_hints: list[InstallHint] = []
    if meta and missing_bins:
        for ispec in meta.install:
            if ispec.bins and any(b in missing_bins for b in ispec.bins):
                cmd = _render_install_command(ispec)
                if cmd:
                    install_hints.append(
                        InstallHint(
                            kind=normalize_install_kind(ispec.kind),
                            label=ispec.label or f"Install via {ispec.kind}",
                            command=cmd,
                        )
                    )

    return EligibilityReport(
        eligible=False,
        reasons=reasons,
        missing_bins=missing_bins,
        missing_env=missing_env,
        missing_env_detail=missing_env_detail,
        install_hints=install_hints,
        disabled=disabled,
        wrong_os=wrong_os,
        declared=_is_declared(spec),
    )
