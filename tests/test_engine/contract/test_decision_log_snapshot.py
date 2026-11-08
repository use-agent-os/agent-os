"""Snapshot of one fixed multi-tool session through the real ``TurnRunner``.

The harness refactor (OpenSpec change ``refactor-agent-harness-core``) moves
code out of ``Agent._turn_generator`` and ``TurnRunner._run_turn`` phase by
phase. This test runs one scripted session -- two parallel tools, one failing
tool, then a final answer -- end to end through the runner with real tool
dispatch, and pins what an observer can see:

* the engine event kinds in order (state transitions collapsed),
* the tool results the model received, in order,
* the per-turn ``DecisionEntry`` fields that do not depend on the machine,
  the clock or hashing (timings, hashes, ids and char counts are dropped).

A phase that changes any of these on purpose updates the snapshot in the same
PR, so the diff shows exactly what moved. Regenerate with::

    AGENTOS_UPDATE_SNAPSHOTS=1 uv run pytest \
        tests/test_engine/contract/test_decision_log_snapshot.py
"""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine.runtime import TurnRunner
from agentos.gateway.config import AgentOSRouterConfig, AttachmentsConfig, GatewayConfig
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import CallerKind, ToolContext, ToolSpec
from tests.fakes import ScriptedProvider, done, text, tool_call

KEY = "agent:main:webchat:decision-snapshot"
SNAPSHOT = Path(__file__).parent / "snapshots" / "decision_log_multi_tool.json"

#: DecisionEntry fields that are stable across machines and runs.
_STABLE_FIELDS = (
    "session_key",
    "tool_choice",
    "tokens_input",
    "tokens_output",
    "model",
    "provider",
    "tool_count",
    "skill_count",
    "cache_mode",
    "retrieval_mode",
    "resolved_model",
    "skills_invoked",
    "stop_reason",
    "refusal_category",
)


class _Selector:
    def __init__(self, provider: ScriptedProvider) -> None:
        self.provider = provider
        self.current_config = SimpleNamespace(model="scripted/model")

    def clone(self) -> _Selector:
        return self

    def resolve(self) -> ScriptedProvider:
        return self.provider

    def override_model(self, model: str) -> None:
        self.current_config = SimpleNamespace(model=model)


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def lookup(**arguments: Any) -> str:
        return f"looked up {arguments.get('q')}"

    async def stats(**arguments: Any) -> str:
        return "3 rows"

    async def broken(**arguments: Any) -> str:
        raise RuntimeError("backend unavailable")

    for name, handler in (("lookup", lookup), ("stats", stats), ("broken", broken)):
        registry.register(
            ToolSpec(
                name=name,
                description=f"snapshot tool {name}",
                parameters={"q": {"type": "string"}},
                concurrency="parallel" if name != "broken" else "exclusive",
                read_only=True,
            ),
            handler,
        )
    return registry


def _script() -> ScriptedProvider:
    return ScriptedProvider(
        [
            [
                text("Checking two sources."),
                tool_call("lookup", {"q": "alpha"}, tool_use_id="L1"),
                tool_call("stats", {"q": "beta"}, tool_use_id="S1"),
                done("tool_use", input_tokens=120, output_tokens=30),
            ],
            [
                tool_call("broken", {"q": "gamma"}, tool_use_id="B1"),
                done("tool_use", input_tokens=160, output_tokens=12),
            ],
            [
                text("Alpha found, three rows, gamma backend is down."),
                done("end_turn", input_tokens=190, output_tokens=18),
            ],
        ]
    )


def _collapse(kinds: list[str]) -> list[str]:
    out: list[str] = []
    for kind in kinds:
        if out and out[-1] == kind and kind in {"state_change", "text_delta", "run_heartbeat"}:
            continue
        out.append(kind)
    return out


def _tool_results(provider: ScriptedProvider) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    last = provider.calls[-1].messages
    for message in last:
        if not isinstance(message.content, list):
            continue
        for block in message.content:
            if getattr(block, "type", None) == "tool_result":
                results.append({"id": block.tool_use_id, "is_error": bool(block.is_error)})
    return results


def _stable_entry(entry: Any) -> dict[str, Any]:
    raw = dataclasses.asdict(entry)
    stable = {name: raw.get(name) for name in _STABLE_FIELDS}
    stable["pipeline_steps"] = [
        [step.get("step_name"), step.get("applied")] for step in raw.get("pipeline_steps", [])
    ]
    return stable


@pytest.fixture(autouse=True)
def _state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path))


@pytest.fixture
async def manager(tmp_path: Path) -> AsyncIterator[SessionManager]:
    storage = SessionStorage(":memory:")
    await storage.connect()
    yield SessionManager(storage, inject_time_prefix=False, checkpoint_workspace_dir=tmp_path)
    await storage.close()


async def _observe(
    manager: SessionManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    entries: list[Any] = []
    monkeypatch.setattr("agentos.engine.runtime.write_decision_entry", entries.append)
    provider = _script()
    runner = TurnRunner(
        provider_selector=_Selector(provider),
        tool_registry=_registry(),
        session_manager=manager,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            agentos_router=AgentOSRouterConfig(enabled=False),
        ),
    )
    await manager.create(KEY)
    await manager.append_message(KEY, "user", "summarise alpha, beta and gamma")

    events = [
        event
        async for event in runner.run(
            "summarise alpha, beta and gamma",
            KEY,
            tool_context=ToolContext(caller_kind=CallerKind.WEB),
            history_has_persisted_user=True,
        )
    ]

    return {
        "provider_calls": len(provider.calls),
        "event_kinds": _collapse([event.kind for event in events]),
        "tool_results_seen_by_model": _tool_results(provider),
        "decision_entries": [_stable_entry(entry) for entry in entries],
    }


async def test_multi_tool_session_matches_snapshot(
    manager: SessionManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = await _observe(manager, tmp_path, monkeypatch)

    if os.environ.get("AGENTOS_UPDATE_SNAPSHOTS") == "1" or not SNAPSHOT.exists():
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(observed, indent=2, sort_keys=True) + "\n")
        if os.environ.get("AGENTOS_UPDATE_SNAPSHOTS") != "1":
            pytest.fail(f"snapshot did not exist; wrote {SNAPSHOT.name}, re-run to verify")

    assert observed == json.loads(SNAPSHOT.read_text())


async def test_snapshot_session_is_deterministic(
    manager: SessionManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await _observe(manager, tmp_path, monkeypatch)

    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        second_manager = SessionManager(
            storage, inject_time_prefix=False, checkpoint_workspace_dir=tmp_path
        )
        second = await _observe(second_manager, tmp_path, monkeypatch)
    finally:
        await storage.close()

    assert first == second
