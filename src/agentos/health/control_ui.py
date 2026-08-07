"""Detect when the bundled React Control UI is older than its frontend sources.

Warn-only helper shared by gateway boot and ``agentos doctor``. The mtime
comparison is a hint, not an oracle: ``git checkout`` / ``git pull`` rewrite
source mtimes and can flag a legitimately fresh bundle, so callers must never
gate readiness on this result.

Lives under ``health`` rather than at the top level so both consumers stay
inside the architecture import contract: ``health/evaluator.py`` reaches it
intra-package, and the gateway reaches it over the already-approved
``("gateway", "health")`` edge. The bundle path is passed in by those gateway
callers, which already own ``_DIST_DIR`` — nothing here re-derives it.

Nothing is memoized. Both a boot log line and a ``doctor.status`` response must
describe the filesystem as it is *now*: caching the verdict would leave the
doctor reporting a stale bundle forever after the operator rebuilt it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Built or fetched, never build inputs. ``dist`` covers both the Vite output
# directory and any nested build artefact a tool drops inside the tree.
_PRUNE_DIRS = frozenset({"node_modules", "dist", ".git", "__pycache__", ".vite"})

BUILD_CMD = "python scripts/build_control_ui.py build"


def build_hint() -> str:
    """Return the command that rebuilds the Control UI bundle."""
    return BUILD_CMD


@dataclass(frozen=True)
class ControlUiBundleReport:
    """One consistent view of bundle-vs-sources, computed in a single pass.

    ``stale`` is ``None`` whenever there is nothing to compare — a wheel install
    (no ``frontend/``) or a missing bundle. Reporting all three together keeps
    the doctor's evidence internally consistent; deriving them from separate
    reads is how ``stale=True`` alongside ``bundle_mtime > source_mtime`` happens.
    """

    stale: bool | None
    source_mtime: float | None
    bundle_mtime: float | None

    @property
    def wheel_install(self) -> bool:
        """True when no frontend sources ship at all, so nothing can be stale."""
        return self.source_mtime is None


def checkout_root() -> Path | None:
    """The source checkout this module lives in, or ``None`` for a wheel install.

    Walks up looking for the checkout markers instead of counting parent levels,
    so moving this module between packages cannot silently retarget it at some
    unrelated directory above ``site-packages``.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file() and (candidate / "frontend").is_dir():
            return candidate
    return None


def frontend_input_mtime(root: Path | None = None) -> float | None:
    """Newest mtime under ``frontend/``, or ``None`` when it does not ship.

    Walks the whole directory minus the pruned build/vendor trees rather than
    naming individual inputs: an allowlist of files silently goes stale the
    first time someone adds a config the build reads (``eslint.config.js``,
    ``components.json``, ``scripts/check-bundle-budget.mjs`` all postdate one),
    and the failure mode is a false negative nobody can see.
    """
    base = root if root is not None else checkout_root()
    if base is None:
        return None
    frontend = base / "frontend"
    if not (frontend / "package.json").is_file():
        return None
    newest = 0.0
    for current, dirs, files in os.walk(frontend):
        dirs[:] = [name for name in dirs if name not in _PRUNE_DIRS]
        for filename in files:
            try:
                newest = max(newest, (Path(current) / filename).stat().st_mtime)
            except OSError:
                continue
    return newest


def inspect_control_ui_bundle(
    bundle_index: Path,
    *,
    root: Path | None = None,
) -> ControlUiBundleReport:
    """Compare ``bundle_index`` against the frontend sources in one pass.

    Strict ``>``: equal timestamps count as fresh, which is false-negative safe
    on filesystems with coarse mtime resolution.
    """
    source_mtime = frontend_input_mtime(root)
    try:
        bundle_mtime: float | None = bundle_index.stat().st_mtime
    except OSError:
        bundle_mtime = None
    if source_mtime is None or bundle_mtime is None:
        return ControlUiBundleReport(
            stale=None, source_mtime=source_mtime, bundle_mtime=bundle_mtime
        )
    return ControlUiBundleReport(
        stale=source_mtime > bundle_mtime,
        source_mtime=source_mtime,
        bundle_mtime=bundle_mtime,
    )


def control_ui_boot_warning(bundle_index: Path, *, root: Path | None = None) -> str | None:
    """The boot log event for this bundle, or ``None`` when there is nothing to say.

    Lives here rather than inline in ``boot.py`` so the ordering is testable
    without standing up a gateway: a missing bundle outranks a stale one, since
    the two hints differ in urgency and only one should be emitted.
    """
    if not bundle_index.is_file():
        return "gateway.control_ui.dist_missing"
    if inspect_control_ui_bundle(bundle_index, root=root).stale:
        return "gateway.control_ui.dist_stale"
    return None
