"""Compatibility alias for legacy chat command helpers."""

from __future__ import annotations

import sys

from agentos.cli.tui.adapters import chat_compat as _target

sys.modules[__name__] = _target
