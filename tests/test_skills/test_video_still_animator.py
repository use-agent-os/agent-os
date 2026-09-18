from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "video-still-animator"
    / "scripts"
    / "animate.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("animate", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_animate_filter_complex_preserves_aspect_ratio(tmp_path, monkeypatch) -> None:
    module = _load_module()
    input_file = tmp_path / "shot.png"
    input_file.write_bytes(b"fake png data")
    output_file = tmp_path / "out.mp4"

    captured_cmd = []

    def fake_run(cmd, capture_output=True, check=False):
        captured_cmd.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "animate.py",
            "--input",
            str(input_file),
            "--output",
            str(output_file),
            "--width",
            "720",
            "--height",
            "1280",
        ],
    )

    exit_code = module.main()
    assert exit_code == 0

    assert "-filter_complex" in captured_cmd
    vf_idx = captured_cmd.index("-filter_complex") + 1
    vf_str = captured_cmd[vf_idx]

    # Verify aspect ratio preservation via force_original_aspect_ratio and crop
    assert "force_original_aspect_ratio=increase" in vf_str
    assert "crop=2880:5120" in vf_str
