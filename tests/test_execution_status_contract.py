from __future__ import annotations

import importlib


def _execution_status_module():
    return importlib.import_module("agentos.execution_status")


def test_execution_status_defaults_to_unknown_normal_status() -> None:
    module = _execution_status_module()

    status = module.normalize_execution_status(None)

    assert status == {
        "version": 1,
        "status": "unknown",
        "exit_code": None,
        "timed_out": False,
        "truncated": False,
        "reason": None,
        "source": "unknown",
        "preservation_class": "normal",
    }


def test_execution_status_rejects_invalid_status_with_fallback_reason() -> None:
    module = _execution_status_module()

    status = module.normalize_execution_status(
        {
            "version": 1,
            "status": "not-a-status",
            "source": "adapter",
            "preservation_class": "normal",
        }
    )

    assert status["status"] == "unknown"
    assert status["reason"] == "invalid_status"


def test_derive_is_error_is_true_only_for_terminal_failures() -> None:
    module = _execution_status_module()

    assert module.derive_is_error({"status": "success"}) is False
    assert module.derive_is_error({"status": "unknown"}) is False
    assert module.derive_is_error({"status": "error"}) is True
    assert module.derive_is_error({"status": "timeout"}) is True
    assert module.derive_is_error({"status": "cancelled"}) is True


def test_execution_status_truncated_does_not_change_success_status() -> None:
    module = _execution_status_module()

    status = module.normalize_execution_status(
        {
            "version": 1,
            "status": "success",
            "exit_code": 0,
            "timed_out": False,
            "truncated": True,
            "reason": None,
            "source": "adapter",
            "preservation_class": "retain_summary",
        }
    )

    assert status["status"] == "success"
    assert status["truncated"] is True
    assert module.derive_is_error(status) is False


def test_legacy_error_normalizes_to_diagnostic_execution_status() -> None:
    module = _execution_status_module()

    status = module.normalize_legacy_execution_status(is_error=True)

    assert status["version"] == 1
    assert status["status"] == "error"
    assert status["source"] == "legacy"
    assert status["reason"] == "legacy_missing_status"
    assert status["preservation_class"] == "diagnostic"


def test_legacy_non_error_normalizes_to_unknown_normal_status() -> None:
    module = _execution_status_module()

    status = module.normalize_legacy_execution_status(is_error=False)

    assert status["version"] == 1
    assert status["status"] == "unknown"
    assert status["source"] == "legacy"
    assert status["reason"] == "legacy_missing_status"
    assert status["preservation_class"] == "normal"
