"""Compatibility alias for terminal signal handlers."""

from __future__ import annotations

import sys

from agentos.cli.tui.terminal import signals as _target

sys.modules[__name__] = _target
