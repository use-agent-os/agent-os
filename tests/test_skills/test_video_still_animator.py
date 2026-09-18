"""Regression tests for the bundled video-still-animator Ken Burns script.

The script is intentionally imported by file path, like the other bundled
skill scripts under ``tests/test_skills`` (see
``test_openrouter_skill_key_resolution.py``): its directory contains hyphens
and it runs as a standalone subprocess script, not a package module.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    REPO_ROOT
    / "src/agentos/skills/bundled/video-still-animator/scripts/animate.py"
)


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_agentos_test_animate", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def animate_script() -> ModuleType:
    return _load_script()


def test_filter_graph_scales_by_covering_then_crops_instead_of_stretching(
    animate_script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #2728: a landscape still at a vertical target must not be
    forced straight to the target aspect ratio -- that stretches it."""
    input_file = tmp_path / "shot.png"
    input_file.write_bytes(b"not a real png, subprocess.run is mocked below")
    output_file = tmp_path / "out.mp4"

    captured_cmd: list[str] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        captured_cmd.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "animate.py",
            "--input", str(input_file),
            "--output", str(output_file),
            "--width", "720",
            "--height", "1280",
        ],
    )

    assert animate_script.main() == 0

    vf = captured_cmd[captured_cmd.index("-filter_complex") + 1]
    assert "scale=2880:5120:force_original_aspect_ratio=increase" in vf
    assert "crop=2880:5120" in vf
    # The naive fix -- scale=2880:5120 alone, no force_original_aspect_ratio
    # -- is exactly the bug: confirm it's gone, not just that the good form
    # is present.
    assert "scale=2880:5120:flags=lanczos" not in vf


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="requires a real ffmpeg binary")
def test_real_render_does_not_distort_a_landscape_still_into_a_vertical_clip(
    animate_script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end with a real ffmpeg: a perfect circle in a 16:9 still must
    still measure as a circle (not an ellipse) after animating to 9:16.

    A stretch-to-fit bug and a correct cover-and-crop fix both produce a
    720x1280 output, so only pixel content -- not just dimensions -- proves
    the difference.
    """
    from PIL import Image, ImageDraw

    input_file = tmp_path / "shot.png"
    output_file = tmp_path / "out.mp4"

    img = Image.new("L", (1920, 1080), color=0)
    ImageDraw.Draw(img).ellipse((660, 240, 1260, 840), fill=255)  # true circle, r=300
    img.save(input_file)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "animate.py",
            "--input", str(input_file),
            "--output", str(output_file),
            "--width", "720",
            "--height", "1280",
            "--duration", "1",
        ],
    )

    assert animate_script.main() == 0

    frame_path = tmp_path / "frame.png"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(output_file), "-frames:v", "1", str(frame_path)],
        capture_output=True,
        check=True,
    )

    frame = Image.open(frame_path).convert("L")
    px = frame.load()
    w, h = frame.size
    xs = [x for x in range(w) for y in range(0, h, 2) if px[x, y] > 128]
    ys = [y for x in range(0, w, 2) for y in range(h) if px[x, y] > 128]
    bbox_w, bbox_h = max(xs) - min(xs), max(ys) - min(ys)

    # A stretched ellipse (the bug) comes out roughly 4x taller than wide at
    # this width/height combination; an undistorted circle is close to 1:1.
    # zoompan's slight initial zoom leaves a little slack either way.
    assert 0.85 <= bbox_w / bbox_h <= 1.15, (bbox_w, bbox_h)
