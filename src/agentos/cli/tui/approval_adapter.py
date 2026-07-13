"""Compatibility alias for the terminal TUI approval adapter."""

from __future__ import annotations

import sys

from agentos.cli.tui.terminal import approval as _target

sys.modules[__name__] = _target
