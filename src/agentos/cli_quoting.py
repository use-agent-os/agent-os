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
    # A Windows filename cannot contain ``"``, so the replacement only guards
    # hand-written values; ``""`` is the literal-quote spelling both cmd.exe
    # and PowerShell accept inside a double-quoted string.
    escaped = value.replace('"', '""')
    # A run of backslashes immediately before the closing quote must be
    # doubled: the program that ultimately parses this command line (MSVC-style
    # argv parsing -- what python.exe itself uses) reads an odd trailing run as
    # an escape for the closing quote rather than a delimiter, so the quote
    # never closes and the rest of the line is swallowed into this argument.
    # Directory paths routinely end in exactly this shape.
    trailing_backslashes = len(escaped) - len(escaped.rstrip("\\"))
    if trailing_backslashes:
        escaped += "\\" * trailing_backslashes
    return '"' + escaped + '"'


def config_cli_arg(config_path: str | Path | None) -> str:
    """Render the ``--config <path>`` suffix, or ``""`` when there is no path."""
    if not config_path:
        return ""
    return f" --config {quote_cli_arg(str(config_path))}"
