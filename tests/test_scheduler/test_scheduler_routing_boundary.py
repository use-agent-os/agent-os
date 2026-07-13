from __future__ import annotations

from pathlib import Path


def test_gateway_routing_imports_stay_behind_scheduler_adapter() -> None:
    scheduler_root = Path("src/agentos/scheduler")
    offenders = [
        str(path)
        for path in scheduler_root.glob("*.py")
        if path.name != "routing.py"
        and "agentos.gateway.routing" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
