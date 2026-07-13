"""Compatibility alias for terminal turn stream defaults."""

from __future__ import annotations

import sys

from agentos.cli.tui.adapters import turn_stream_defaults as _target

sys.modules[__name__] = _target
