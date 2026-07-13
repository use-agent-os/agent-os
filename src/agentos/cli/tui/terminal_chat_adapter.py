"""Compatibility alias for terminal chat adapter composition."""

from __future__ import annotations

import sys

from agentos.cli.tui.adapters import terminal_chat_adapter as _target

sys.modules[__name__] = _target
