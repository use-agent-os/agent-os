"""The sensitive-path scanner must return a verdict, never raise (#1503).

``Path("~\\.aws\\credentials").expanduser()`` on POSIX reads the whole
backslash tail as a user name, asks ``pwd.getpwnam`` for it, and raises
``RuntimeError: Could not determine home directory``. The same happens to any
``~`` in an environment with no resolvable home — a minimal container, a
headless service account. Every expansion in ``sensitive_paths`` was guarded
except the ones exercised here, and an exception escaping a security scan is
worse than a wrong verdict: it turns the check into a tool crash, which is a
*fail-open* outcome for the caller that was supposed to be blocked.

Three entry points reach an expansion, and all three are driven by model
input: the path token itself, the surrounding command text, and the ``cwd``
that ``exec_command(workdir=…)`` hands to ``sensitive_target_in_command``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentos.sandbox.sensitive_paths import (
    sensitive_path_in_text,
    sensitive_path_marker,
    sensitive_target_in_command,
)


@pytest.fixture
def no_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """An environment where the home directory cannot be determined."""

    def _boom() -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", staticmethod(_boom))
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)


# ── The reported token shape ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        (r"~\.aws\credentials", "~/.aws"),
        (r"~\.ssh\id_rsa", "~/.ssh"),
        (r"~\.config\gcloud\credentials.db", "~/.config/gcloud"),
    ],
)
def test_backslash_tilde_tokens_return_a_verdict(token: str, expected: str) -> None:
    """Fails without the fix: RuntimeError out of the scanner."""
    assert sensitive_path_marker(token) == expected


def test_backslash_tilde_in_command_text_returns_a_verdict() -> None:
    assert sensitive_path_in_text(r"cat ~\.aws\credentials") == "~/.aws"
    assert sensitive_path_in_text(r"type ~\.ssh\id_rsa") == "~/.ssh"


def test_an_unexpandable_tilde_token_is_not_reported_as_sensitive() -> None:
    """A token pathlib cannot expand is still judged, not merely survived."""
    assert sensitive_path_marker(r"~\projects\notes.txt") is None


# ── The cwd the shell tool hands in (exec_command workdir=…) ────────────────


def test_an_unexpandable_cwd_does_not_crash_the_command_scan() -> None:
    """Fails without the fix.

    ``shell._check_exec_approval`` calls ``sensitive_target_in_command(...,
    cwd=workdir)`` with the workdir the model supplied, so a ``~\\…`` workdir
    took the whole hard block down with a RuntimeError before any verdict was
    reached.
    """
    assert sensitive_target_in_command("ls -la", cwd=r"~\projects\app") is None


def test_a_root_wipe_is_still_blocked_under_an_unexpandable_cwd() -> None:
    """The verdict has to survive the unexpandable anchor, not just the call."""
    assert sensitive_target_in_command("rm -rf /", cwd=r"~\projects\app") == "/"


def test_a_sensitive_target_is_still_blocked_under_an_unexpandable_cwd() -> None:
    assert sensitive_target_in_command("rm -rf ~/.ssh/id_rsa", cwd=r"~\x\y") == "~/.ssh"


# ── Indeterminate home: minimal container / headless service account ────────


@pytest.mark.usefixtures("no_home")
def test_marker_survives_an_indeterminate_home() -> None:
    """Fails without the fix: ``Path.home()`` raised out of the candidates."""
    assert sensitive_path_marker("~/.aws/credentials") == "~/.aws"
    assert sensitive_path_marker("~/.ssh/id_rsa") == "~/.ssh"
    assert sensitive_path_marker("/etc/passwd") == "/etc"
    assert sensitive_path_marker("relative/file.txt") is None


@pytest.mark.usefixtures("no_home")
def test_command_scan_survives_an_indeterminate_home() -> None:
    assert sensitive_target_in_command("rm -rf ~/.ssh/id_rsa") == "~/.ssh"
    assert sensitive_target_in_command("rm -rf /") == "/"
    assert sensitive_target_in_command("rm -rf build/") is None


@pytest.mark.usefixtures("no_home")
def test_command_scan_survives_an_indeterminate_home_and_a_tilde_cwd() -> None:
    """Both failure modes at once — the shape a container actually hits."""
    assert sensitive_target_in_command("rm -rf /", cwd="~/workspace") == "/"


# ── Both simulated platforms, so neither CI job skips this ─────────────────


@pytest.mark.parametrize("platform_name", ["nt", "posix"])
def test_verdicts_hold_on_both_platforms(
    platform_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "name", platform_name)

    assert sensitive_path_marker(r"~\.ssh\id_rsa") == "~/.ssh"
    assert sensitive_path_marker("/etc/shadow") == "/etc"
    assert sensitive_target_in_command("rm -rf /", cwd=r"~\x") == "/"


# ── Guards: the ordinary paths must be unaffected ──────────────────────────


def test_ordinary_verdicts_are_unchanged() -> None:
    """Guard: passes either way by design. Falling back to an unexpanded path
    must not weaken a verdict that never needed the fallback."""
    assert sensitive_path_marker("~/.ssh/id_rsa") == "~/.ssh"
    assert sensitive_path_marker("/etc/passwd") == "/etc"
    assert sensitive_path_marker(str(Path.home() / ".aws" / "credentials")) == "~/.aws"
    assert sensitive_path_marker("notes.txt") is None
    assert sensitive_target_in_command("rm -rf /etc") == "/etc"
    assert sensitive_target_in_command("echo hello") is None
