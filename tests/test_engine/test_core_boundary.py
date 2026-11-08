"""``agentos.engine.core`` stays free of the gateway (issue #3817).

The loop core must be runnable without a gateway, so no module under
``engine/core`` may import ``agentos.gateway`` -- directly, lazily inside a
function, or under ``TYPE_CHECKING``. The check reads the source with
``ast`` so a lazy import cannot slip past it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import agentos.engine.core as core

FORBIDDEN = ("agentos.gateway",)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


def test_engine_core_never_imports_the_gateway() -> None:
    root = Path(core.__file__).parent
    sources = sorted(root.rglob("*.py"))
    assert sources, "engine/core has no modules"

    offenders = [
        f"{path.relative_to(root)}: {name}"
        for path in sources
        for name in _imports(path)
        if any(name == bad or name.startswith(bad + ".") for bad in FORBIDDEN)
    ]

    assert offenders == []
