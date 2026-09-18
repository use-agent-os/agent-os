from __future__ import annotations

import pytest
import structlog.testing

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
        r"del C:\tmp\file.txt",
        r"DEL C:\tmp\file.txt",
        r"dEl C:\tmp\file.txt",
        r"rmdir /s /q C:\tmp\folder",
        r"RMDIR /S /Q C:\tmp\folder",
        r"Remove-Item C:\tmp\stale.txt",
        r"remove-item C:\tmp\stale.txt",
        r"REMOVE-ITEM C:\tmp\stale.txt",
        r"rd /s /q C:\tmp\folder",
        r"RD /S /Q C:\tmp\folder",
        r"erase C:\tmp\file.txt",
        r"ERASE /F C:\tmp\file.txt",
        r"echo 1 && rd /s /q C:\tmp\folder",
        r"echo 1; erase C:\tmp\file.txt",
        r"echo 1 | rd C:\tmp\folder",
        r"git push origin main --force",
        r"git push --force",
        r"git   push   origin   feature   --force",
        r"Format-Volume -DriveLetter D",
        r"Clear-Disk -Number 1",
        r"Stop-Computer -Force",
        r"Restart-Computer -Force",
        # Shell wrappers (cmd /c, powershell -c) and multiline commands for del/rd/rmdir/erase
        r"cmd /c del C:\tmp\x",
        r"cmd /c rd /s /q C:\tmp",
        r"cmd.exe /c del C:\tmp\x",
        r"cmd.exe /c erase C:\tmp\x",
        r"powershell -c del C:\tmp\x",
        r"powershell -c rd C:\tmp",
        "echo 1\ndel C:\\tmp\\x",
        "echo 1\nrd /s /q C:\\tmp",
        "echo 1\nrmdir /s /q C:\\d",
        "echo 1\nerase C:\\tmp\\x",
    ],
)
def test_windows_destructive_commands_are_denied(command: str) -> None:
    result = shell_policy.SafeBinPolicy.from_env().check(command)

    assert result.allowed is False
    assert result.needs_approval is False
    assert "blocked by policy" in result.reason


@pytest.mark.parametrize(
    "command",
    [
        r"mkdir rd",
        r"cd rd",
        r"ls rd",
        r"dir rd",
        r"git checkout -b rd-feature",
        r"git branch rd",
        r"cat erase.txt",
        r"type erase.txt",
        r"python erase.py",
        r'git commit -m "erase old cache"',
        r"npm run erase-cache",
        r"echo 3rd party",
        r"curl -o out.bin https://cdn.example.com/rd",
        r"kubectl get pods -n rd",
        r"helm install rd ./chart",
        r"psql -c 'SELECT * FROM rd'",
        r"python train.py --dataset erase-bench",
        r"git log --grep rd",
        r"cd C:\data\rd",
        r"echo rd",
        r"npm run build && npm test",
    ],
)
def test_windows_anchored_rd_erase_negative_cases_allowed(command: str) -> None:
    result = shell_policy.SafeBinPolicy.from_env().check(command)

    assert result.allowed is True
    assert result.needs_approval is False


def test_windows_deny_env_overrides_platform_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_SAFE_BIN_DENY", r"\bcustom-block\b")

    policy = shell_policy.SafeBinPolicy.from_env()

    custom = policy.check("custom-block")
    default_del = policy.check(r"del C:\tmp\file.txt")
    assert custom.allowed is False
    assert default_del.allowed is True
    assert default_del.needs_approval is False


def test_windows_custom_warn_env_sets_warnlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_SAFE_BIN_WARN", r"\bcustom-warn\b")

    result = shell_policy.SafeBinPolicy.from_env().check("custom-warn")

    assert result.allowed is True
    assert result.needs_approval is True
    assert "requires approval" in result.reason


def test_windows_empty_warn_env_preserves_default_denylist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTOS_SAFE_BIN_WARN", "")

    result = shell_policy.SafeBinPolicy.from_env().check(r"Format-Volume -DriveLetter D")

    assert result.allowed is False
    assert result.needs_approval is False


def test_legacy_shell_denylist_warns_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_SHELL_DENYLIST", r"\blegacy-block\b")

    with structlog.testing.capture_logs() as captured:
        first = shell_policy.SafeBinPolicy.from_env()
        second = shell_policy.SafeBinPolicy.from_env()

    warnings = [
        event for event in captured if event["event"] == "shell_policy.legacy_deny_env_detected"
    ]
    assert len(warnings) == 1
    assert first.check("legacy-block").allowed is False
    assert second.check("legacy-block").allowed is False


@pytest.mark.parametrize(
    "command",
    [
        r"rm C:\tmp\file.txt",
        r"RM C:\tmp\file.txt",
        r"ri -Recurse -Force C:\tmp\folder",
        r"RI -Recurse -Force C:\tmp\folder",
        r"rm.exe -rf C:\tmp\folder",
        r"echo 1 && rm C:\tmp\file.txt",
        r"echo 1; ri C:\tmp\folder",
        r"echo 1 | rm C:\tmp\file.txt",
        r"cmd /c rm C:\tmp\x",
        r"cmd.exe /c ri /s /q C:\tmp",
        r"powershell -c rm C:\tmp\x",
        r"powershell -c ri C:\tmp",
        "echo 1\nrm C:\\tmp\\x",
        "echo 1\nri /s /q C:\\tmp",
    ],
)
def test_windows_denies_rm_and_ri_remove_item_aliases(command: str) -> None:
    """`rm` and `ri` are PowerShell's other two built-in Remove-Item aliases (#2100, #1964)."""
    result = shell_policy.SafeBinPolicy.from_env().check(command)

    assert result.allowed is False
    assert result.needs_approval is False
    assert "blocked by policy" in result.reason


@pytest.mark.parametrize(
    "command",
    [
        r"mkdir rm",
        r"cd rm",
        r"cd ri",
        r"docker run --rm image",
        r"git rm --cached file.txt",
        r"git checkout -b rm-feature",
        r"git branch rm",
        r"git branch ri",
        r"npm run rm-cache",
        r"npm run build:ri",
        r'git commit -m "rm old cache"',
        r"cat rm.txt",
        r"type ri.md",
        r"ripgrep --version",
        r"rg --files",
        r"terraform apply -target=module.ri",
        r"kubectl get pods -n ri",
        r"curl -o out.bin https://cdn.example.com/rm",
        r"cd C:\data\rm",
        r"echo rm",
        r"echo ri",
        r"echo ring the bell",
        r"npm run build && npm test",
    ],
)
def test_windows_anchored_rm_ri_negative_cases_allowed(command: str) -> None:
    result = shell_policy.SafeBinPolicy.from_env().check(command)

    assert result.allowed is True
    assert result.needs_approval is False


@pytest.mark.parametrize(
    "command",
    [
        r'powershell -c "rd /s /q C:\x"',
        r'powershell -c "erase C:\x"',
        r'powershell -c "rm -r C:\x"',
        r"powershell -Command 'ri -Recurse C:\x'",
        r'powershell -ExecutionPolicy Bypass -Command "rm C:\x"',
        r'powershell -ep Bypass -c "rm C:\x"',
        r'powershell -ConfigurationName default -Command "rm C:\x"',
        r'cmd /c "rm C:\x"',
        r'pwsh.exe -NoProfile -Command "rd -Force C:\x"',
    ],
)

def test_windows_quoted_wrapper_payload_is_denied(command: str) -> None:
    """A wrapper payload is usually quoted; every anchored alias must see through it."""
    result = shell_policy.SafeBinPolicy.from_env().check(command)

    assert result.allowed is False


@pytest.mark.parametrize(
    "command",
    [
        r"rd-report.ps1",
        r"erase-log-viewer.exe --tail",
        r"rm-cache.cmd",
        r"ri-report.exe --out x",
        r".\rm-old-builds.ps1",
        r"pwsh -File rm.ps1",
    ],
)
def test_windows_script_named_like_an_alias_is_not_denied(command: str) -> None:
    """A script that merely starts with an alias name is not an invocation of it."""
    result = shell_policy.SafeBinPolicy.from_env().check(command)

    assert result.allowed is True
    assert result.needs_approval is False


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
        "reboot",
        "halt",
    ],
)
def test_windows_enforces_the_shared_catastrophic_denylist(command: str) -> None:
    """Windows must extend `DEFAULT_DENYLIST`, not replace it (#1964).

    These platform-independent hazards also reach a Windows host through
    git-bash, MSYS, Cygwin, or WSL, and `shutdown` is a native Windows binary
    too -- none of them were gated on Windows before this fix.
    """
    result = shell_policy.SafeBinPolicy.from_env().check(command)

    assert result.allowed is False
    assert "blocked by policy" in result.reason


def test_windows_denylist_is_a_superset_of_the_posix_denylist() -> None:
    denylist = shell_policy.SafeBinPolicy.from_env().denylist

    assert set(shell_policy.DEFAULT_DENYLIST).issubset(denylist)
    assert set(shell_policy.DEFAULT_DENYLIST_WIN).issubset(denylist)


def test_posix_defaults_are_unaffected_by_the_windows_union(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shell_policy.os, "name", "posix")

    policy = shell_policy.SafeBinPolicy.from_env()

    assert policy.denylist == shell_policy.DEFAULT_DENYLIST
    assert policy.warnlist == shell_policy.DEFAULT_WARNLIST

    # rm stays at the warn tier on POSIX; only Windows hard-denies it.
    rm_result = policy.check(r"rm -rf ~/project")
    assert rm_result.allowed is True
    assert rm_result.needs_approval is True

    # Windows-only spellings stay out of the POSIX list entirely.
    assert policy.check(r"del C:\tmp\file.txt").allowed is True
    assert policy.check(r"ri -Recurse -Force C:\data").allowed is True
