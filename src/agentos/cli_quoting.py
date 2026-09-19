"""Platform-aware quoting for commands AgentOS prints as copy-pasteable hints.

Every "next step" line the CLI prints is meant to be pasted straight back into
the user's shell. ``shlex.quote`` only knows POSIX rules, so on Windows a path
with a space came back single-quoted — ``--config 'C:\\Users\\John Doe\\x.toml'``
— which neither ``cmd.exe`` nor PowerShell parses as one argument. Windows is
also where user profiles with spaces are most common, so that hint failed
exactly where it was needed most.

Hints are the only thing quoted here. Commands AgentOS *executes* build an
argv list and never go through a shell.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

# Anything that makes a Windows shell split or reinterpret a bare token:
# whitespace, the quote characters, the ``cmd.exe`` metacharacters
# (``^ & | < > ( ) % !``) and the PowerShell ones (``$ ` { } [ ] ; , = @``).
_WINDOWS_SPECIAL = frozenset(" \t\n\"'`^&|<>()[]{};,=$!%@")


def _is_windows_shell() -> bool:
    """Which shell family the reader is most likely pasting into."""
    return os.name == "nt"


def quote_cli_arg(value: str) -> str:
    """Quote *value* for the shell the reader is most likely to paste into."""
    if not _is_windows_shell():
        return shlex.quote(value)
    if not value:
        return '""'
    if not any(char in _WINDOWS_SPECIAL for char in value):
        return value
    # Double quotes are the one form both shells read as a single token, but
    # PowerShell still expands ``$name`` and backtick escapes inside them, so a
    # path like ``C:\home\Jo$hn`` came back as ``C:\home\Jo`` (#2978). Both
    # are escaped with a backtick, PowerShell's own escape character. cmd.exe
    # does not know the backtick and passes it through, so a hint holding one
    # of these characters is now right in PowerShell and wrong in cmd.exe,
    # where before it was the reverse; PowerShell is the shell Windows opens
    # by default, so it is the one the hint is written for.
    # ``""`` is the literal-quote spelling both shells accept inside a
    # double-quoted string; a Windows filename cannot contain ``"``, so that
    # replacement only guards hand-written values.
    escaped = value.replace("`", "``").replace("$", "`$").replace('"', '""')
    return '"' + escaped + '"'


def config_cli_arg(config_path: str | Path | None) -> str:
    """Render the ``--config <path>`` suffix, or ``""`` when there is no path."""
    if not config_path:
        return ""
    return f" --config {quote_cli_arg(str(config_path))}"
