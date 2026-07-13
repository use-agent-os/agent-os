"""Spawn-depth constants must agree across the engine and gateway paths."""

from __future__ import annotations

from agentos.agents.limits import MAX_SPAWN_DEPTH
from agentos.engine import subagent as engine_subagent
from agentos.tools.builtin import sessions as sessions_tool


def test_max_spawn_depth_is_three() -> None:
    assert MAX_SPAWN_DEPTH == 3


def test_engine_default_matches_shared_limit() -> None:
    assert engine_subagent.DEFAULT_MAX_SPAWN_DEPTH == MAX_SPAWN_DEPTH


def test_sessions_tool_matches_shared_limit() -> None:
    assert sessions_tool._MAX_SPAWN_DEPTH == MAX_SPAWN_DEPTH
