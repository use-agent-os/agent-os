"""Shared subagent governance limits.

Single source of truth for spawn-depth caps and related governance constants,
used by the subagent spawn path (``tools/builtin/sessions.py``).
"""

from __future__ import annotations

MAX_SPAWN_DEPTH = 3
"""Maximum subagent nesting depth.

Depth 0 = main session, depth 1 = first subagent, depth 2 = sub-subagent,
depth 3 = leaf worker. Spawning at depth >= 3 is rejected.
"""
