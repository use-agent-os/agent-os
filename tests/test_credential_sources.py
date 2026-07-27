"""Tests for discovering credentials that already exist elsewhere.

The two invariants worth defending: probing must never read a secret, and
nothing must import one without being asked to.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from agentos import credential_sources
from agentos.credential_sources import CredentialSource


@pytest.fixture(autouse=True)
def clear_probe_cache():
    credential_sources.reset_probe_cache()
    yield
    credential_sources.reset_probe_cache()


def _source(**kwargs) -> CredentialSource:
    base = {
        "id": "fake",
        "label": "Fake CLI",
        "provides": ("FAKE_TOKEN",),
        "hint": "Run `fake login`.",
        "probe": lambda: True,
        "read": lambda: "value-from-source",
    }
    base.update(kwargs)
    return CredentialSource(**base)  # type: ignore[arg-type]


class TestRegistry:
    def test_finds_sources_by_variable_name(self) -> None:
        assert [s.id for s in credential_sources.sources_for("GITHUB_TOKEN")] == ["gh_cli"]
        assert [s.id for s in credential_sources.sources_for("GH_TOKEN")] == ["gh_cli"]

    def test_unknown_names_have_no_source(self) -> None:
        assert credential_sources.sources_for("SOMETHING_ELSE") == []
        assert credential_sources.available_for("SOMETHING_ELSE") is None


class TestProbing:
    def test_probe_result_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A listing asks about every unset variable; without caching that is one
        # subprocess per row per refresh.
        calls: list[int] = []

        def counting_probe() -> bool:
            calls.append(1)
            return True

        source = _source(probe=counting_probe)
        assert credential_sources.is_available(source) is True
        assert credential_sources.is_available(source) is True
        assert len(calls) == 1

    def test_refresh_bypasses_the_cache(self) -> None:
        calls: list[int] = []
        source = _source(probe=lambda: (calls.append(1), True)[1])
        credential_sources.is_available(source)
        credential_sources.is_available(source, refresh=True)
        assert len(calls) == 2

    def test_a_probe_that_raises_reports_unavailable(self) -> None:
        def broken() -> bool:
            raise RuntimeError("gh exploded")

        # A broken external CLI must not take down an env listing.
        assert credential_sources.is_available(_source(probe=broken)) is False

    def test_available_for_skips_unusable_sources(self) -> None:
        assert credential_sources.available_for("FAKE_TOKEN") is None


class TestGitHubCliSource:
    def test_probe_uses_a_status_check_not_a_token_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The whole probe/read split exists so a listing can say "available"
        # without ever fetching a secret.
        seen: list[list[str]] = []

        def fake_run(argv, **kwargs):
            seen.append(argv)
            return SimpleNamespace(returncode=0, stdout="Logged in to github.com\n")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(credential_sources.shutil, "which", lambda _: "/usr/bin/gh")

        assert credential_sources.is_available(credential_sources.GH_CLI) is True
        assert seen == [["gh", "auth", "status"]]
        assert not any("token" in arg for argv in seen for arg in argv)

    def test_absent_cli_is_unavailable_without_running_anything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode(*args, **kwargs):
            raise AssertionError("must not shell out when gh is not installed")

        monkeypatch.setattr(credential_sources.shutil, "which", lambda _: None)
        monkeypatch.setattr(subprocess, "run", explode)
        # which() short-circuits, so the assertion is that `explode` never ran.
        assert credential_sources.is_available(credential_sources.GH_CLI) is False

    def test_read_returns_the_first_line_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(credential_sources.shutil, "which", lambda _: "/usr/bin/gh")

        def fake_run(argv, **kwargs):
            if argv[1:] == ["auth", "status"]:
                return SimpleNamespace(returncode=0, stdout="ok")
            return SimpleNamespace(returncode=0, stdout="gho_thetoken\nsome trailing noise\n")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert credential_sources.read_from("GITHUB_TOKEN", "gh_cli") == "gho_thetoken"

    def test_a_timeout_is_not_an_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(credential_sources.shutil, "which", lambda _: "/usr/bin/gh")

        def timing_out(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="gh", timeout=3)

        monkeypatch.setattr(subprocess, "run", timing_out)
        assert credential_sources.is_available(credential_sources.GH_CLI, refresh=True) is False


class TestReadFrom:
    def test_unknown_source_for_a_name_is_a_lookup_error(self) -> None:
        with pytest.raises(LookupError, match="No credential source"):
            credential_sources.read_from("GITHUB_TOKEN", "not_a_source")

    def test_unusable_source_reports_how_to_fix_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(credential_sources.shutil, "which", lambda _: None)
        with pytest.raises(RuntimeError, match="gh auth login"):
            credential_sources.read_from("GITHUB_TOKEN", "gh_cli")

    def test_a_source_returning_nothing_is_an_error_not_an_empty_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(credential_sources.shutil, "which", lambda _: "/usr/bin/gh")

        def fake_run(argv, **kwargs):
            if argv[1:] == ["auth", "status"]:
                return SimpleNamespace(returncode=0, stdout="ok")
            return SimpleNamespace(returncode=0, stdout="   \n")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(RuntimeError, match="returned no value"):
            credential_sources.read_from("GITHUB_TOKEN", "gh_cli")


class TestNothingImportsItself:
    def test_the_module_exposes_no_automatic_hydration(self) -> None:
        # Discovery is a report, not an action. Anything that moved credentials
        # on its own would make "AgentOS took my GitHub token" a real sentence.
        exported = {name for name in dir(credential_sources) if not name.startswith("_")}
        assert not exported & {"hydrate", "autoload", "seed", "apply", "inject"}
