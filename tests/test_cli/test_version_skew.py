"""Version-skew policy: warn when gateway older, refuse when gateway newer."""

from __future__ import annotations

import pytest

from agentos.cli.version_skew import (
    ALLOW_SKEW_ENV,
    SkewReporter,
    VersionSkewError,
    evaluate_skew,
)


def test_evaluate_skew_directions() -> None:
    assert evaluate_skew(cli_version="2026.7.19", gateway_version="2026.7.18") == "gateway_older"
    assert evaluate_skew(cli_version="2026.7.18", gateway_version="2026.7.19") == "gateway_newer"
    assert evaluate_skew(cli_version="2026.7.18", gateway_version="2026.7.18") is None
    assert evaluate_skew(cli_version="2026.7.18", gateway_version=None) is None


def test_gateway_older_warns_not_blocks(capsys: pytest.CaptureFixture[str]) -> None:
    reporter = SkewReporter()
    reporter.check(cli_version="2026.7.19", gateway_version="2026.7.18")
    err = capsys.readouterr().err
    assert "OLDER" in err
    assert "gateway restart" in err


def test_gateway_older_warns_once_per_invocation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    reporter = SkewReporter()
    reporter.check(cli_version="2026.7.19", gateway_version="2026.7.18")
    reporter.check(cli_version="2026.7.19", gateway_version="2026.7.18")
    assert capsys.readouterr().err.count("OLDER") == 1


def test_gateway_newer_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ALLOW_SKEW_ENV, raising=False)
    reporter = SkewReporter()
    with pytest.raises(VersionSkewError) as exc:
        reporter.check(cli_version="2026.7.18", gateway_version="2026.7.19")
    assert "NEWER" in str(exc.value)
    assert ALLOW_SKEW_ENV in str(exc.value)


def test_gateway_newer_escape_hatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(ALLOW_SKEW_ENV, "1")
    reporter = SkewReporter()
    reporter.check(cli_version="2026.7.18", gateway_version="2026.7.19")
    assert "proceeding because" in capsys.readouterr().err


def test_equal_versions_silent(capsys: pytest.CaptureFixture[str]) -> None:
    SkewReporter().check(cli_version="2026.7.18", gateway_version="2026.7.18")
    assert capsys.readouterr().err == ""
