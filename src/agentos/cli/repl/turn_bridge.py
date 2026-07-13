"""Compatibility alias for the TUI-owned turn bridge module."""

from __future__ import annotations

import sys

from agentos.cli.tui import turn_bridge as _target

sys.modules[__name__] = _target
