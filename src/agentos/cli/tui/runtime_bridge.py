"""Compatibility alias for TUI runtime composition."""

from __future__ import annotations

import sys

from agentos.cli.tui.adapters import runtime_bridge as _target

sys.modules[__name__] = _target
