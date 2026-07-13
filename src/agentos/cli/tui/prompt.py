"""Compatibility alias for the terminal prompt adapter."""

from __future__ import annotations

import sys

from agentos.cli.tui.terminal import prompt as _target

sys.modules[__name__] = _target
