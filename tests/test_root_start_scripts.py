from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent


def test_start_sh_exists() -> None:
    assert (_ROOT / "start.sh").exists(), "start.sh must exist at repo root"


def test_start_ps1_exists() -> None:
    assert (_ROOT / "start.ps1").exists(), "start.ps1 must exist at repo root"


def test_start_ps1_matches_generator() -> None:
    """The repo-root start.ps1 is a full copy of the wheelhouse generator output
    (render_start_ps1) and the release tree regenerates it at build time. It
    previously drifted — carrying the stale ``$RequiresRouterRuntime`` flag and
    "safe router fallback" wording after the generator was renamed to
    ``$RequiresOnnxRuntime`` for the ML-router removal. Pin it to the generator
    so future generator renames can't leave the committed copy stale."""
    from scripts.build_wheelhouse_zip import render_start_ps1

    expected = render_start_ps1("recommended")
    on_disk = (_ROOT / "start.ps1").read_text(encoding="utf-8")
    assert on_disk == expected, (
        "root start.ps1 has drifted from render_start_ps1('recommended'); "
        "regenerate it from scripts/build_wheelhouse_zip.py"
    )
    # Guard the specific stale tokens the finding called out.
    assert "$RequiresOnnxRuntime" in on_disk
    assert "RequiresRouterRuntime" not in on_disk
    assert "safe router fallback" not in on_disk


def test_start_sh_is_executable() -> None:
    if sys.platform.startswith("win"):
        return  # git mode bits not enforced on Windows
    mode = (_ROOT / "start.sh").stat().st_mode
    assert mode & 0o111 != 0, "start.sh must have executable bit set"
