"""Regression tests for control-UI staleness detection (issue #200)."""

from __future__ import annotations

import os
from pathlib import Path

from agentos.gateway.config import GatewayConfig
from agentos.health.control_ui import (
    BUILD_CMD,
    checkout_root,
    control_ui_boot_warning,
    frontend_input_mtime,
    inspect_control_ui_bundle,
)
from agentos.health.evaluator import evaluate_control_ui
from agentos.health.model import build_report

_BASE = 1_700_000_000
_DIST_REL = Path("src") / "agentos" / "gateway" / "static" / "dist" / "index.html"


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
        dist_index = root / _DIST_REL
        dist_index.parent.mkdir(parents=True)
        dist_index.write_text("<html></html>")
        _utime(dist_index, 200)
    return root


def _stale(root: Path) -> bool | None:
    return inspect_control_ui_bundle(root / _DIST_REL, root=root).stale


def test_wheel_install_no_frontend_is_not_stale(tmp_path: Path) -> None:
    root = _make_tree(tmp_path, frontend=False)
    report = inspect_control_ui_bundle(root / _DIST_REL, root=root)
    assert frontend_input_mtime(root) is None
    assert report.stale is None
    assert report.wheel_install is True


def test_missing_bundle_is_not_stale(tmp_path: Path) -> None:
    root = _make_tree(tmp_path, dist=False)
    assert frontend_input_mtime(root) is not None
    assert _stale(root) is None


def test_fresh_bundle_not_stale(tmp_path: Path) -> None:
    assert _stale(_make_tree(tmp_path)) is False


def test_stale_bundle_detected(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    _utime(root / "frontend" / "src" / "main.tsx", 300)
    assert _stale(root) is True


def test_equal_mtimes_not_stale(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    _utime(root / "frontend" / "src" / "main.tsx", 200)
    assert _stale(root) is False


def test_prunes_built_and_vendored_trees(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    for pruned in ("node_modules/lib", "dist/assets", ".git/objects"):
        directory = root / "frontend" / pruned
        directory.mkdir(parents=True)
        (directory / "artifact.js").write_text("x")
        _utime(directory / "artifact.js", 300)
    assert _stale(root) is False


def test_tracks_any_frontend_input_not_a_named_allowlist(tmp_path: Path) -> None:
    # A hand-kept file list goes stale the first time someone adds a config the
    # build reads; walking the tree keeps working without maintenance.
    root = _make_tree(tmp_path)
    for added in ("eslint.config.js", "components.json", "scripts/check-bundle-budget.mjs"):
        path = root / "frontend" / added
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
        _utime(path, 100)
    assert _stale(root) is False

    _utime(root / "frontend" / "scripts" / "check-bundle-budget.mjs", 300)
    assert _stale(root) is True


def test_verdict_is_never_memoized(tmp_path: Path) -> None:
    # The gateway is long-lived: caching would leave `agentos doctor` reporting
    # a stale bundle forever after the operator ran the rebuild it asked for.
    root = _make_tree(tmp_path)
    _utime(root / "frontend" / "src" / "main.tsx", 300)
    assert _stale(root) is True

    _utime(root / _DIST_REL, 400)  # the operator rebuilds
    report = inspect_control_ui_bundle(root / _DIST_REL, root=root)
    assert report.stale is False
    # Evidence stays internally consistent because it comes from one pass.
    assert report.source_mtime is not None and report.bundle_mtime is not None
    assert report.bundle_mtime > report.source_mtime


def test_checkout_root_finds_this_repo() -> None:
    root = checkout_root()
    assert root is not None
    assert (root / "frontend" / "package.json").is_file()
    assert (root / "pyproject.toml").is_file()


def test_evaluate_control_ui_findings() -> None:
    assert evaluate_control_ui({"stale": None}) == []
    assert evaluate_control_ui({"stale": False}) == []
    findings = evaluate_control_ui(
        {"stale": True, "sourceMtime": 1.0, "bundleMtime": 0.5, "wheelInstall": False}
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.id == "control_ui.stale"
    assert finding.severity == "warn"
    assert finding.readiness_impact == "optional"
    assert finding.fix_steps[0].command == BUILD_CMD
    assert finding.restart_required is False
    assert finding.evidence == {"sourceMtime": 1.0, "bundleMtime": 0.5, "wheelInstall": False}


def test_a_stale_bundle_never_degrades_overall_status() -> None:
    # An mtime hint that `git checkout` can trip must not turn the whole report
    # yellow; the finding still shows up with its rebuild step.
    report = build_report(evaluate_control_ui({"stale": True}))
    assert report["status"] == "ready"
    assert report["ready"] is True
    assert report["findings"][0]["id"] == "control_ui.stale"


# --- consumers -------------------------------------------------------------


def test_doctor_payload_is_silent_when_control_ui_is_disabled() -> None:
    from agentos.gateway.rpc import RpcContext
    from agentos.gateway.rpc_doctor import _control_ui_payload

    config = GatewayConfig()
    config.control_ui.enabled = False

    payload = _control_ui_payload(RpcContext(conn_id="test", config=config))

    assert payload["stale"] is None
    assert evaluate_control_ui(payload) == []


def test_doctor_payload_reports_the_real_bundle_when_enabled() -> None:
    from agentos.gateway.rpc import RpcContext
    from agentos.gateway.rpc_doctor import _control_ui_payload

    payload = _control_ui_payload(RpcContext(conn_id="test", config=GatewayConfig()))

    assert set(payload) == {"stale", "sourceMtime", "bundleMtime", "wheelInstall"}
    assert payload["stale"] in (True, False, None)


def test_boot_warning_prefers_missing_over_stale(tmp_path: Path) -> None:
    """A missing bundle outranks a stale one — the hints differ in urgency."""
    root = _make_tree(tmp_path, dist=False)
    index = root / _DIST_REL
    index.parent.mkdir(parents=True)

    assert control_ui_boot_warning(index, root=root) == "gateway.control_ui.dist_missing"

    index.write_text("<html></html>")
    _utime(index, 100)
    _utime(root / "frontend" / "src" / "main.tsx", 300)
    assert control_ui_boot_warning(index, root=root) == "gateway.control_ui.dist_stale"

    _utime(index, 400)
    assert control_ui_boot_warning(index, root=root) is None


def test_boot_emits_the_events_the_gateway_logs() -> None:
    """The branch in boot.py must go through the helper these tests cover."""
    source = (checkout_root() or Path()) / "src" / "agentos" / "gateway" / "boot.py"
    text = source.read_text(encoding="utf-8")
    assert "control_ui_boot_warning" in text
    # The event names are produced by the helper, not spelled out at the call site.
    assert 'log.warning("gateway.control_ui.dist_stale"' not in text
