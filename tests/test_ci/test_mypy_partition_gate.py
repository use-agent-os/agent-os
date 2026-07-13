from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

TIER_A_MYPY_PARTITION: tuple[str, ...] = (
    "src/agentos/tool_boundary.py",
    "src/agentos/tools/boundary.py",
    "src/agentos/gateway/session_services.py",
    "src/agentos/memory/protocols.py",
    "src/agentos/provider/protocol.py",
    "src/agentos/provider/openai.py",
    "src/agentos/session/compaction.py",
    "src/agentos/scheduler/routing.py",
    "src/agentos/scheduler/delivery.py",
    "src/agentos/scheduler/handlers.py",
    "src/agentos/skills/hub/installer.py",
    "src/agentos/skills/hub/scanner.py",
    "src/agentos/skills/hub/lockfile.py",
    "src/agentos/mcp/discovery.py",
    "src/agentos/tools/builtin/web.py",
)


def test_tier_a_mypy_partition_stays_clean() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "mypy", *TIER_A_MYPY_PARTITION],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert result.returncode == 0, result.stdout
