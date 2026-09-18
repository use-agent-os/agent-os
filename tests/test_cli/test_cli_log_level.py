"""Issue #2896: the CLI printed every structlog debug event to stderr.

``agentos.cli.main`` routed structlog to stderr but installed no level
threshold, so ``agentos context`` -- which resolves five tool profiles, each
logging ``tool_filtered`` at debug per excluded tool -- put ~190 lines on the
terminal on top of its tables. The CLI now filters at INFO unless
``AGENTOS_LOG_LEVEL`` says otherwise; the gateway, whose ``debug.log`` tee
needs those events, restores its own configured level when it boots.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
import structlog

from agentos.cli import main as cli_main
from agentos.gateway import boot
from agentos.gateway.config import GatewayConfig


@pytest.fixture(autouse=True)
def _restore_structlog() -> None:
    yield
    structlog.reset_defaults()
    # Put the CLI's own configuration back for whichever test imports next.
    cli_main._route_logs_to_stderr()


# ── the threshold ──────────────────────────────────────────────────────────


def test_debug_events_do_not_reach_stderr_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AGENTOS_LOG_LEVEL", raising=False)
    cli_main._route_logs_to_stderr()

    structlog.get_logger("t").debug("tool_filtered", tool="x", reason="not_allowed")

    assert capsys.readouterr().err == ""


def test_info_and_above_still_reach_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AGENTOS_LOG_LEVEL", raising=False)
    cli_main._route_logs_to_stderr()

    log = structlog.get_logger("t")
    log.info("kept.info")
    log.warning("kept.warning")
    log.error("kept.error")

    out = capsys.readouterr().err
    assert "kept.info" in out and "kept.warning" in out and "kept.error" in out


def test_nothing_goes_to_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The original reason for the routing (json | jq) must survive the filter."""
    monkeypatch.delenv("AGENTOS_LOG_LEVEL", raising=False)
    cli_main._route_logs_to_stderr()

    structlog.get_logger("t").info("routed")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "routed" in captured.err


@pytest.mark.parametrize("spelling", ["debug", "DEBUG", " Debug "])
def test_agentos_log_level_debug_lets_debug_events_through(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], spelling: str
) -> None:
    monkeypatch.setenv("AGENTOS_LOG_LEVEL", spelling)
    cli_main._route_logs_to_stderr()

    structlog.get_logger("t").debug("tool_filtered")

    assert "tool_filtered" in capsys.readouterr().err


def test_agentos_log_level_warning_hides_info(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTOS_LOG_LEVEL", "warning")
    cli_main._route_logs_to_stderr()

    log = structlog.get_logger("t")
    log.info("hidden")
    log.warning("shown")

    out = capsys.readouterr().err
    assert "hidden" not in out
    assert "shown" in out


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("info", logging.INFO),
        ("WARN", logging.WARNING),
        ("warning", logging.WARNING),
        ("error", logging.ERROR),
        ("critical", logging.CRITICAL),
        ("debug", logging.DEBUG),
    ],
)
def test_cli_log_level_accepts_the_names_the_gateway_accepts(
    monkeypatch: pytest.MonkeyPatch, name: str, expected: int
) -> None:
    monkeypatch.setenv("AGENTOS_LOG_LEVEL", name)

    assert cli_main._cli_log_level() == expected


@pytest.mark.parametrize("bogus", ["", "   ", "verbose", "TRACE", "42"])
def test_an_unknown_level_name_falls_back_to_info_not_to_everything(
    monkeypatch: pytest.MonkeyPatch, bogus: str
) -> None:
    """A typo must not silently turn the terminal back into a debug firehose."""
    monkeypatch.setenv("AGENTOS_LOG_LEVEL", bogus)

    assert cli_main._cli_log_level() == logging.INFO


def test_unset_means_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTOS_LOG_LEVEL", raising=False)

    assert cli_main._cli_log_level() == logging.INFO


# ── the gateway keeps its own level ────────────────────────────────────────


def _remove_debug_handlers() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_agentos_debug_file_handler", False):
            root.removeHandler(handler)
            handler.close()


def test_gateway_boot_restores_debug_for_its_own_process(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The gateway's ``log_level`` defaults to DEBUG and its debug.log tee copies
    structlog events; the CLI's INFO threshold must not starve it."""
    monkeypatch.delenv("AGENTOS_LOG_LEVEL", raising=False)
    monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path))
    cli_main._route_logs_to_stderr()
    structlog.get_logger("t").debug("before.boot")
    assert "before.boot" not in capsys.readouterr().err

    try:
        boot._setup_file_logging(GatewayConfig(log_level="DEBUG", log_file_enabled=False))
        structlog.get_logger("t").debug("after.boot")
    finally:
        _remove_debug_handlers()

    assert "after.boot" in capsys.readouterr().err


def test_gateway_boot_honours_a_quieter_configured_level(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.delenv("AGENTOS_LOG_LEVEL", raising=False)
    monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path))
    cli_main._route_logs_to_stderr()

    try:
        boot._setup_file_logging(GatewayConfig(log_level="WARNING", log_file_enabled=False))
        log = structlog.get_logger("t")
        log.info("hidden.info")
        log.warning("shown.warning")
    finally:
        _remove_debug_handlers()

    out = capsys.readouterr().err
    assert "hidden.info" not in out
    assert "shown.warning" in out


def test_gateway_level_env_override_wins_over_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENTOS_LOG_LEVEL", "ERROR")
    monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path))
    cli_main._route_logs_to_stderr()

    try:
        boot._setup_file_logging(GatewayConfig(log_level="DEBUG", log_file_enabled=False))
        log = structlog.get_logger("t")
        log.warning("hidden.warning")
        log.error("shown.error")
    finally:
        _remove_debug_handlers()

    out = capsys.readouterr().err
    assert "hidden.warning" not in out
    assert "shown.error" in out


def test_gateway_file_tee_still_receives_debug_events(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The threshold is applied before the tee, so filtering too low would
    silently empty debug.log of every structlog event; it must not."""
    monkeypatch.delenv("AGENTOS_LOG_LEVEL", raising=False)
    monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path))
    cli_main._route_logs_to_stderr()

    try:
        boot._setup_file_logging(GatewayConfig(log_level="DEBUG", log_file_enabled=True))
        structlog.get_logger("t").debug("teed.debug.event", marker="present")
        for handler in logging.getLogger().handlers:
            handler.flush()
    finally:
        _remove_debug_handlers()
        boot._remove_structlog_tee()

    assert "teed.debug.event" in (tmp_path / "debug.log").read_text(encoding="utf-8")


# ── the command from the report ────────────────────────────────────────────


def _run_cli(args: list[str], env_home: Path, **extra_env: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["AGENTOS_STATE_DIR"] = str(env_home)
    env.pop("AGENTOS_LOG_LEVEL", None)
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "agentos.cli.main", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=env_home,
        timeout=180,
    )


def test_agentos_context_prints_only_its_tables(tmp_path: Path) -> None:
    result = _run_cli(["context"], tmp_path)

    assert result.returncode == 0, result.stderr
    assert "Fixed per-request cost" in result.stdout
    assert "tool_filtered" not in result.stderr
    assert "[debug" not in result.stderr


def test_agentos_context_debug_events_come_back_on_request(tmp_path: Path) -> None:
    result = _run_cli(["context"], tmp_path, AGENTOS_LOG_LEVEL="debug")

    assert result.returncode == 0, result.stderr
    assert "tool_filtered" in result.stderr
    assert "Fixed per-request cost" in result.stdout
