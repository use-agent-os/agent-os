from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from types import ModuleType


def _load_search_module() -> ModuleType:
    root = Path(__file__).resolve().parents[2]
    script = root / "src/agentos/skills/bundled/multi-search-engine/scripts/search.py"
    spec = importlib.util.spec_from_file_location("multi_search_engine_search", script)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_search_all_runs_requested_engines_concurrently_and_keeps_engine_order() -> None:
    search = _load_search_module()

    def make_handler(engine: str, delay: float):
        def handler(query: str, limit: int) -> list[object]:
            time.sleep(delay)
            return [search.Result(engine=engine, title=query, url=engine, snippet="", rank=limit)]

        return handler

    search.ENGINES = {
        "slow": make_handler("slow", 0.24),
        "medium": make_handler("medium", 0.18),
        "fast": make_handler("fast", 0.05),
    }

    started = time.monotonic()
    payload = search.search_all(
        "parallel search",
        ["slow", "medium", "fast"],
        limit=1,
        strict=False,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.34
    assert [item["engine"] for item in payload["results"]] == ["slow", "medium", "fast"]
    assert payload["errors"] == []
