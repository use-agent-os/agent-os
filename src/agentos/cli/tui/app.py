"""Compatibility alias for the terminal TUI application."""

from __future__ import annotations

import sys

from agentos.cli.tui.terminal import app as _target

sys.modules[__name__] = _target
