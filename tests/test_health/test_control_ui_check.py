"""Regression tests for control-UI staleness detection (issue #200)."""

from __future__ import annotations

import os
from pathlib import Path

from agentos.control_ui_check import (
    BUILD_CMD,
    DIST_REL,
    control_ui_is_stale,
    frontend_input_mtime,
    repo_root,
)
from agentos.gateway.control_ui import _DIST_DIR
from agentos.health.evaluator import evaluate_control_ui
from agentos.health.model import build_report

_BASE = 1_700_000_000


def _utime(path: Path, offset: float) -> None:
    os.utime(path, (_BASE + offset, _BASE + offset))


def _make_tree(root: Path, *, frontend: bool = True, dist: bool = True) -> Path:
    """Synthetic checkout: frontend sources (fresh) + built bundle (old)."""
    if frontend:
        (root / "frontend" / "src").mkdir(parents=True)
        (root / "frontend" / "src" / "main.tsx").write_text("export const x = 1")
        (root / "frontend" / "package.json").write_text("{}")
        (root / "frontend" / "index.html").write_text("<html></html>")
        _utime(root / "frontend" / "src" / "main.tsx", 100)
        _utime(root / "frontend" / "package.json", 100)
        _utime(root / "frontend" / "index.html", 100)
    if dist:
        dist_index = root / DIST_REL
        dist_index.parent.mkdir(parents=True)
        dist_index.write_text("<html></html>")
        _utime(dist_index, 200)
    return root


def test_wheel_install_no_frontend_is_not_stale(tmp_path: Path) -> None:
    root = _make_tree(tmp_path, frontend=False)
    assert frontend_input_mtime(root) is None
    assert control_ui_is_stale(root) is None


def test_missing_bundle_is_not_stale(tmp_path: Path) -> None:
    root = _make_tree(tmp_path, dist=False)
    assert frontend_input_mtime(root) is not None
    assert control_ui_is_stale(root) is None


def test_fresh_bundle_not_stale(tmp_path: Path) -> None:
    assert control_ui_is_stale(_make_tree(tmp_path)) is False


def test_stale_bundle_detected(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    _utime(root / "frontend" / "src" / "main.tsx", 300)
    assert control_ui_is_stale(root) is True


def test_prunes_node_modules_and_dist(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    ignored = root / "frontend" / "src" / "node_modules" / "lib"
    ignored.mkdir(parents=True)
    (ignored / "index.js").write_text("x")
    _utime(ignored / "index.js", 300)
    assert control_ui_is_stale(root) is False


def test_top_level_input_files_count(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    _utime(root / "frontend" / "package.json", 300)
    assert control_ui_is_stale(root) is True


def test_equal_mtimes_not_stale(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    _utime(root / "frontend" / "src" / "main.tsx", 200)
    assert control_ui_is_stale(root) is False


def test_dist_path_contract() -> None:
    assert (repo_root() / DIST_REL).resolve().parent == _DIST_DIR.resolve()


def test_evaluate_control_ui_findings() -> None:
    assert evaluate_control_ui({"stale": None}) == []
    assert evaluate_control_ui({"stale": False}) == []
    findings = evaluate_control_ui(
        {
            "stale": True,
            "sourceMtime": 1.0,
            "bundleMtime": 0.5,
            "wheelInstall": False,
        }
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.id == "control_ui.stale"
    assert finding.severity == "warn"
    assert finding.readiness_impact == "degrades"
    assert finding.fix_steps[0].command == BUILD_CMD
    assert finding.restart_required is False
    assert finding.evidence == {"sourceMtime": 1.0, "bundleMtime": 0.5, "wheelInstall": False}


def test_build_report_degraded_status() -> None:
    finding = evaluate_control_ui({"stale": True})[0]
    report = build_report([finding])
    assert report["status"] == "degraded"
    assert report["ready"] is True
