"""Tests for the AGENTOS_TASK_MAX_CONCURRENCY and
AGENTOS_CHANNEL_INFLIGHT_CAP env overrides.
"""
from __future__ import annotations

import pytest

from agentos.gateway.config import GatewayConfig


def test_task_max_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """AGENTOS_TASK_MAX_CONCURRENCY=16 sets task_runtime.max_concurrency to 16."""
    monkeypatch.setenv("AGENTOS_TASK_MAX_CONCURRENCY", "16")
    config = GatewayConfig()
    assert config.task_runtime.max_concurrency == 16


def test_invalid_env_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Non-integer AGENTOS_TASK_MAX_CONCURRENCY falls back to default 4 with a warning."""
    monkeypatch.setenv("AGENTOS_TASK_MAX_CONCURRENCY", "abc")
    import logging

    with caplog.at_level(logging.WARNING):
        config = GatewayConfig()

    assert config.task_runtime.max_concurrency == 4
    assert any(
        "AGENTOS_TASK_MAX_CONCURRENCY" in record.message
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )


def test_channel_inflight_cap_default() -> None:
    """channel_inflight_cap defaults to 8 when env is not set."""
    config = GatewayConfig()
    assert config.task_runtime.channel_inflight_cap == 8


def test_channel_inflight_cap_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """AGENTOS_CHANNEL_INFLIGHT_CAP=12 sets task_runtime.channel_inflight_cap to 12."""
    monkeypatch.setenv("AGENTOS_CHANNEL_INFLIGHT_CAP", "12")
    config = GatewayConfig()
    assert config.task_runtime.channel_inflight_cap == 12


def test_channel_inflight_cap_invalid_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Non-integer AGENTOS_CHANNEL_INFLIGHT_CAP falls back to default 8 with a warning."""
    monkeypatch.setenv("AGENTOS_CHANNEL_INFLIGHT_CAP", "bad")
    import logging

    with caplog.at_level(logging.WARNING):
        config = GatewayConfig()

    assert config.task_runtime.channel_inflight_cap == 8
    assert any(
        "AGENTOS_CHANNEL_INFLIGHT_CAP" in record.message
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )


def test_zero_env_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AGENTOS_TASK_MAX_CONCURRENCY=0 falls back to default 4 with a warning."""
    monkeypatch.setenv("AGENTOS_TASK_MAX_CONCURRENCY", "0")
    import logging

    with caplog.at_level(logging.WARNING):
        config = GatewayConfig()

    assert config.task_runtime.max_concurrency == 4
    assert any(
        "AGENTOS_TASK_MAX_CONCURRENCY" in record.message
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )


def test_negative_env_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AGENTOS_TASK_MAX_CONCURRENCY=-5 falls back to default 4 with a warning."""
    monkeypatch.setenv("AGENTOS_TASK_MAX_CONCURRENCY", "-5")
    import logging

    with caplog.at_level(logging.WARNING):
        config = GatewayConfig()

    assert config.task_runtime.max_concurrency == 4
    assert any(
        "AGENTOS_TASK_MAX_CONCURRENCY" in record.message
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )


def test_channel_zero_env_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-M5: AGENTOS_CHANNEL_INFLIGHT_CAP=0 falls back to default 8 with a warning."""
    monkeypatch.setenv("AGENTOS_CHANNEL_INFLIGHT_CAP", "0")
    import logging

    with caplog.at_level(logging.WARNING):
        config = GatewayConfig()

    assert config.task_runtime.channel_inflight_cap == 8
    assert any(
        "AGENTOS_CHANNEL_INFLIGHT_CAP" in record.message
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )
