"""Compatibility alias for the TUI backend runtime module."""

from __future__ import annotations

import sys

from agentos.cli.tui.backend import runtime as _target

sys.modules[__name__] = _target
