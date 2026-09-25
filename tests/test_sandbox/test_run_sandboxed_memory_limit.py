"""Tests for memory limit enforcement and exit classification in ``run_sandboxed``."""

from __future__ import annotations

import sys

import pytest

from agentos.safety import sandbox as sandbox_mod
from agentos.safety.sandbox import (
    REASON_MEMORY_LIMIT,
    REASON_OK,
    SandboxLimits,
    run_sandboxed,
)


def test_rlimit_as_memory_exhaustion_reports_memory_limit_reason() -> None:
    if not sandbox_mod.HAS_RESOURCE:  # pragma: no cover — Windows CI only
        pytest.skip("host has no resource module")

    # Attempt to allocate 200MB under a 64MB memory cap.
    result = run_sandboxed(
        [sys.executable, "-c", "x = bytearray(200 * 1024 * 1024)"],
        SandboxLimits(memory_mb=64, wall_seconds=5),
    )

    assert result.returncode != 0
    assert "MemoryError" in result.stderr
    assert result.reason == REASON_MEMORY_LIMIT


def test_non_zero_exit_unrelated_error_reports_ok_reason() -> None:
    result = run_sandboxed(
        [sys.executable, "-c", "raise ValueError('regular error')"],
        SandboxLimits(wall_seconds=5),
    )

    assert result.returncode != 0
    assert "ValueError" in result.stderr
    assert result.reason == REASON_OK


@pytest.mark.parametrize(
    ("err_snippet", "expected_reason"),
    [
        ("MemoryError: unable to allocate array", REASON_MEMORY_LIMIT),
        ("fatal: out of memory", REASON_MEMORY_LIMIT),
        ("OSError: [Errno 12] Cannot allocate memory", REASON_MEMORY_LIMIT),
        ("terminate called after throwing an instance of 'std::bad_alloc'", REASON_MEMORY_LIMIT),
        ("RuntimeError: something else failed", REASON_OK),
    ],
)
def test_stderr_allocation_error_markers(err_snippet: str, expected_reason: str) -> None:
    result = run_sandboxed(
        [
            sys.executable,
            "-c",
            f"import sys; sys.stderr.write({err_snippet!r}); sys.exit(1)",
        ],
        SandboxLimits(wall_seconds=5),
    )

    assert result.returncode == 1
    assert result.reason == expected_reason


def test_signal_exit_reasons() -> None:
    # Exit with SIGSEGV (11) should map to REASON_MEMORY_LIMIT
    result = run_sandboxed(
        [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGSEGV)"],
        SandboxLimits(wall_seconds=5),
    )
    if sys.platform.startswith("win"):  # pragma: no cover
        pytest.skip("signals not supported on Windows")

    assert result.returncode == -11
    assert result.reason == REASON_MEMORY_LIMIT
