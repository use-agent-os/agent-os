"""Safe-bin policy enforcement for shell command execution."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# Patterns that are always dangerous regardless of context
DEFAULT_DENYLIST: list[str] = [
    r"rm\s+-rf\s+/\*?$",  # rm -rf / and rm -rf /*
    r"mkfs\b",  # format filesystems
    r"dd\s+if=",  # raw disk writes
    r"shutdown\b",  # system shutdown
    r"reboot\b",  # system reboot
    r"halt\b",  # system halt
    r":\(\)\s*\{.*:\|:.*\}",  # fork bomb
    r">\s*/dev/sda",  # overwrite block device
    r"chmod\s+-R\s+777\s+/",  # world-writable root
    r"(?i)\bFormat-Volume\b",  # PowerShell filesystem format
    r"(?i)\bClear-Disk\b",  # PowerShell disk wipe
    r"(?i)\bStop-Computer\b",  # PowerShell system shutdown
    r"(?i)\bRestart-Computer\b",  # PowerShell system reboot
]

# Anchors a command name to the start of a command: line start or a
# separator, optionally through a ``cmd /c`` / ``powershell -c`` wrapper. The
# wrapper's payload may open with a quote (``powershell -c "rm -r C:\x"`` is
# how cmd and subprocess hand PowerShell a command), so the quote is allowed
# only there — not after a bare separator, where ``{"rd": 1}`` in a JSON
# payload would otherwise look like a command.
_WIN_CMD_PREFIX: str = (
    r"(?:^|[;&|\n])\s*"
    r"(?:(?:cmd(?:\.exe)?\s+/[ck]|(?:powershell|pwsh)(?:\.exe)?(?:\s+-[a-zA-Z]+)*)\s+[\"']?)?"
)

#: What may follow an anchored command name: a separator, whitespace or the
#: end. Unlike ``\b`` this refuses ``rm.ps1`` / ``rm-cache.cmd`` — scripts
#: that merely start with the alias — while still catching ``rm.exe``.
_WIN_CMD_END: str = r"(?:\.exe)?(?![\w.\-])"

# Windows-only spellings. These are *added* to ``DEFAULT_DENYLIST`` on
# Windows, never swapped in for it: the catastrophic patterns above are
# platform-independent (``shutdown`` is a Windows binary too, and the rest
# reach a Windows host through git-bash, MSYS, Cygwin or WSL).
DEFAULT_DENYLIST_WIN: list[str] = [
    r"\bdel\b",
    r"\brmdir\b",
    r"\bRemove-Item\b",
    _WIN_CMD_PREFIX + r"rd\b",
    _WIN_CMD_PREFIX + r"erase\b",
    # PowerShell's ``rm`` and ``ri`` are aliases of ``Remove-Item``; anchored
    # like ``rd``/``erase`` so ``npm run rm-cache`` or ``terraform ri`` pass.
    _WIN_CMD_PREFIX + r"rm" + _WIN_CMD_END,
    _WIN_CMD_PREFIX + r"ri" + _WIN_CMD_END,
    r"\bFormat-Volume\b",
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
                deny = (
                    [*DEFAULT_DENYLIST, *DEFAULT_DENYLIST_WIN]
                    if os.name == "nt"
                    else list(DEFAULT_DENYLIST)
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
