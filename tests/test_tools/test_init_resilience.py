from __future__ import annotations

import importlib

import pytest
import structlog.testing

import agentos.tools.builtin as builtin

_REAL_IMPORT_MODULE = importlib.import_module


def _reload_builtin_with_failure(
    monkeypatch: pytest.MonkeyPatch,
    failing_module: str,
) -> None:
    def _fake_import(name: str, package: str | None = None):
        if name == f"agentos.tools.builtin.{failing_module}":
            raise RuntimeError(f"{failing_module} import failed")
        return _REAL_IMPORT_MODULE(name, package)

    monkeypatch.setattr(importlib, "import_module", _fake_import)
    importlib.reload(builtin)


def _restore_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    importlib.reload(builtin)


def test_nonfatal_builtin_import_failure_logs_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        with structlog.testing.capture_logs() as captured:
            _reload_builtin_with_failure(monkeypatch, "web")
    finally:
        _restore_builtin(monkeypatch)

    assert any(
        event["event"] == "builtin_tool.import_failed" and event["module"] == "web"
        for event in captured
    )


@pytest.mark.parametrize("module", ["shell", "patch", "filesystem"])
def test_fatal_builtin_import_failures_propagate(
    module: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        with pytest.raises(RuntimeError, match=f"{module} import failed"):
            _reload_builtin_with_failure(monkeypatch, module)
    finally:
        _restore_builtin(monkeypatch)
