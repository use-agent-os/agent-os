"""Passive update-notice throttle + suppression matrix (network mocked)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentos.cli import update_notice


@pytest.fixture(autouse=True)
def _state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AGENTOS_NO_UPDATE_NOTICE", raising=False)
    for var in update_notice._CI_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _mock_latest(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    monkeypatch.setattr(
        "agentos.cli.pypi_client.latest_version", lambda timeout=2.0: value
    )


def _tty(monkeypatch: pytest.MonkeyPatch, is_tty: bool) -> None:
    monkeypatch.setattr(update_notice, "_stderr_is_tty", lambda: is_tty)


def test_emits_when_newer_available(monkeypatch: pytest.MonkeyPatch) -> None:
    _tty(monkeypatch, True)
    _mock_latest(monkeypatch, "2026.8.1")
    msg = update_notice.maybe_emit_update_notice(current_version="2026.7.18")
    assert msg is not None
    assert "2026.7.18 → 2026.8.1" in msg
    assert "agentos upgrade" in msg


def test_suppressed_when_up_to_date(monkeypatch: pytest.MonkeyPatch) -> None:
    _tty(monkeypatch, True)
    _mock_latest(monkeypatch, "2026.7.18")
    assert update_notice.maybe_emit_update_notice(current_version="2026.7.18") is None


def test_suppressed_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    _tty(monkeypatch, False)
    _mock_latest(monkeypatch, "2026.9.9")
    assert update_notice.maybe_emit_update_notice(current_version="2026.7.18") is None


def test_suppressed_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    _tty(monkeypatch, True)
    monkeypatch.setenv("CI", "true")
    _mock_latest(monkeypatch, "2026.9.9")
    assert update_notice.maybe_emit_update_notice(current_version="2026.7.18") is None


def test_suppressed_when_config_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    _tty(monkeypatch, True)
    _mock_latest(monkeypatch, "2026.9.9")
    config = SimpleNamespace(updates=SimpleNamespace(notify=False))
    assert (
        update_notice.maybe_emit_update_notice(current_version="2026.7.18", config=config)
        is None
    )


def test_env_escape_hatch_suppresses(monkeypatch: pytest.MonkeyPatch) -> None:
    _tty(monkeypatch, True)
    monkeypatch.setenv("AGENTOS_NO_UPDATE_NOTICE", "1")
    _mock_latest(monkeypatch, "2026.9.9")
    assert update_notice.maybe_emit_update_notice(current_version="2026.7.18") is None


def test_throttled_within_24h(monkeypatch: pytest.MonkeyPatch) -> None:
    _tty(monkeypatch, True)
    _mock_latest(monkeypatch, "2026.9.9")
    # First call at t=0 records the check.
    first = update_notice.maybe_emit_update_notice(current_version="2026.7.18", now=0.0)
    assert first is not None
    # 1 hour later — throttled.
    second = update_notice.maybe_emit_update_notice(current_version="2026.7.18", now=3600.0)
    assert second is None
    # 25 hours later — due again.
    third = update_notice.maybe_emit_update_notice(
        current_version="2026.7.18", now=25 * 3600.0
    )
    assert third is not None


def test_offline_records_check_and_stays_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    _tty(monkeypatch, True)
    _mock_latest(monkeypatch, None)
    assert update_notice.maybe_emit_update_notice(current_version="2026.7.18", now=0.0) is None
    # The offline attempt still throttles the next call.
    state = update_notice._read_state(update_notice.notice_state_path())
    assert "last_checked" in state
