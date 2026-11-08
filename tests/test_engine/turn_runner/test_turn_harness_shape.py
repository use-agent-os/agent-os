"""``TurnHarness`` orchestrates the turn; no adapter classes remain (#3819)."""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from types import SimpleNamespace

import agentos.engine.turn_runner as turn_runner_pkg
from agentos.engine.harness.turn import TurnHarness, TurnRequest
from agentos.engine.runtime import TurnRunner

#: The orchestration method stays a readable sequence of stage calls.
RUN_TURN_MAX_LINES = 200


def _function(obj: object) -> ast.AsyncFunctionDef | ast.FunctionDef:
    node = ast.parse(textwrap.dedent(inspect.getsource(obj))).body[0]
    assert isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    return node


def test_run_turn_stays_under_the_line_budget() -> None:
    node = _function(TurnHarness._run_turn)
    length = (node.end_lineno or node.lineno) - node.lineno + 1

    assert length < RUN_TURN_MAX_LINES, (
        f"TurnHarness._run_turn is {length} lines; build stage inputs or terminal "
        f"paths in helpers instead (limit {RUN_TURN_MAX_LINES})."
    )


def test_router_replay_loops_instead_of_recursing() -> None:
    called = {
        n.func.attr
        for n in ast.walk(_function(TurnHarness._run_turn))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }

    assert "_run_turn" not in called and "run_turn" not in called


def test_no_adapter_classes_bind_ports_to_the_runner() -> None:
    root = Path(turn_runner_pkg.__file__).parent.parent
    offenders = [
        f"{path.relative_to(root)}:{node.name}"
        for path in sorted(root.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ClassDef)
        and node.name.endswith("Adapter")
        and node.name.startswith(("_TurnRunner", "_PromptReport", "_RequestContext"))
    ]

    assert offenders == []


def test_stages_are_built_with_the_runner_as_their_ports() -> None:
    runner = TurnRunner(provider_selector=None, config=SimpleNamespace())
    stages = runner._harness.stages

    assert stages.provider_and_tools._provider_resolver is runner
    assert stages.history_load._history_loader is runner
    assert stages.turn_finalizer._session_totals is runner


def test_replacing_a_stage_on_the_runner_replaces_it_in_the_harness() -> None:
    runner = TurnRunner(provider_selector=None, config=SimpleNamespace())
    replacement = object()

    runner._turn_finalizer_stage = replacement

    assert runner._harness.stages.turn_finalizer is replacement
    assert runner._turn_finalizer_stage is replacement


def test_turn_request_defaults_match_the_run_turn_signature() -> None:
    signature = inspect.signature(TurnRunner._run_turn)
    request = TurnRequest(message="m", session_key="s", agent_id="a", model=None, attachments=[])

    for name, parameter in signature.parameters.items():
        if name == "self" or parameter.default is inspect.Parameter.empty:
            continue
        assert getattr(request, name) == parameter.default, name
