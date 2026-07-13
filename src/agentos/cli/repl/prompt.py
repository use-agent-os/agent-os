"""Compatibility alias for the TUI-owned terminal prompt module."""

from __future__ import annotations

import sys

from agentos.cli.tui import prompt as _target

sys.modules[__name__] = _target
