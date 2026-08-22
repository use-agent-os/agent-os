"""Canonical vocabulary and command builder for skill install specs.

Three code paths used to carry their own idea of which ``install.kind`` values
exist and what each one runs — the Web UI executor
(:mod:`agentos.skills.hub.deps`), the agent tool (``install_skill_deps``), and
the display-only hints in :mod:`agentos.skills.eligibility`. They disagreed, so
a skill declaring a kind one path knew about failed on another, and the command
shown to the operator wasn't always the command that ran.

Everything now flows through this module: the kind sets below, and
:func:`build_install_argv`, which is both what the executors run and what the
hints render.

Not every declarable kind can be executed on the operator's behalf. ``apt``
needs root and ``download`` needs a fetch plus a chmod, so they narrow down
through :data:`AUTO_INSTALL_KINDS` and :data:`ARGV_INSTALL_KINDS` — they still
render a command the operator can copy.
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentos.skills.types import SkillInstallSpec

#: Every install kind a skill manifest may declare, after normalization.
INSTALL_KINDS = frozenset({"brew", "npm", "go", "uv", "download", "apt"})

#: Legacy spellings kept working, mapped onto their canonical kind.
INSTALL_KIND_ALIASES = {"node": "npm"}

#: Kinds that need a privilege agentos won't take on the operator's behalf.
#: They render an install command and nothing else.
MANUAL_INSTALL_KINDS = frozenset({"apt"})

#: Kinds :func:`agentos.skills.hub.deps.install_deps` can run unattended.
AUTO_INSTALL_KINDS = frozenset(INSTALL_KINDS - MANUAL_INSTALL_KINDS)

#: Kinds that reduce to a single argv. ``download`` is absent because fetching
#: a binary also has to mark it executable, which no one argv does; it runs
#: through :func:`agentos.skills.hub.deps.install_download`.
ARGV_INSTALL_KINDS = frozenset(AUTO_INSTALL_KINDS - {"download"})

# Strict allowlists — an install spec must never reach a shell as a flag or as
# a second command, and must not turn into a different operation on the
# package manager's own syntax.
_BREW_FORMULA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/_@.+-]*$")
# npm: optional @scope, plus the optional @version or @tag a manifest may pin.
_NPM_PACKAGE_RE = re.compile(
    r"^(?:@[A-Za-z0-9][A-Za-z0-9._-]*/)?[A-Za-z0-9][A-Za-z0-9._-]*(?:@[A-Za-z0-9][A-Za-z0-9.^~*-]*)?$"
)
_GO_MODULE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~/-]*(?:@[A-Za-z0-9][A-Za-z0-9._~+-]*)?$")
# uv: package, optional extras, optional PEP 440 specifier (``ruff==0.5.0``).
_UV_PACKAGE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?"
    r"(?:(?:[=<>!~]=|[<>])[A-Za-z0-9][A-Za-z0-9.*+!_-]*)?$"
)
# apt: a trailing '-' on an install line means *remove* that package, and a
# trailing '+' means force-install, so the last character is anchored too.
_APT_PACKAGE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.+-]*[a-z0-9])?$")
#: A download URL never becomes an argv, but the hint that shows it must not
#: be able to smuggle a second shell command past the operator.
DOWNLOAD_URL_RE = re.compile(r"^https://[a-zA-Z0-9._/-]+$")
# The binary a download lands as — it becomes an unquoted path in that hint.
_BIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class InstallSpecError(ValueError):
    """An install spec names an unknown kind or an unusable value."""


def normalize_install_kind(kind: str) -> str:
    """Return the canonical spelling of ``kind`` (unknown kinds pass through)."""
    normalized = (kind or "").strip().lower()
    return INSTALL_KIND_ALIASES.get(normalized, normalized)


def is_supported_install_kind(kind: str) -> bool:
    """Whether ``kind`` names a known install kind, alias spellings included."""
    return normalize_install_kind(kind) in INSTALL_KINDS


def _checked(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not value:
        raise InstallSpecError(f"Missing install value: {label}")
    if value.startswith("-") or not pattern.match(value):
        raise InstallSpecError(f"Unsafe install value for {label}: {value}")
    return value


def build_install_argv(spec: SkillInstallSpec) -> list[str]:
    """Return the command for ``spec``, or raise :class:`InstallSpecError`.

    Covers :data:`ARGV_INSTALL_KINDS` plus ``apt`` — whose command is rendered
    for the operator to run but never executed here. ``download`` has no argv
    form and always raises.
    """
    kind = normalize_install_kind(spec.kind)

    if kind == "brew":
        # brew is the one kind allowed to fall back to the spec id: its
        # manifests have always named the spec after the formula. The
        # others must say what they install — an id like "node-claude" is
        # a label, and installing it from a public registry is a hazard.
        formula = _checked(spec.formula or spec.package or spec.id, _BREW_FORMULA_RE, "formula")
        return ["brew", "install", formula]

    if kind == "npm":
        package = _checked(spec.package, _NPM_PACKAGE_RE, "package")
        # --ignore-scripts: installing a dependency must not run arbitrary
        # lifecycle scripts from the registry on the operator's machine.
        return ["npm", "install", "-g", "--ignore-scripts", package]

    if kind == "go":
        module = _checked(spec.module or spec.package, _GO_MODULE_RE, "module")
        return ["go", "install", module if "@" in module else f"{module}@latest"]

    if kind == "uv":
        package = _checked(spec.package or spec.module, _UV_PACKAGE_RE, "package")
        # A spec that declares bins wants a command on PATH; one that doesn't
        # wants an importable library in the environment.
        subcommand = ["tool", "install"] if spec.bins else ["pip", "install"]
        return ["uv", *subcommand, package]

    if kind == "apt":
        package = _checked(spec.package, _APT_PACKAGE_RE, "package")
        return ["sudo", "apt-get", "install", "-y", package]

    if kind == "download":
        raise InstallSpecError(
            "Install kind 'download' has no single-command form; it runs through the Skills page"
        )

    raise InstallSpecError(f"Unsupported install kind: {spec.kind}")


def render_install_command(spec: SkillInstallSpec) -> str:
    """The shell command for ``spec`` as a copyable string, or ``""``.

    Every surface that shows an operator what to run goes through here, so what
    is displayed is what :func:`build_install_argv` would execute. ``download``
    has no argv form and is spelled out — validated and quoted the same way.
    """
    if normalize_install_kind(spec.kind) == "download":
        url = spec.url
        if not url or not DOWNLOAD_URL_RE.match(url):
            return ""
        bin_name = spec.bins[0] if spec.bins else spec.id
        if not _BIN_NAME_RE.match(bin_name or ""):
            return ""
        dest = f"~/.local/bin/{bin_name}"
        return f"curl -fsSL -o {dest} {shlex.quote(url)} && chmod +x {dest}"
    try:
        return shlex.join(build_install_argv(spec))
    except InstallSpecError:
        return ""
