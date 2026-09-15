"""Regression tests for #1964: the Windows denylist extends the shared one.

``SafeBinPolicy.from_env`` picked *one* default list by platform, so on
Windows the catastrophic ``DEFAULT_DENYLIST`` patterns (``rm -rf /``,
``mkfs``, ``dd if=``, ``shutdown``, …) were never consulted. PowerShell's
``rm`` and ``ri`` aliases for ``Remove-Item`` were also missing from
``DEFAULT_DENYLIST_WIN`` while ``del``/``rd``/``erase``/``Remove-Item`` were
denied — and with an empty Windows warnlist, an unmatched spelling ran with
no gate at all.
"""

from __future__ import annotations

import pytest

from agentos.tools.builtin import shell_policy


@pytest.fixture(autouse=True)
def _windows_policy_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(shell_policy.os, "name", "nt")
    monkeypatch.delenv("AGENTOS_SAFE_BIN_DENY", raising=False)
    monkeypatch.delenv("AGENTOS_SAFE_BIN_ALLOW", raising=False)
    monkeypatch.delenv("AGENTOS_SAFE_BIN_WARN", raising=False)
    monkeypatch.delenv("AGENTOS_SHELL_DENYLIST", raising=False)
    monkeypatch.setattr(shell_policy, "_LEGACY_ENV_WARNED", False)


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "rm -rf /*",
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
        ":(){ :|:& };:",
        "echo x > /dev/sda",
        "chmod -R 777 /",
        "shutdown -h now",
        "shutdown /s /t 0",
        "reboot",
    ],
)
def test_windows_enforces_the_shared_catastrophic_denylist(command: str) -> None:
    result = shell_policy.SafeBinPolicy.from_env().check(command)

    assert result.allowed is False
    assert "blocked by policy" in result.reason


@pytest.mark.parametrize(
    "command",
    [
        r"rm -Recurse -Force C:\data",
        r"rm C:\data\x",
        r"RM -r C:\data",
        r"rm -rf C:/work/project",
        r"ri -Recurse -Force C:\data",
        r"ri C:\data\x",
        r"echo 1 && rm -r C:\data",
        r"echo 1; ri C:\data\x",
        r"echo 1 | rm C:\data\x",
        r"cmd /c rm C:\tmp\x",
        r"powershell -c rm -Recurse C:\tmp",
        r"pwsh -NoProfile -c ri C:\tmp\x",
        "echo 1\nrm -r C:\\tmp",
        "echo 1\nri C:\\tmp\\x",
        # The wrapper's payload is usually quoted.
        r'powershell -c "rm -r C:\x"',
        r"powershell -Command 'ri -Recurse C:\x'",
        r'cmd /c "rm C:\x"',
        r'pwsh.exe -NoProfile -Command "rm -Force C:\x"',
        r"rm.exe -rf C:\data",
        r"rm;",
    ],
)
def test_windows_denies_rm_and_ri_remove_item_aliases(command: str) -> None:
    result = shell_policy.SafeBinPolicy.from_env().check(command)

    assert result.allowed is False
    assert result.needs_approval is False


@pytest.mark.parametrize(
    "command",
    [
        r"npm run rm-cache",
        r"git rm --cached file.txt",
        r"chmod rm",
        r"cat rm.txt",
        r"type ri.txt",
        r"terraform ri",
        r"dotnet ri",
        r"cd C:\data\rm",
        r"echo rm",
        r"echo ri",
        r"python rm.py",
        r'git commit -m "rm old cache"',
        r"ripgrep pattern",
        r"rg pattern",
        r"npm run build && npm test",
        # Scripts that merely start with the alias, at command position.
        r"pwsh -File rm.ps1",
        r"rm-cache.cmd",
        r".\rm-old-builds.ps1",
        r"ri-report.exe --out x",
        r'curl -d "{rd: 1, rm: 2}" https://example.com',
    ],
)
def test_windows_anchored_rm_ri_negative_cases_allowed(command: str) -> None:
    result = shell_policy.SafeBinPolicy.from_env().check(command)

    assert result.allowed is True
    assert result.needs_approval is False


def test_posix_default_denylist_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell_policy.os, "name", "posix")

    policy = shell_policy.SafeBinPolicy.from_env()

    assert policy.denylist == shell_policy.DEFAULT_DENYLIST
    # ``rm`` stays at the warn tier on POSIX; only Windows hard-denies it.
    result = policy.check("rm build/artifact.o")
    assert result.allowed is True
    assert result.needs_approval is True


@pytest.mark.parametrize(
    "command",
    [
        r'cmd /c "rd /s /q C:\x"',
        r'powershell -c "erase C:\x"',
        r"pwsh -NoProfile -Command 'rd C:\x'",
    ],
)
def test_windows_quoted_wrapper_payload_reaches_rd_and_erase_too(command: str) -> None:
    """The quote allowance in ``_WIN_CMD_PREFIX`` benefits every anchored alias."""
    result = shell_policy.SafeBinPolicy.from_env().check(command)

    assert result.allowed is False


def test_windows_deny_env_still_replaces_both_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_SAFE_BIN_DENY", r"\bcustom-block\b")

    policy = shell_policy.SafeBinPolicy.from_env()

    assert policy.check("custom-block").allowed is False
    assert policy.check("shutdown -h now").allowed is True
    assert policy.check(r"rm C:\data\x").allowed is True
