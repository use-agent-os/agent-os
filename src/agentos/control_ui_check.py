"""Detect when the bundled React Control UI is older than its frontend sources.

Warn-only helper shared by gateway boot and ``agentos doctor``. The mtime
comparison is a hint, not an oracle: ``git checkout`` / ``git pull`` rewrite
source mtimes and can flag a legitimately fresh bundle, so callers must never
gate readiness on this result.

Must stay dependency-free (stdlib only) and must not import any ``agentos.*``
module: the architecture import-contract test tracks edges between top-level
*packages* only, and this module deliberately lives outside that graph.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

# Inputs consumed by the Vite build (sdist-only; absent in wheels).
FRONTEND_INPUT_DIRS = ("src", "public")
FRONTEND_INPUT_FILES = (
    "index.html",
    "package.json",
    "package-lock.json",
    "vite.config.ts",
    "tsconfig.json",
)
# Directories that are built or fetched, never inputs.
_PRUNE_DIRS = {"node_modules", "dist", ".git"}

# Mirrored in agentos/gateway/control_ui.py (_DIST_DIR); the contract test
# tests/test_health/test_control_ui_check.py asserts they stay in sync.
DIST_REL = Path("src") / "agentos" / "gateway" / "static" / "dist" / "index.html"

BUILD_CMD = "python scripts/build_control_ui.py build"


def build_hint() -> str:
    """Return the command that rebuilds the Control UI bundle."""
    return BUILD_CMD


@functools.cache
def repo_root(root: Path | None = None) -> Path:
    """Resolve the checkout root; ``root`` wins when given (tests)."""
    if root is not None:
        return root
    return Path(__file__).resolve().parents[2]


@functools.cache
def frontend_input_mtime(root: Path | None = None) -> float | None:
    """Newest mtime among tracked frontend inputs, or ``None`` for a wheel.

    ``None`` means there is nothing to compare against (``frontend/`` ships
    only in the sdist), so wheel installs must stay silent.
    """
    frontend = repo_root(root) / "frontend"
    if not (frontend / "package.json").is_file():
        return None
    newest = 0.0
    for input_dir in FRONTEND_INPUT_DIRS:
        for current, dirs, files in os.walk(frontend / input_dir):
            dirs[:] = [dirname for dirname in dirs if dirname not in _PRUNE_DIRS]
            for filename in files:
                try:
                    newest = max(newest, (Path(current) / filename).stat().st_mtime)
                except OSError:
                    continue
    for filename in FRONTEND_INPUT_FILES:
        try:
            newest = max(newest, (frontend / filename).stat().st_mtime)
        except OSError:
            continue
    return newest


@functools.cache
def control_ui_is_stale(root: Path | None = None) -> bool | None:
    """Whether the bundled Control UI predates the frontend sources.

    Returns ``None`` when there is nothing to compare (wheel install or
    missing bundle). Strict ``>``: equal timestamps count as fresh, which is
    false-negative safe on filesystems with coarse mtime resolution.
    """
    source_mtime = frontend_input_mtime(root)
    if source_mtime is None:
        return None
    try:
        bundle_mtime = (repo_root(root) / DIST_REL).stat().st_mtime
    except OSError:
        return None
    return source_mtime > bundle_mtime
