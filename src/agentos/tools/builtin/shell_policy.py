"""Safe-bin policy enforcement for shell command execution."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# Patterns that are always dangerous regardless of context
DEFAULT_DENYLIST: list[str] = [
    r"rm\s+-rf\s+/\*?$",  # rm -rf / and rm -rf /*
    # Leading \b as well as trailing: without it these match inside a longer
    # word, so `echo asphalt`, `python autoshutdown.py` and `./fastreboot.sh`
    # were all blocked outright. That mattered only on POSIX before; this list
    # now applies on Windows too, so the false positive would have spread.
    r"\bmkfs\b",  # format filesystems
    r"dd\s+if=",  # raw disk writes
    r"\bshutdown\b",  # system shutdown
    r"\breboot\b",  # system reboot
    r"\bhalt\b",  # system halt
    r":\(\)\s*\{.*:\|:.*\}",  # fork bomb
    r">\s*/dev/sda",  # overwrite block device
    r"chmod\s+-R\s+777\s+/",  # world-writable root
    r"(?i)\bFormat-Volume\b",  # PowerShell filesystem format
    r"(?i)\bClear-Disk\b",  # PowerShell disk wipe
    r"(?i)\bStop-Computer\b",  # PowerShell system shutdown
    r"(?i)\bRestart-Computer\b",  # PowerShell system reboot
]

# Where a command name may start: the beginning of the string, a shell
# separator, or the opening of a block or subexpression. ``(`` and ``{`` are
# command positions too -- ``powershell -c "if (Test-Path x) { rm -r x }"`` and
# ``cmd /c (del x)`` both run a real command that a separator-only anchor never
# sees. Only openers are listed; a command does not begin right after ``)``.
_WIN_CMD_START: str = r"(?:^|[;&|\n({])\s*"

# An optional wrapper between the command position and the command name. The
# wrapper's payload is usually quoted -- ``powershell -c "rm -r C:\x"`` is how
# cmd and subprocess hand PowerShell a command string -- so one opening quote
# is allowed here, and only here, not after a bare separator.
_WIN_CMD_WRAPPER: str = (
    r"(?:(?:cmd(?:\.exe)?\s+/[ck]|(?:powershell|pwsh)(?:\.exe)?(?:\s+-[a-zA-Z]+)*)\s+[\"']?)?"
)

_WIN_CMD_PREFIX: str = _WIN_CMD_START + _WIN_CMD_WRAPPER

# What may follow an anchored command name: an optional ``.exe``, then anything
# that is not a name character. A bare ``\b`` ends the match at the ``-`` in
# ``rm-cache.cmd`` and denies a script that merely starts with an alias name;
# this refuses that while still matching the real ``rm.exe``.
_WIN_CMD_END: str = r"(?:\.exe)?(?![\w.\-])"


def _win_command(name: str) -> str:
    """Deny *name* only where it is actually being run as a command."""
    return _WIN_CMD_PREFIX + name + _WIN_CMD_END


DEFAULT_DENYLIST_WIN: list[str] = [
    # del / rmdir / Remove-Item stay unanchored on purpose. They are long
    # enough not to collide with ordinary arguments, and matching them
    # anywhere also catches a nesting this module's anchor cannot express.
    # Narrowing them would trade a false positive for a missed deletion.
    r"\bdel\b",
    r"\brmdir\b",
    r"\bRemove-Item\b",
    # PowerShell ships six built-in aliases for Remove-Item: del, erase, rd,
    # ri, rm and rmdir. rm and ri were the two with no pattern, so a deletion
    # spelled either way ran unblocked and unconfirmed. They are anchored the
    # way rd and erase are, because a bare \brm\b fires inside `docker run
    # --rm` and `git rm --cached`, and \bri\b inside almost anything.
    _win_command("rd"),
    _win_command("erase"),
    _win_command("rm"),
    _win_command("ri"),
    r"\bFormat-Volume\b",
    # The native counterpart of Format-Volume. Anchored, so `git log
    # --format=%H` and `--format json` are untouched.
    _win_command("format"),
    r"\bStop-Computer\b",
    r"\bRestart-Computer\b",
    r"\bClear-Disk\b",
    r"git\s+push\s+.*--force",
]

# Patterns that require two-step confirmation (warn, not block)
DEFAULT_WARNLIST: list[str] = [
    r"\brm\b",  # any rm invocation (catches rm, rm -r, rm -R, rm -f, rm -rf /etc, etc.)
    r"chmod\s+-R",  # recursive permission change
    r"chown\s+-R",  # recursive ownership change
    r"git\s+push\s+.*--force",  # force push
    r"(?i)\bDROP\s+",  # SQL drop
    r"(?i)\bTRUNCATE\s+",  # SQL truncate
    r"pip\s+install\s+(?!-e)",  # non-editable pip install
]

DEFAULT_WARNLIST_WIN: list[str] = []

_LEGACY_ENV_WARNED: bool = False


def _resolve_env_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _legacy_denylist_if_set() -> list[str]:
    global _LEGACY_ENV_WARNED

    legacy = os.environ.get("AGENTOS_SHELL_DENYLIST", "").strip()
    if not legacy:
        return []
    if not _LEGACY_ENV_WARNED:
        import structlog

        structlog.get_logger(__name__).warning(
            "shell_policy.legacy_deny_env_detected",
            message=("AGENTOS_SHELL_DENYLIST is deprecated; use AGENTOS_SAFE_BIN_DENY"),
        )
        _LEGACY_ENV_WARNED = True
    return _resolve_env_list(legacy)


@dataclass
class PolicyResult:
    """Result of a policy check."""

    allowed: bool
    reason: str
    needs_approval: bool = False


@dataclass
class SafeBinPolicy:
    denylist: list[str]  # regex patterns — if any match, command is denied
    allowlist: list[str]  # regex patterns — if non-empty, only matching commands are allowed
    warnlist: list[str] = field(default_factory=list)  # patterns requiring two-step approval

    @classmethod
    def from_env(cls) -> SafeBinPolicy:
        deny_env = os.environ.get("AGENTOS_SAFE_BIN_DENY", "")
        allow_env = os.environ.get("AGENTOS_SAFE_BIN_ALLOW", "")
        warn_env_present = "AGENTOS_SAFE_BIN_WARN" in os.environ
        warn_env = os.environ.get("AGENTOS_SAFE_BIN_WARN", "")

        deny = _resolve_env_list(deny_env)
        allow = _resolve_env_list(allow_env)
        warn = _resolve_env_list(warn_env)

        if not deny:
            deny = _legacy_denylist_if_set()
            if not deny:
                # Windows *extends* the shared list rather than replacing it.
                # Replacing dropped every catastrophic pattern that is not
                # Windows-specific -- `shutdown` is a native Windows binary,
                # and `rm -rf /`, `mkfs`, `dd if=`, `chmod -R 777 /` and the
                # fork bomb all reach a Windows host through git-bash, MSYS,
                # Cygwin or WSL. None of them were gated there.
                deny = (
                    [*DEFAULT_DENYLIST, *DEFAULT_DENYLIST_WIN]
                    if os.name == "nt"
                    else DEFAULT_DENYLIST
                )
        if not warn and not warn_env_present:
            warn = DEFAULT_WARNLIST_WIN if os.name == "nt" else DEFAULT_WARNLIST

        return cls(denylist=deny, allowlist=allow, warnlist=warn)

    def check(self, command: str) -> PolicyResult:
        """Check command against policy layers: allowlist → denylist → warnlist."""
        # Allowlist check: if non-empty, command must match at least one pattern
        if self.allowlist:
            matched = any(
                re.search(pattern, command, flags=re.IGNORECASE if os.name == "nt" else 0)
                for pattern in self.allowlist
            )
            if not matched:
                return PolicyResult(
                    allowed=False,
                    reason=f"command not in allowlist: {command!r}",
                )

        # Denylist check: command must not match any pattern (never bypassable)
        for pattern in self.denylist:
            if re.search(pattern, command, flags=re.IGNORECASE if os.name == "nt" else 0):
                return PolicyResult(
                    allowed=False,
                    reason=f"command blocked by policy (pattern: {pattern!r}): {command!r}",
                )

        # Warnlist check: command needs two-step approval
        for pattern in self.warnlist:
            if re.search(pattern, command, flags=re.IGNORECASE if os.name == "nt" else 0):
                return PolicyResult(
                    allowed=True,
                    reason=f"command requires approval (pattern: {pattern!r}): {command!r}",
                    needs_approval=True,
                )

        return PolicyResult(allowed=True, reason="")


# Module-level singleton loaded from environment at import time.
# Tests may replace this with a custom instance.
_policy: SafeBinPolicy | None = None


def get_policy() -> SafeBinPolicy:
    global _policy
    if _policy is None:
        _policy = SafeBinPolicy.from_env()
    return _policy


def set_policy(policy: SafeBinPolicy) -> None:
    """Override the active policy (useful for testing)."""
    global _policy
    _policy = policy


def check_safe_bin(command: str) -> PolicyResult:
    """Check command against the active policy. Returns PolicyResult."""
    return get_policy().check(command)
