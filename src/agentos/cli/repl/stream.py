"""Compatibility alias for the TUI-owned terminal stream renderer."""

from __future__ import annotations

import sys

from agentos.cli.tui import stream as _target

sys.modules[__name__] = _target
