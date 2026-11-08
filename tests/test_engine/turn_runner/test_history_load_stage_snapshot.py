"""Snapshot regression net for ``HistoryLoadStage`` through ``TurnRunner._run_turn``.

The corpus enumerates every input shape the stage has been observed to
handle and pins the output snapshot. The harness patches the BEFORE-turn
helper the slice owns (``_load_history``; the T3-upgrade and DB-preflight
compaction helpers are gone -- compaction runs in the agent's
``CompactionService``) plus reuses the upstream stage's patch helpers from
the agent-bootstrap snapshot harness so the slice runs against
deterministic stubs.

It then probes ``_build_attachment_messages`` (the line immediately
after the slice in ``_run_turn``) to capture the post-slice locals and
raise a sentinel ``BaseException`` that halts the generator without
touching downstream stages. The raising-loader case exercises the
exception-propagation contract.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine.runtime import TurnRunner
from agentos.engine.types import DoneEvent, ErrorEvent

# Reuse upstream patch helpers from's equivalence harness — this
# stage sits AFTER AgentBootstrapStage's slice so the same upstream
# patching strategy applies. Local duplication would just inflate LOC.
from .test_agent_bootstrap_stage_snapshot import (
    _make_turn_factory,
    _patch_assemble_prompt,
    _patch_builder,
    _patch_ctx_mutators,
    _patch_memory_helpers,
    _patch_observability,
    _patch_resolve_prompt_config,
    _patch_resolver,
    _patch_router_context,
    _patch_run_pipeline,
    _patch_session_id,
    _StubModelCatalog,
    _StubProvider,
    _StubSelector,
)

# ---------------------------------------------------------------------------
# Sentinel for halting the generator after the slice
# ---------------------------------------------------------------------------


class _SliceCapture(BaseException):
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot


def _capture_locals_at_post_slice() -> dict[str, Any]:
    """Read ``_run_turn``'s locals at the boundary right after the slice.

    The probe is hooked onto ``_build_attachment_messages`` (the very
    next call site after the compaction-and-history slice). At entry,
    the caller's frame contains every local the compaction/history boundary
    must populate.
    """

    frame = sys._getframe(2)
    while frame is not None:
        if "agent" in frame.f_locals and "agent_config" in frame.f_locals:
            break
        frame = frame.f_back
    assert frame is not None, "could not locate _run_turn frame"
    locs = frame.f_locals

    agent = locs.get("agent")
    return {
        "outcome": "success",
        "agent_request_context_prompt_after": getattr(
            getattr(agent, "config", None), "request_context_prompt", None
        ),
        "agent_context_overflow_threshold": getattr(
            getattr(agent, "config", None), "context_overflow_threshold", None
        ),
    }


# ---------------------------------------------------------------------------
# Local budget / thinking patches —'s helpers take a case dict with
# different fields, so a smaller local stub keeps the corpus simple.
# ---------------------------------------------------------------------------


def _patch_budget_resolvers(runner: TurnRunner) -> None:
    def _runtime(self, session_key):  # noqa: ARG001, ARG002
        return 60.0

    def _max_iter(self, session_key, mi):  # noqa: ARG001, ARG002
        return mi if mi is not None else 10

    def _iter_t(self, session_key, it):  # noqa: ARG001, ARG002
        return it if it is not None else 30.0

    def _tool_t(self, session_key, tt):  # noqa: ARG001, ARG002
        return tt if tt is not None else 20.0

    def _req_t(self, session_key, rt):  # noqa: ARG001, ARG002
        return rt if rt is not None else 120.0

    def _retries(self, session_key, r):  # noqa: ARG001, ARG002
        return r if r is not None else 3

    runner._resolve_agent_runtime_timeout = _runtime.__get__(runner, TurnRunner)
    runner._resolve_agent_max_iterations = _max_iter.__get__(runner, TurnRunner)
    runner._resolve_agent_iteration_timeout = _iter_t.__get__(runner, TurnRunner)
    runner._resolve_agent_tool_timeout = _tool_t.__get__(runner, TurnRunner)
    runner._resolve_agent_request_timeout = _req_t.__get__(runner, TurnRunner)
    runner._resolve_agent_max_provider_retries = _retries.__get__(runner, TurnRunner)


def _patch_thinking(runner: TurnRunner) -> None:
    def _resolve_turn_thinking(self, turn):  # noqa: ARG001, ARG002
        return False

    runner._resolve_turn_thinking = _resolve_turn_thinking.__get__(runner, TurnRunner)


# ---------------------------------------------------------------------------
# Compaction / history-slice patches
# ---------------------------------------------------------------------------


def _patch_load_history(runner, *, return_value=None, raises=None, calls=None):
    async def _load(self, agent, session_key, *, trim_last_user=True):  # noqa: ARG001, ARG002
        if calls is not None:
            calls.append({"trim_last_user": trim_last_user})
        if raises is not None:
            raise raises("history boom")
        return return_value

    runner._load_history = _load.__get__(runner, TurnRunner)


def _patch_post_slice_probe(runner):
    """Hook _build_attachment_messages (first call past the slice)."""

    def _probe(self, *args, **kwargs):  # noqa: ARG001, ARG002
        snapshot = _capture_locals_at_post_slice()
        raise _SliceCapture(snapshot)

    runner._build_attachment_messages = _probe.__get__(runner, TurnRunner)


# ---------------------------------------------------------------------------
# Corpus — 13 cases per the design.
# ---------------------------------------------------------------------------


_CASE_BASE: dict[str, Any] = dict(
    session_key="agent:main:s1",
    history_return=None,
    history_raises=None,
    request_context_in=None,
    expected_final_request_context=None,
    history_has_persisted_user=True,
    preflight_compact_ratio=None,
    expected_threshold=0.85,
)


def _case(case_id: str, **overrides: Any) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = dict(_CASE_BASE)
    payload.update(overrides)
    return case_id, payload


_CORPUS: list[tuple[str, dict[str, Any]]] = [
    _case("no_durable_context"),
    _case("cron_session", session_key="cron:tick:s1"),
    _case(
        "durable_summaries_exist",
        history_return="SUMMARY-X",
        expected_final_request_context="SUMMARY-X",
    ),
    _case(
        "legacy_summary_marker_in_transcript",
        history_return="[Context Summary] body",
        expected_final_request_context="[Context Summary] body",
    ),
    _case(
        "summary_prepended_to_dynamic_context",
        history_return="SUMMARY-Y",
        request_context_in="DYNAMIC",
        expected_final_request_context="SUMMARY-Y\n\nDYNAMIC",
    ),
    _case("trim_last_user_true", history_has_persisted_user=True),
    _case("trim_last_user_false", history_has_persisted_user=False),
    _case(
        "configured_compaction_ratio_reaches_agent",
        preflight_compact_ratio=0.6,
        expected_threshold=0.6,
    ),
    _case("history_loader_raises", history_raises=RuntimeError),
]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _build_runner() -> TurnRunner:
    return TurnRunner(
        provider_selector=None,
        tool_registry=None,
        session_manager=None,
        skill_loader=None,
        usage_tracker=None,
        config=None,
        memory_sync_managers=None,
        model_catalog=_StubModelCatalog(),
        memory_retrievers=None,
        turn_capture_services=None,
        session_lock_provider=None,
        diagnostics_state=None,
        turn_hooks=None,
    )


def _setup_runner(case: dict[str, Any]) -> tuple[TurnRunner, dict[str, list]]:
    runner = _build_runner()
    selector = _StubSelector(
        "sel",
        current_model="claude-sonnet-4.5",
        resolve_returns=_StubProvider("override-resolved"),
    )
    _patch_resolver(runner, _StubProvider("p"), selector)
    _patch_builder(runner, [SimpleNamespace(name="t1")], object(), {"tool_profile": "agent"})
    _patch_ctx_mutators(runner)
    _patch_assemble_prompt(runner, "BASE", {})
    _patch_run_pipeline(
        runner,
        _make_turn_factory(metadata={"tool_profile": "agent"}, tool_defs=[]),
        provider=_StubProvider("post-pipeline"),
    )
    _patch_router_context(runner)
    _patch_resolve_prompt_config(
        runner,
        "FINAL",
        None,
        case["request_context_in"],
    )
    _patch_session_id(runner, "sess-1")
    _patch_budget_resolvers(runner)
    _patch_thinking(runner)
    _patch_memory_helpers(runner)
    _patch_observability(runner)

    call_log: dict[str, list] = {"history": []}
    _patch_load_history(
        runner,
        return_value=case["history_return"],
        raises=case["history_raises"],
        calls=call_log["history"],
    )
    _patch_post_slice_probe(runner)
    if case["preflight_compact_ratio"] is not None:
        runner._config = SimpleNamespace(preflight_compact_ratio=case["preflight_compact_ratio"])

    return runner, call_log


async def _drive(runner: TurnRunner, case: dict[str, Any]):
    captured = None
    raised = None
    yielded: list[Any] = []
    gen = runner._run_turn(
        message="hi",
        session_key=case["session_key"],
        agent_id="agent:main",
        model=None,
        attachments=[],
        tool_context=None,
        input_mode="user",
        persist_input=False,
        input_provenance=None,
        history_has_persisted_user=case["history_has_persisted_user"],
        semantic_message=None,
    )
    try:
        async for event in gen:
            yielded.append(event)
    except _SliceCapture as cap:
        captured = cap.snapshot
    except BaseException as exc:  # noqa: BLE001
        raised = type(exc)
    finally:
        await gen.aclose()
    return captured, yielded, raised


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


_CORPUS_IDS = [c[0] for c in _CORPUS]


@pytest.mark.parametrize("case_id,case", _CORPUS, ids=_CORPUS_IDS)
@pytest.mark.asyncio
async def test_history_load_stage_snapshot(
    case_id: str,
    case: dict[str, Any],
) -> None:
    """Drive each case through the stage; snapshot must match expected."""
    runner, call_log = _setup_runner(case)
    captured, yielded, raised = await _drive(runner, case)

    if case["history_raises"] is not None:
        # HistoryLoader exception propagates through _run_turn's terminal
        # handler -> ErrorEvent yielded; probe never fires.
        assert captured is None
        assert raised is None
        # The runner's error, then the one DoneEvent that ends every turn.
        assert len(yielded) == 2
        assert isinstance(yielded[0], ErrorEvent)
        assert isinstance(yielded[1], DoneEvent) and yielded[1].outcome.kind == "failed"
        assert yielded[0].code == "agent_error"
        # History must have been attempted once before raising.
        assert len(call_log["history"]) == 1
        return

    assert raised is None, f"{case_id} raised: {raised}"
    assert captured is not None, f"{case_id} captured nothing"

    expected_snapshot = {
        "outcome": "success",
        "agent_request_context_prompt_after": case["expected_final_request_context"],
        "agent_context_overflow_threshold": case["expected_threshold"],
    }
    assert captured == expected_snapshot, (
        f"case={case_id}: snapshot diverged.\n  expected={expected_snapshot}\n  actual  ={captured}"
    )

    # History always loaded once, with the caller's trim flag.
    assert len(call_log["history"]) == 1
    assert call_log["history"][0]["trim_last_user"] is case["history_has_persisted_user"]


def test_runner_has_no_before_turn_compaction_paths() -> None:
    """T3-upgrade, DB preflight and emergency trims were folded into the
    agent's CompactionService; the runner keeps none of them."""
    runner = _build_runner()
    for name in (
        "_maybe_compact_on_t3_upgrade",
        "_maybe_preflight_compact",
        "_record_emergency_ephemeral_compaction",
        "_emergency_compaction_overrides",
    ):
        assert not hasattr(runner, name), name
