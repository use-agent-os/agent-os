"""Issue #2100: the Windows denylist was missing rm and ri.

PowerShell ships exactly six built-in aliases for ``Remove-Item`` — ``del``,
``erase``, ``rd``, ``ri``, ``rm`` and ``rmdir``. ``DEFAULT_DENYLIST_WIN``
carried patterns for four of them plus ``Remove-Item`` itself, and none for
``rm`` or ``ri``. ``DEFAULT_WARNLIST_WIN`` is empty, so there was no two-step
approval to fall through to either: a deletion spelled ``rm`` or ``ri`` ran
unblocked and unconfirmed.

Two adjacent holes are covered here as well, both verified against ``main``:

* ``SafeBinPolicy.from_env`` *replaced* the shared denylist on Windows instead
  of extending it, so ``shutdown`` (a native Windows binary), ``rm -rf /``,
  ``mkfs``, ``dd if=``, ``chmod -R 777 /`` and the fork bomb were ungated
  there — all reachable on a Windows host through git-bash, MSYS, Cygwin or
  WSL.
* The command-position anchor recognised only ``^`` and ``; & | \\n``, so a
  command inside a PowerShell block or subexpression slipped past it. That was
  already true of ``rd`` and ``erase`` before ``rm``/``ri`` existed:
  ``powershell -c "if ($true) { rd /s C:\\x }"`` was allowed.

The anchoring matters in both directions. A bare ``\\brm\\b`` would fire inside
``docker run --rm`` and ``git rm --cached``, and ``\\bri\\b`` inside almost any
argument, so a denylist that noisy gets switched off.
"""

from __future__ import annotations

import pytest

from agentos.tools.builtin import shell_policy

#: PowerShell's complete set of built-in Remove-Item aliases.
REMOVE_ITEM_ALIASES = ("del", "erase", "rd", "ri", "rm", "rmdir")


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(shell_policy.os, "name", "nt")
    for name in (
        "AGENTOS_SAFE_BIN_DENY",
        "AGENTOS_SAFE_BIN_ALLOW",
        "AGENTOS_SAFE_BIN_WARN",
        "AGENTOS_SHELL_DENYLIST",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(shell_policy, "_LEGACY_ENV_WARNED", False)
    return shell_policy.SafeBinPolicy.from_env()


@pytest.fixture
def posix(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(shell_policy.os, "name", "posix")
    for name in (
        "AGENTOS_SAFE_BIN_DENY",
        "AGENTOS_SAFE_BIN_ALLOW",
        "AGENTOS_SAFE_BIN_WARN",
        "AGENTOS_SHELL_DENYLIST",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(shell_policy, "_LEGACY_ENV_WARNED", False)
    return shell_policy.SafeBinPolicy.from_env()


def assert_denied(policy, command: str) -> None:
    result = policy.check(command)
    assert result.allowed is False, f"expected denial: {command!r}"
    assert result.needs_approval is False
    assert "blocked by policy" in result.reason


def assert_allowed(policy, command: str) -> None:
    result = policy.check(command)
    assert result.allowed is True, f"expected allowed: {command!r} ({result.reason})"
    assert result.needs_approval is False


# ── the reported gap ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "rm important_file.txt",
        r"rm -r -Force C:\data",
        r"ri -Recurse -Force C:\data",
        "ri data.txt",
    ],
)
def test_rm_and_ri_are_denied(windows, command: str) -> None:
    """The issue's own reproduction."""
    assert_denied(windows, command)


@pytest.mark.parametrize("alias", REMOVE_ITEM_ALIASES)
def test_every_remove_item_alias_is_denied(windows, alias: str) -> None:
    """The set is closed: all six of PowerShell's aliases, not four of six.

    Parametrized over the alias list itself so adding a spelling to the
    constant without a pattern fails here rather than shipping.
    """
    assert_denied(windows, rf"{alias} C:\tmp\file.txt")


@pytest.mark.parametrize("alias", REMOVE_ITEM_ALIASES + ("Remove-Item",))
@pytest.mark.parametrize("case", ["lower", "upper", "title"])
def test_aliases_are_case_insensitive(windows, alias: str, case: str) -> None:
    """PowerShell is case-insensitive, so the denylist must be too — the
    Windows branch passes re.IGNORECASE for exactly this reason."""
    spelled = {"lower": alias.lower(), "upper": alias.upper(), "title": alias.title()}[case]
    assert_denied(windows, rf"{spelled} C:\tmp\file.txt")


# ── command position ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        r"rm C:\x",
        r"   rm C:\x",
        r"echo 1 && rm C:\x",
        r"echo 1 & rm C:\x",
        r"echo 1; rm C:\x",
        r"echo 1 | rm C:\x",
        "echo 1\nrm C:\\x",
    ],
    ids=["start", "indented", "and-and", "and", "semicolon", "pipe", "newline"],
)
def test_an_alias_is_caught_at_every_command_position(windows, command: str) -> None:
    assert_denied(windows, command)


@pytest.mark.parametrize(
    "command",
    [
        r"cmd /c rm C:\x",
        r"cmd.exe /c rm C:\x",
        r"cmd /k ri C:\x",
        r"powershell -c rm C:\x",
        r"pwsh -c ri C:\x",
        r"powershell.exe -NoProfile -Command rm C:\x",
    ],
)
def test_an_alias_is_caught_through_a_wrapper(windows, command: str) -> None:
    assert_denied(windows, command)


@pytest.mark.parametrize(
    "command",
    [
        'powershell -c "rm -r C:\\x"',
        "powershell -c 'ri C:\\x'",
        'cmd /c "rm C:\\x"',
    ],
)
def test_a_quoted_wrapper_payload_is_caught(windows, command: str) -> None:
    """``powershell -c "rm -r C:\\x"`` is how cmd and subprocess hand
    PowerShell a command string, so the opening quote must not hide it."""
    assert_denied(windows, command)


@pytest.mark.parametrize(
    "command",
    [
        'powershell -c "if (Test-Path C:\\x) { rm -r C:\\x }"',
        'powershell -c "(rm C:\\x)"',
        'powershell -c "1..3 | % { ri C:\\x }"',
        'powershell -c "if ($true) { rd /s C:\\x }"',
        'powershell -c "foreach ($f in $files) { erase $f }"',
        r"cmd /c (del C:\x)",
    ],
    ids=["if-block", "subexpression", "foreach-block", "rd-block", "erase-block", "cmd-parens"],
)
def test_an_alias_inside_a_block_or_subexpression_is_caught(windows, command: str) -> None:
    """``(`` and ``{`` open a command position too.

    An anchor that recognises only ``^`` and ``; & | \\n`` misses every one of
    these. The ``rd`` and ``erase`` cases were already reachable on ``main``
    before ``rm``/``ri`` were added, so this is a fix to the existing anchor
    rather than new-alias housekeeping.
    """
    assert_denied(windows, command)


# ── what must stay allowed ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "docker run --rm ubuntu",
        "docker run --rm -it alpine sh",
        "git rm --cached file.txt",
        "git rm -r --cached .",
        "npm run rm-cache",
        r".\rm-cache.cmd",
        r".\rd-report.ps1",
        "python ri_helper.py",
        "kubectl get pods -n ri",
        "kubectl get pods -n rm",
        r"cd C:\data\rd",
        r"cd C:\data\rm",
        "echo rm",
        "echo ri",
        "echo 3rd party",
        "npm run erase-cache",
        "git checkout -b rd-feature",
        "helm install ri ./chart",
    ],
)
def test_ordinary_commands_are_not_denied(windows, command: str) -> None:
    """A two-letter token appears constantly in ordinary command lines.

    ``docker run --rm`` and ``git rm --cached`` are the cases that decide
    whether this denylist is usable — a bare ``\\brm\\b`` blocks both, and a
    policy that blocks them gets turned off.
    """
    assert_allowed(windows, command)


def test_an_exe_suffix_still_matches_but_a_longer_name_does_not(windows) -> None:
    """``rm.exe`` is the real binary; ``rm-cache.cmd`` merely starts with it."""
    assert_denied(windows, r"rm.exe -rf C:\x")
    assert_allowed(windows, r".\rm-cache.cmd")


# ── the shared denylist now applies on Windows ──────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "shutdown /s /t 0",
        "shutdown -r -t 0",
        "rm -rf /",
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
        "chmod -R 777 /",
        ":(){ :|: & };:",
    ],
    ids=["shutdown", "shutdown-r", "rm-rf-root", "mkfs", "dd", "chmod-777", "fork-bomb"],
)
def test_catastrophic_shared_patterns_are_gated_on_windows(windows, command: str) -> None:
    """The Windows list used to *replace* the shared one rather than extend it.

    ``shutdown`` is a native Windows binary; the rest arrive through git-bash,
    MSYS, Cygwin or WSL, all of which are ordinary on a Windows dev box.
    """
    assert_denied(windows, command)


@pytest.mark.parametrize(
    "command",
    [
        r"del C:\tmp\file.txt",
        r"rmdir /s /q C:\tmp\folder",
        r"Remove-Item C:\tmp\stale.txt",
        "Format-Volume -DriveLetter D",
        "Stop-Computer",
        "Restart-Computer",
        "Clear-Disk -Number 1",
        "git push origin main --force",
    ],
)
def test_existing_windows_patterns_still_deny(windows, command: str) -> None:
    """Extending the list must not disturb what it already caught."""
    assert_denied(windows, command)


# ── the native counterpart of Format-Volume ─────────────────────────────────


@pytest.mark.parametrize("command", ["format C: /fs:ntfs /q", r"format D:", "echo 1 && format E:"])
def test_native_format_is_denied(windows, command: str) -> None:
    """``Format-Volume`` was covered but its native spelling was not — the same
    asymmetry as ``Remove-Item`` being covered while ``rm`` was not."""
    assert_denied(windows, command)


@pytest.mark.parametrize(
    "command",
    [
        "ruff format src tests",
        "git log --format=%H",
        "cargo fmt --format json",
        "black --check --format text .",
        r".\format-helper.ps1",
    ],
)
def test_the_word_format_in_an_argument_is_not_denied(windows, command: str) -> None:
    assert_allowed(windows, command)


# ── word boundaries on the shared patterns ──────────────────────────────────


@pytest.mark.parametrize(
    "command",
    ["echo asphalt", "python autoshutdown.py", "./fastreboot.sh", "echo cobalt"],
)
def test_a_longer_word_containing_a_keyword_is_not_denied(posix, windows, command: str) -> None:
    """``halt\\b`` had no leading boundary, so ``asphalt`` matched it — and the
    same for ``autoshutdown`` and ``fastreboot``.

    A POSIX-only false positive until this change, which is exactly why it had
    to be fixed here: extending the shared list to Windows would have carried
    it across.
    """
    assert_allowed(posix, command)
    assert_allowed(windows, command)


@pytest.mark.parametrize("command", ["sudo halt", "shutdown now", "reboot", "mkfs.ext4 /dev/sda1"])
def test_the_keywords_themselves_are_still_denied(posix, command: str) -> None:
    assert_denied(posix, command)


# ── POSIX behaviour is unchanged ────────────────────────────────────────────


def test_posix_still_routes_a_plain_rm_to_approval_not_denial(posix) -> None:
    """On POSIX ``rm`` is a warnlist entry, not a denylist one. The Windows
    change must not reach across and start hard-denying it."""
    result = posix.check("rm important_file.txt")

    assert result.allowed is True
    assert result.needs_approval is True


def test_posix_does_not_gain_the_windows_patterns(posix) -> None:
    """``del`` and ``ri`` are Windows spellings; on POSIX they are ordinary
    words and must not be denied."""
    assert_allowed(posix, "ri data.txt")
    assert_allowed(posix, "echo del")


# ── configuration still wins ────────────────────────────────────────────────


def test_an_explicit_deny_env_still_replaces_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """The platform defaults are only a fallback; an operator who sets the
    variable owns the list entirely, extension included."""
    monkeypatch.setattr(shell_policy.os, "name", "nt")
    monkeypatch.setenv("AGENTOS_SAFE_BIN_DENY", r"\bcustom-block\b")
    monkeypatch.delenv("AGENTOS_SAFE_BIN_ALLOW", raising=False)
    monkeypatch.delenv("AGENTOS_SAFE_BIN_WARN", raising=False)

    policy = shell_policy.SafeBinPolicy.from_env()

    assert policy.check("custom-block").allowed is False
    assert policy.check("rm important_file.txt").allowed is True
    assert policy.check("shutdown /s /t 0").allowed is True
