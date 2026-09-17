"""Issue #2485: the Windows denylist prefix stopped at a PowerShell flag value.

``_WIN_CMD_PREFIX`` anchors ``rm`` / ``ri`` / ``rd`` / ``erase`` to the start
of a command, optionally through a ``cmd /c`` or ``powershell -c`` wrapper.
Its flag pattern was ``(?:\\s+-[a-zA-Z]+)*`` -- flags with no value -- so the
moment a flag carried one (``-ExecutionPolicy Bypass``, ``-ep Bypass``,
``-WindowStyle Hidden``) the prefix stopped matching and the wrapped
deletion came back ``allowed=True``.

Measured on ``main`` over 28 shapes: 10 wrong. Three more shapes were wrong
in the first fix proposed for this (#2503) as well: a payload opening with
PowerShell's call operator and a script block (``-Command "& {rm C:\\x}"``),
a wrapper nested in a wrapper (``cmd /c powershell -ep bypass -c "rm ..."``),
and whitespace between the opening quote and the command.
"""

from __future__ import annotations

import re
import time

import pytest

from agentos.tools.builtin import shell_policy
from agentos.tools.builtin.shell_policy import _WIN_CMD_END, _WIN_CMD_PREFIX, SafeBinPolicy


@pytest.fixture
def windows_policy(monkeypatch: pytest.MonkeyPatch) -> SafeBinPolicy:
    """The policy exactly as a Windows host builds it, on any CI runner."""
    monkeypatch.setattr(shell_policy.os, "name", "nt")
    for name in (
        "AGENTOS_SAFE_BIN_DENY",
        "AGENTOS_SAFE_BIN_ALLOW",
        "AGENTOS_SAFE_BIN_WARN",
        "AGENTOS_SHELL_DENYLIST",
    ):
        monkeypatch.delenv(name, raising=False)
    return SafeBinPolicy.from_env()


def _anchored(command: str, name: str = "rm") -> bool:
    return re.search(_WIN_CMD_PREFIX + name + _WIN_CMD_END, command, re.IGNORECASE) is not None


# ── the issue: flags that carry a value ─────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        r'powershell -ExecutionPolicy Bypass -Command "rm C:\x"',
        r'powershell -ep Bypass -c "rm C:\x"',
        r'powershell -ConfigurationName default -Command "rm C:\x"',
    ],
)
def test_the_issues_three_shapes_are_blocked(windows_policy: SafeBinPolicy, command: str) -> None:
    result = windows_policy.check(command)

    assert result.allowed is False
    assert result.needs_approval is False


@pytest.mark.parametrize(
    "command",
    [
        r'powershell -ExecutionPolicy:Bypass -c "rm C:\x"',
        r'powershell -ep:Bypass -c "rm C:\x"',
        r'powershell -WindowStyle Hidden -c "rm C:\x"',
        r'powershell -w hidden -c "rm C:\x"',
        r'powershell -nop -w hidden -ep bypass -c "rm -r C:\x"',
        r'pwsh -NoProfile -NonInteractive -ExecutionPolicy Unrestricted -c "rm -Recurse C:\x"',
        r'powershell.exe -NoLogo -InputFormat Text -OutputFormat Text -c "rm C:\x"',
        r'powershell -Version 5.1 -c "rm C:\x"',
        r'powershell -WorkingDirectory C:\tmp -c "rm C:\x"',
    ],
)
def test_every_flag_value_form_is_seen_through(windows_policy: SafeBinPolicy, command: str) -> None:
    assert windows_policy.check(command).allowed is False


def test_a_flag_value_never_starts_with_a_dash() -> None:
    """``-nop -c`` is two flags, not ``-nop`` with the value ``-c``."""
    assert _anchored(r'powershell -nop -c "rm C:\x"')


def test_an_unquoted_command_after_dash_c_is_still_the_command() -> None:
    """``-c rm C:\\x``: the engine must leave ``rm`` for the command match
    rather than reading it as ``-c``'s value."""
    assert _anchored(r"powershell -c rm C:\x")
    assert _anchored(r"pwsh -Command rm C:\x")


# ── beyond the issue: the shapes #2503 still lets through ───────────────────


@pytest.mark.parametrize(
    "command",
    [
        r'powershell -Command "& {rm C:\x}"',
        r'powershell -Command "& { rm -r C:\x }"',
        r'powershell -c "&{rm C:\x}"',
        r'powershell -c "& rm C:\x"',
        r'powershell -c "{rm C:\x}"',
    ],
)
def test_the_call_operator_and_script_block_are_seen_through(
    windows_policy: SafeBinPolicy, command: str
) -> None:
    assert windows_policy.check(command).allowed is False


@pytest.mark.parametrize(
    "command",
    [
        r'cmd /c powershell -ep bypass -c "rm C:\x"',
        r'cmd.exe /c "powershell -c rm C:\x"',
        r'cmd /k "pwsh -nop -c ""rm C:\x"""',
        r'cmd /c "powershell -c \"rm C:\x\""',
        r'powershell -c "cmd /c rm C:\x"',
        r"cmd /c cmd /c rm C:\x",
        r'powershell -c "powershell -ep bypass -c rm C:\x"',
    ],
)
def test_a_wrapper_inside_a_wrapper_is_seen_through(
    windows_policy: SafeBinPolicy, command: str
) -> None:
    assert windows_policy.check(command).allowed is False


@pytest.mark.parametrize(
    "command",
    [
        r'powershell -c "  rm C:\x"',
        r"powershell -c '	rm C:\x'",
        r'powershell -c " & { rm C:\x }"',
    ],
)
def test_whitespace_after_the_opening_quote_is_seen_through(
    windows_policy: SafeBinPolicy, command: str
) -> None:
    assert windows_policy.check(command).allowed is False


# ── every alias, every anchor ───────────────────────────────────────────────


@pytest.mark.parametrize("alias", ["rm", "ri", "rd", "erase"])
@pytest.mark.parametrize(
    "shape",
    [
        "{alias} C:\\x",
        'powershell -ep Bypass -c "{alias} C:\\x"',
        'powershell -Command "& {{{alias} C:\\x}}"',
        'cmd /c powershell -c "{alias} C:\\x"',
        'echo a; powershell -w hidden -c "{alias} C:\\x"',
        "echo a && {alias} C:\\x",
        'echo a | powershell -ep Bypass -c "{alias} C:\\x"',
    ],
)
def test_every_alias_through_every_anchor(
    windows_policy: SafeBinPolicy, alias: str, shape: str
) -> None:
    assert windows_policy.check(shape.format(alias=alias)).allowed is False


def test_case_does_not_matter_on_windows(windows_policy: SafeBinPolicy) -> None:
    assert windows_policy.check(r'PowerShell -EP Bypass -C "RM C:\x"').allowed is False
    assert (
        windows_policy.check(r'POWERSHELL.EXE -ExecutionPolicy Bypass -Command "Ri C:\x"').allowed
        is False
    )


# ── what must stay allowed ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "docker run --rm img",
        "git rm --cached f",
        "npm run rm-cache",
        'powershell -c "Get-ChildItem C:\\x"',
        'powershell -c "git rm --cached f"',
        'powershell -ep Bypass -c "npm run rm-cache"',
        "powershell -ExecutionPolicy Bypass -File build.ps1",
        'powershell -c "docker run --rm img"',
        'cmd /c "git rm --cached f"',
        'powershell -Command "& {git rm --cached f}"',
        "echo rm",
        "ls rd-report.ps1",
        "python rm-tool.py",
        'powershell -c "Write-Host rm"',
        'powershell -c "ls | grep rm"',
    ],
)
def test_ordinary_commands_stay_allowed(windows_policy: SafeBinPolicy, command: str) -> None:
    result = windows_policy.check(command)

    assert result.allowed is True
    assert result.needs_approval is False


def test_a_script_merely_starting_with_the_alias_is_not_the_alias(
    windows_policy: SafeBinPolicy,
) -> None:
    """``_WIN_CMD_END`` is unchanged: ``rm-cache.cmd`` is not ``rm``."""
    assert windows_policy.check(r'powershell -ep Bypass -c "rm-cache.cmd"').allowed is True
    assert windows_policy.check(r'powershell -ep Bypass -c "rm.exe C:\x"').allowed is False


def test_the_opening_quote_is_only_allowed_after_a_wrapper() -> None:
    """A bare separator followed by a quote is not a wrapper payload."""
    assert not _anchored('echo a; "rm C:\\x"')
    assert _anchored("echo a; rm C:\\x")


# ── the pattern stays cheap ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "powershell " + "-a " * 2000 + "x",
        "powershell " + "-a b " * 2000 + "x",
        "cmd /c " * 500 + "x",
        'powershell -c "' + " " * 5000 + "x",
        'powershell -c "' + "& { " * 1000 + "x",
    ],
)
def test_nested_quantifiers_do_not_backtrack_catastrophically(command: str) -> None:
    """Repeated wrappers and repeated flag values are both ``*`` groups with
    ``*`` inside; an adversarial command must still be classified in
    milliseconds, not seconds."""
    pattern = re.compile(_WIN_CMD_PREFIX + "rm" + _WIN_CMD_END, re.IGNORECASE)

    started = time.perf_counter()
    pattern.search(command)

    assert time.perf_counter() - started < 0.5


# ── the other lists are untouched ───────────────────────────────────────────


def test_the_shared_catastrophic_list_still_applies_on_windows(
    windows_policy: SafeBinPolicy,
) -> None:
    assert windows_policy.check("rm -rf /").allowed is False
    assert (
        windows_policy.check('powershell -ep Bypass -c "Remove-Item -Recurse C:\\x"').allowed
        is False
    )
    assert (
        windows_policy.check('powershell -ep Bypass -c "Format-Volume -DriveLetter C"').allowed
        is False
    )
