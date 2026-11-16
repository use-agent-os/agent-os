"""Importing the MCP client first in a fresh interpreter must not break the builtin tools.

``agentos.mcp.discovery`` imports the tool registry, which loads the builtin
tools; ``agents`` and ``sessions`` import the gateway, which imports
``rpc_mcp``, which needs discovery. A ``from ... import name`` there used to
hit the half-initialized discovery module and drop both builtin tools with
``builtin_tool.import_failed``.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    ["agentos.mcp.discovery", "agentos.mcp", "agentos.mcp.stdio", "agentos.gateway.rpc_mcp"],
)
def test_fresh_import_has_no_circular_import_failure(module: str, tmp_path) -> None:
    env = {**os.environ, "AGENTOS_STATE_DIR": str(tmp_path)}
    script = (
        f"import {module}\n"
        "from agentos.tools.registry import get_default_registry\n"
        "assert 'sessions_list' in get_default_registry().list_names()\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "builtin_tool.import_failed" not in output
    assert "circular import" not in output
