"""Unit tests for the LLM-judge router strategy (mocked provider)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.agentos_router.llm_judge import (
    LLMJudgeStrategy,
    compute_flags,
    resolve_judge_target,
)
from agentos.provider.types import (
    DoneEvent,
    ErrorEvent,
    TextDeltaEvent,
    ToolUseEndEvent,
)

ALL_TIERS = ["c0", "c1", "c2", "c3"]

EXPECTED_EXTRA_KEYS = {
    "route_class",
    "top1_label",
    "final_route_class",
    "confidence",
    "thinking_mode",
    "prompt_policy",
    "flags",
    "reason",
    "probabilities",
    "margin",
    "difficulty",
}


def _tool_call_events(route_class: str, confidence: float = 0.9) -> list[Any]:
    return [
        ToolUseEndEvent(
            tool_use_id="tu_1",
            tool_name="emit_route",
            arguments={
                "route_class": route_class,
                "confidence": confidence,
                "reason": "test verdict",
            },
        ),
        DoneEvent(),
    ]


def _text_events(text: str) -> list[Any]:
    return [TextDeltaEvent(text=text), DoneEvent()]


def _error_events(message: str) -> list[Any]:
    return [ErrorEvent(message=message)]


class FakeProvider:
    """Scripted streaming provider: one event list per chat() call."""

    provider_name = "fake"

    def __init__(self, scripts: list[list[Any]]) -> None:
        self._scripts = list(scripts)
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages, tools=None, config=None):
        self.calls.append({"messages": messages, "tools": tools, "config": config})
        if not self._scripts:
            raise AssertionError("FakeProvider called more times than scripted")
        events = self._scripts.pop(0)

        async def _gen():
            for event in events:
                yield event

        return _gen()

    async def list_models(self):
        return []


class HangingProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__(scripts=[])

    def chat(self, messages, tools=None, config=None):
        self.calls.append({"messages": messages, "tools": tools, "config": config})

        async def _gen():
            await asyncio.sleep(30)
            yield DoneEvent()

        return _gen()


def _router_cfg(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "tiers": {
            "c0": {
                "provider": "bankr",
                "model": "deepseek-v4-flash",
                "description": "fast route for trivial chat",
            },
            "c1": {
                "provider": "bankr",
                "model": "minimax-m3",
                "description": "default balanced text model",
            },
            "c2": {
                "provider": "bankr",
                "model": "glm-5.2",
                "description": "stronger text model",
            },
            "c3": {
                "provider": "bankr",
                "model": "claude-opus-4.8",
                "description": "highest-quality reasoning model",
            },
            "image_model": {
                "provider": "bankr",
                "model": "minimax-m3",
                "description": "image route",
                "image_only": True,
            },
        },
        "default_tier": "c1",
        "routing_timeout_seconds": 5.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _llm_cfg(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "provider": "bankr",
        "model": "minimax-m3",
        "api_key": "sk-test",
        "api_key_env": "",
        "base_url": "https://gw.example/api/v1",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _strategy(
    provider: FakeProvider,
    router_cfg: SimpleNamespace | None = None,
    llm_cfg: SimpleNamespace | None = None,
) -> LLMJudgeStrategy:
    return LLMJudgeStrategy(
        router_cfg=router_cfg or _router_cfg(),
        llm_cfg=llm_cfg or _llm_cfg(),
        provider_factory=lambda *_args, **_kwargs: provider,
    )


# ---------------------------------------------------------------------------
# Forced tool call happy path
# ---------------------------------------------------------------------------


async def test_forced_tool_call_happy_path() -> None:
    provider = FakeProvider([_tool_call_events("R2")])
    strategy = _strategy(provider)

    tier, confidence, source, extra = await strategy.classify(
        "Refactor this multi-module parser and explain trade-offs", ALL_TIERS
    )

    assert tier == "c2"
    assert confidence == 1.0
    assert source == "llm_judge"
    assert extra["route_class"] == "R2"
    assert extra["final_route_class"] == "R2"
    assert extra["reason"] == "test verdict"

    call = provider.calls[0]
    cfg = call["config"]
    assert cfg.temperature == 0.0
    assert cfg.thinking is False
    assert cfg.tool_choice == {"type": "function", "function": {"name": "emit_route"}}
    assert [tool.name for tool in call["tools"]] == ["emit_route"]
    assert cfg.timeout < 5.0  # internal timeout below the router step budget


async def test_happy_path_clamps_to_valid_tiers() -> None:
    provider = FakeProvider([_tool_call_events("R3")])
    strategy = _strategy(provider)

    tier, _confidence, source, extra = await strategy.classify(
        "delete the production database", ["c0", "c1"]
    )

    # R3's mapped tier (c3) is above every valid tier, so it clamps to the
    # HIGHEST available tier — never down to the cheapest c0. A high-risk turn
    # must not silently collapse to the cheapest model.
    assert tier == "c1"
    assert source == "llm_judge"
    assert extra["route_class"] == "R3"
    assert extra["final_route_class"] == "R1"


# ---------------------------------------------------------------------------
# Text-JSON fallback + repair pass
# ---------------------------------------------------------------------------


async def test_text_json_fallback() -> None:
    payload = json.dumps({"route_class": "R3", "confidence": 0.8, "reason": "hard"})
    provider = FakeProvider([_text_events(f"Sure — here you go: {payload}")])
    strategy = _strategy(provider)

    tier, confidence, source, extra = await strategy.classify(
        "plan a safe rollback of the failed migration", ALL_TIERS
    )

    assert tier == "c3"
    assert confidence == 1.0
    assert source == "llm_judge"
    assert extra["route_class"] == "R3"
    assert len(provider.calls) == 1  # no repair round needed


async def test_repair_reprompt_recovers() -> None:
    payload = json.dumps({"route_class": "R1", "confidence": 0.7, "reason": "ok"})
    provider = FakeProvider(
        [
            _text_events("I think this is a medium one."),
            _text_events(payload),
        ]
    )
    strategy = _strategy(provider)

    tier, _confidence, source, _extra = await strategy.classify(
        "write a csv parser", ALL_TIERS
    )

    assert tier == "c1"
    assert source == "llm_judge"
    assert len(provider.calls) == 2
    # Repair call carries the raw output and asks for JSON only, no tools.
    repair_call = provider.calls[1]
    assert repair_call["tools"] is None
    assert repair_call["config"].tool_choice is None
    roles = [msg.role for msg in repair_call["messages"]]
    assert roles == ["user", "assistant", "user"]


def test_candidate_scan_skips_brace_bearing_prose() -> None:
    """Reasoning/local judges routinely emit stray braces before the verdict
    (think-traces, hedges like ``{maybe R1?}``). A greedy first-{-to-last-}
    match would span the stray brace and fail to parse; the brace-balanced
    candidate scan must skip it and recover the real verdict."""
    from agentos.agentos_router.llm_judge import _extract_verdict_from_text

    text = (
        "<think>the user wants {something}</think> "
        '{maybe R1?} … {"route_class": "R2", "confidence": 0.8, "reason": "x"}'
    )
    verdict = _extract_verdict_from_text(text)
    assert verdict is not None
    assert verdict.route_class == "R2"
    assert verdict.reason == "x"


def test_candidate_scan_ignores_braces_inside_strings() -> None:
    """Braces inside a JSON string value must not split the balanced span: an
    unescaped ``}`` inside a string must not be treated as the object's close."""
    from agentos.agentos_router.llm_judge import _extract_verdict_from_text

    text = (
        'noise {balanced?} then '
        '{"route_class": "R3", "confidence": 0.9, "reason": "use {x} now"}'
    )
    verdict = _extract_verdict_from_text(text)
    assert verdict is not None
    assert verdict.route_class == "R3"
    assert verdict.reason == "use {x} now"


def test_candidate_scan_returns_none_without_object() -> None:
    from agentos.agentos_router.llm_judge import _extract_verdict_from_text

    assert _extract_verdict_from_text("no json {here at all") is None
    assert _extract_verdict_from_text("") is None


def test_verdict_recovers_past_unbalanced_prose_quote() -> None:
    """Finding (round 9): an UNBALANCED double-quote in a reasoning/local judge's
    think-trace BEFORE the verdict must not trap the string-aware scanner in
    string state and swallow the verdict's braces. Prose-level quotes carry no
    JSON structure, so the scanner must ignore them (string state is entered only
    inside an object). A regression here silently drops a high-risk turn to the
    default tier via ``judge_unavailable``."""
    from agentos.agentos_router.llm_judge import _extract_verdict_from_text

    # Single stray double-quote in the trace, then the real verdict.
    text = (
        'the user said "delete prod db then migrate the whole thing '
        '{"route_class": "R3", "confidence": 0.9, "reason": "destructive prod op"}'
    )
    verdict = _extract_verdict_from_text(text)
    assert verdict is not None
    assert verdict.route_class == "R3"
    assert verdict.reason == "destructive prod op"


def test_verdict_recovers_via_brace_only_fallback() -> None:
    """Pathological case: a leading brace-balanced object contains an
    UNTERMINATED string whose opening quote swallows that object's closing brace
    under the string-aware scan, so the string-aware pass never balances it and
    yields no verdict. The brace-only fallback pass ignores quotes entirely,
    isolates the malformed leading object (which json.loads rejects) and then the
    real verdict object — recovering it rather than degrading to
    judge_unavailable."""
    from agentos.agentos_router.llm_judge import (
        _extract_verdict_from_text,
        _iter_json_dicts,
    )

    text = (
        '{"x": "unterminated} '
        '{"route_class": "R2", "confidence": 0.7, "reason": "refactor"}'
    )
    # Precondition: the string-aware pass alone finds NO verdict (the fallback is
    # what does the work) — otherwise this test wouldn't exercise the fallback.
    assert not any(
        d.get("route_class") == "R2" for d in _iter_json_dicts(text, string_aware=True)
    )

    verdict = _extract_verdict_from_text(text)
    assert verdict is not None
    assert verdict.route_class == "R2"


async def test_text_json_fallback_with_reasoning_trace() -> None:
    """The text-JSON fallback is the OPERATIVE path for judges that cannot honor
    a forced tool_choice (Ollama / local endpoints), where reasoning models wrap
    output in brace-bearing think-traces. Such a trace must still classify — not
    degrade to judge_unavailable."""
    payload = json.dumps({"route_class": "R3", "confidence": 0.8, "reason": "hard"})
    trace = f"<think>hmm {{maybe R2}} but production → {{escalate}}</think>{payload}"
    provider = FakeProvider([_text_events(trace)])
    strategy = _strategy(provider)

    tier, confidence, source, extra = await strategy.classify(
        "delete the production users table then migrate", ALL_TIERS
    )

    assert tier == "c3"
    assert source == "llm_judge"
    assert extra["route_class"] == "R3"
    assert len(provider.calls) == 1  # no repair round needed


async def test_repair_reprompt_recovers_with_brace_bearing_trace() -> None:
    """Even when both the initial call AND the repair round wrap the verdict in
    brace-bearing prose, the balanced scan must recover the verdict rather than
    degrading to judge_unavailable."""
    payload = json.dumps({"route_class": "R1", "confidence": 0.7, "reason": "ok"})
    provider = FakeProvider(
        [
            _text_events("I think {this} is medium {ish}."),
            _text_events(f"<think>{{R1?}} yes</think> {payload}"),
        ]
    )
    strategy = _strategy(provider)

    tier, _confidence, source, _extra = await strategy.classify(
        "write a csv parser", ALL_TIERS
    )

    assert tier == "c1"
    assert source == "llm_judge"
    assert len(provider.calls) == 2


async def test_classify_recovers_when_both_rounds_carry_unbalanced_quote() -> None:
    """Finding (round 9): a reasoning/local judge whose think-trace consistently
    emits an UNBALANCED double-quote before the verdict would, on the OLD
    string-aware scanner, degrade to judge_unavailable on the initial round AND
    the repair round — silently dropping a high-risk ``delete prod db`` turn to
    the default tier on EVERY turn. The hardened scanner must classify R3 from the
    first round (no wasted repair) and never reach judge_unavailable."""
    trace = (
        'the user said "delete the production database and migrate '
        '{"route_class": "R3", "confidence": 0.9, "reason": "destructive prod op"}'
    )
    provider = FakeProvider([_text_events(trace)])
    strategy = _strategy(provider)

    tier, _confidence, source, extra = await strategy.classify(
        "delete prod db then run the migration", ALL_TIERS
    )

    assert tier == "c3"
    assert source == "llm_judge"
    assert extra["route_class"] == "R3"
    assert len(provider.calls) == 1  # recovered on the first round, no repair


async def test_text_json_fallback_skips_leading_non_verdict_object() -> None:
    """A valid verdict preceded by another VALID JSON object (a reasoning/analysis
    blob) must still classify: the candidate scan validates route_class per
    object, so ``{"analysis": ...}`` before the real verdict is skipped rather
    than committed to (which would degrade to judge_unavailable). This is the
    exact shape emitted by Ollama / local / reasoning judges that can't honor a
    forced tool_choice."""
    analysis = json.dumps({"analysis": "this request is complex"})
    verdict = json.dumps({"route_class": "R2", "confidence": 0.8, "reason": "refactor"})
    provider = FakeProvider([_text_events(f"{analysis}\n{verdict}")])
    strategy = _strategy(provider)

    tier, confidence, source, extra = await strategy.classify(
        "refactor this multi-module parser", ALL_TIERS
    )

    assert tier == "c2"
    assert source == "llm_judge"
    assert extra["route_class"] == "R2"
    assert len(provider.calls) == 1  # first-parseable-object bug would repair/fail


async def test_text_json_fallback_skips_invalid_route_class_object() -> None:
    """A self-correction where an out-of-range route_class object precedes the
    corrected verdict must recover the valid one, not stop at the first dict."""
    bad = json.dumps({"route_class": "R5", "confidence": 1.2, "reason": "too high"})
    good = json.dumps({"route_class": "R2", "confidence": 0.8, "reason": "corrected"})
    provider = FakeProvider([_text_events(f"{bad} … actually {good}")])
    strategy = _strategy(provider)

    tier, _confidence, source, extra = await strategy.classify(
        "refactor this parser", ALL_TIERS
    )

    assert tier == "c2"
    assert source == "llm_judge"
    assert extra["route_class"] == "R2"
    assert len(provider.calls) == 1


async def test_repair_reprompt_skips_leading_non_verdict_object() -> None:
    """Even in the repair round (which has no further fallback), a valid verdict
    preceded by a leading non-verdict object must be recovered."""
    thought = json.dumps({"thought": "let me reconsider"})
    verdict = json.dumps({"route_class": "R1", "confidence": 0.7, "reason": "simple"})
    provider = FakeProvider(
        [
            _text_events("I am not sure yet."),
            _text_events(f"{thought}\n{verdict}"),
        ]
    )
    strategy = _strategy(provider)

    tier, _confidence, source, extra = await strategy.classify(
        "write a csv parser", ALL_TIERS
    )

    assert tier == "c1"
    assert source == "llm_judge"
    assert extra["route_class"] == "R1"
    assert len(provider.calls) == 2


def test_extract_verdict_from_text_returns_first_valid_verdict() -> None:
    """The verdict extractor validates route_class per candidate, so it skips a
    valid-but-non-verdict object and an out-of-range route_class object."""
    from agentos.agentos_router.llm_judge import _extract_verdict_from_text

    text = (
        '{"analysis": "complex"} then {"route_class": "R9", "confidence": 0.5, "reason": "bad"} '
        'finally {"route_class": "R2", "confidence": 0.8, "reason": "ok"}'
    )
    verdict = _extract_verdict_from_text(text)
    assert verdict is not None
    assert verdict.route_class == "R2"
    assert verdict.reason == "ok"

    assert _extract_verdict_from_text('{"analysis": "no verdict here"}') is None
    assert _extract_verdict_from_text("") is None


async def test_garbage_after_repair_returns_judge_unavailable() -> None:
    provider = FakeProvider(
        [
            _text_events("no json here"),
            _text_events("still no json"),
        ]
    )
    strategy = _strategy(provider)

    tier, confidence, source, extra = await strategy.classify("hello there world", ALL_TIERS)

    assert tier == "c1"  # default_tier
    assert confidence == 0.0
    assert source == "judge_unavailable"
    assert extra["reason"].startswith("judge_unavailable")
    assert len(provider.calls) == 2  # exactly ONE repair re-prompt, no retry loop


async def test_invalid_route_class_triggers_repair_then_unavailable() -> None:
    provider = FakeProvider(
        [
            _tool_call_events("R9"),
            _text_events("garbage"),
        ]
    )
    strategy = _strategy(provider)

    tier, _confidence, source, _extra = await strategy.classify("do something", ALL_TIERS)

    assert tier == "c1"
    assert source == "judge_unavailable"
    assert len(provider.calls) == 2


# ---------------------------------------------------------------------------
# Internal timeout
# ---------------------------------------------------------------------------


async def test_internal_timeout_returns_default_class() -> None:
    provider = HangingProvider()
    strategy = _strategy(provider, router_cfg=_router_cfg(judge_timeout_seconds=0.1))

    tier, confidence, source, extra = await strategy.classify("some question", ALL_TIERS)

    assert tier == "c1"
    assert confidence == 0.0
    assert source == "judge_unavailable"
    assert "timeout" in extra["reason"]


async def test_provider_error_event_returns_judge_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding #4: a stream 'error' event raises _JudgeCallError inside
    _call_provider, which classify() catches via the generic except ->
    judge_unavailable + 'llm_judge.call_failed' log. Distinct D4 branch from
    timeout / unparseable output."""
    from agentos.agentos_router import llm_judge as llm_judge_module

    logged: list[tuple[str, dict[str, Any]]] = []
    orig_warning = llm_judge_module.log.warning
    monkeypatch.setattr(
        llm_judge_module.log,
        "warning",
        lambda event, *a, **k: (logged.append((event, k)), orig_warning(event, *a, **k))[
            -1
        ],
    )

    provider = FakeProvider([_error_events("upstream 503: model overloaded")])
    strategy = _strategy(provider)

    tier, confidence, source, extra = await strategy.classify(
        "refactor this parser", ALL_TIERS
    )

    assert tier == "c1"  # default_tier
    assert confidence == 0.0
    assert source == "judge_unavailable"
    # The provider's error message surfaces in the reason.
    assert "upstream 503: model overloaded" in extra["reason"]
    assert extra["reason"].startswith("judge_unavailable")
    assert any(event == "llm_judge.call_failed" for event, _ in logged)
    # No repair round is attempted on a provider error.
    assert len(provider.calls) == 1


def test_internal_timeout_defaults_below_router_step_budget() -> None:
    strategy = LLMJudgeStrategy(
        router_cfg=_router_cfg(routing_timeout_seconds=5.0),
        llm_cfg=_llm_cfg(),
        provider_factory=lambda *_a, **_k: None,
    )
    assert strategy._timeout < 5.0
    assert strategy._timeout > 0.0


def test_internal_timeout_clamped_below_outer_budget_when_explicit_too_large() -> None:
    """Finding #3: the outer router step wraps a NON-cancellable to_thread with
    asyncio.wait_for(routing_timeout_seconds). An operator may set
    judge_timeout_seconds >= routing_timeout_seconds (config only validates
    gt=0.0); the judge's internal timeout must still be clamped strictly below
    the outer budget so it — not the un-cancellable outer wait_for — is the
    operative timeout and the worker thread is never orphaned."""
    strategy = LLMJudgeStrategy(
        router_cfg=_router_cfg(
            routing_timeout_seconds=5.0, judge_timeout_seconds=100.0
        ),
        llm_cfg=_llm_cfg(),
        provider_factory=lambda *_a, **_k: None,
    )
    assert strategy._timeout < 5.0

    # A small explicit timeout below the ceiling is honored verbatim.
    strategy_small = LLMJudgeStrategy(
        router_cfg=_router_cfg(routing_timeout_seconds=5.0, judge_timeout_seconds=1.0),
        llm_cfg=_llm_cfg(),
        provider_factory=lambda *_a, **_k: None,
    )
    assert strategy_small._timeout == 1.0


@pytest.mark.parametrize("budget", [0.5, 0.6, 1.0, 5.0, 10.0, 30.0])
def test_internal_timeout_always_strictly_below_outer_budget(budget: float) -> None:
    """Finding #3: the inner judge timeout must stay STRICTLY below the outer
    routing_timeout_seconds for every legal budget (config only validates
    gt=0.0) — including tiny budgets where the 0.5s floor would otherwise meet
    or exceed the budget and orphan the worker thread."""
    strategy = LLMJudgeStrategy(
        router_cfg=_router_cfg(routing_timeout_seconds=budget),
        llm_cfg=_llm_cfg(),
        provider_factory=lambda *_a, **_k: None,
    )
    assert 0.0 < strategy._timeout < budget


@pytest.mark.parametrize(
    ("budget", "explicit"),
    [
        (0.1, 0.05),  # explicit below a sub-0.1 ceiling
        (0.1, 0.5),  # explicit above the whole budget
        (0.1, 100.0),  # explicit far above the budget
        (0.2, 0.05),  # explicit below the fixed 0.1 floor
        (0.5, 0.01),  # tiny explicit
    ],
)
def test_internal_timeout_strictly_below_budget_with_explicit_sub_budget(
    budget: float, explicit: float
) -> None:
    """Finding #3: an EXPLICIT judge_timeout combined with a tiny (<=0.1s)
    routing budget must still land strictly below the outer budget. Both
    routing_timeout_seconds and judge_timeout_seconds are validated only
    gt=0.0, so e.g. budget=0.1 + judge_timeout=0.05 is legal config; the old
    fixed 0.1s floor pushed the inner timeout to == budget, tripping the
    construction assert (asserts on) or silently orphaning the worker thread
    (python -O, asserts off). Constructing the strategy must not raise and the
    resulting inner timeout must stay strictly below the outer budget."""
    strategy = LLMJudgeStrategy(
        router_cfg=_router_cfg(
            routing_timeout_seconds=budget, judge_timeout_seconds=explicit
        ),
        llm_cfg=_llm_cfg(),
        provider_factory=lambda *_a, **_k: None,
    )
    assert 0.0 < strategy._timeout < budget


# ---------------------------------------------------------------------------
# probe_local_judge — loop-safe connectivity check (spec D2, WebUI/RPC path)
# ---------------------------------------------------------------------------


def _patch_probe_provider(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> None:
    """Make probe_local_judge's self-built strategy use ``provider``.

    probe_local_judge constructs its own LLMJudgeStrategy internally (no factory
    injection), so it goes through build_provider. Patch that symbol in the
    llm_judge module to hand back the scripted FakeProvider instead of dialing a
    real endpoint."""
    from agentos.agentos_router import llm_judge as llm_judge_module

    monkeypatch.setattr(
        llm_judge_module,
        "build_provider",
        lambda **_kwargs: provider,
    )


def test_probe_local_judge_success_no_running_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interactive CLI path calls probe_local_judge from synchronous code
    (no running loop). A reachable endpoint returning a usable verdict -> None."""
    from agentos.agentos_router.llm_judge import probe_local_judge

    _patch_probe_provider(monkeypatch, FakeProvider([_tool_call_events("R1")]))

    assert probe_local_judge("http://localhost:11434/v1", "llama3", "sk-local") is None


def test_probe_local_judge_success_inside_running_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Findings #1-#5 (blocker): the WebUI/RPC onboarding path reaches
    probe_local_judge from INSIDE the running gateway event loop (the
    onboarding.router.configure async handler awaits upsert_router directly).

    A bare asyncio.run raises RuntimeError there; the probe's broad except then
    reports a bogus 'not usable' error for a perfectly reachable endpoint. The
    loop-safe probe must instead drive the classify coroutine off-loop and
    return None for a reachable endpoint even when a loop is already running."""
    from agentos.agentos_router.llm_judge import probe_local_judge

    _patch_probe_provider(monkeypatch, FakeProvider([_tool_call_events("R2")]))

    async def _drive() -> str | None:
        # Called on a running loop, exactly like the RPC handler chain.
        return probe_local_judge("http://localhost:11434/v1", "llama3", "sk-local")

    result = asyncio.run(_drive())
    assert result is None


def test_probe_local_judge_surfaces_unusable_endpoint_inside_running_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even inside a running loop, a reachable-but-wrong-model endpoint (one that
    never returns a usable routing decision) must still be reported as unusable —
    not masked by an asyncio-loop RuntimeError."""
    from agentos.agentos_router.llm_judge import probe_local_judge

    # Garbage text + garbage repair -> judge_unavailable.
    _patch_probe_provider(
        monkeypatch,
        FakeProvider([_text_events("not json"), _text_events("still not json")]),
    )

    async def _drive() -> str | None:
        return probe_local_judge("http://localhost:11434/v1", "llama3", None)

    result = asyncio.run(_drive())
    assert result == "the endpoint did not return a usable routing decision"


def test_internal_timeout_fallback_matches_config_default() -> None:
    """Finding #3: a duck-typed router_cfg missing routing_timeout_seconds must
    fall back to the SAME default the outer router step reads
    (DEFAULT_ROUTING_TIMEOUT_SECONDS == config default 10.0), or the two
    budgets desync and the inner-wins guarantee breaks."""
    from agentos.agentos_router.llm_judge import DEFAULT_ROUTING_TIMEOUT_SECONDS

    assert DEFAULT_ROUTING_TIMEOUT_SECONDS == 10.0
    cfg = SimpleNamespace(
        tiers=_router_cfg().tiers, default_tier="c1"
    )  # no routing_timeout_seconds
    strategy = LLMJudgeStrategy(
        router_cfg=cfg,
        llm_cfg=_llm_cfg(),
        provider_factory=lambda *_a, **_k: None,
    )
    assert 0.0 < strategy._timeout < DEFAULT_ROUTING_TIMEOUT_SECONDS


def test_resolution_warns_when_provider_cannot_force_tool_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding #1: an Ollama-backed judge cannot force cfg.tool_choice, so the
    D1 structured-output contract is not guaranteed. Resolution must warn rather
    than degrade silently."""
    from agentos.agentos_router import llm_judge as llm_judge_module

    events: list[str] = []
    monkeypatch.setattr(
        llm_judge_module.log,
        "warning",
        lambda event, *a, **k: events.append(event),
    )

    strategy = LLMJudgeStrategy(
        router_cfg=_router_cfg(judge_model="llama-guard", judge_provider="ollama"),
        llm_cfg=_llm_cfg(provider="ollama"),
        provider_factory=lambda *_a, **_k: None,
    )
    target = strategy._resolve_target()

    assert target is not None
    assert "llm_judge.forced_tool_choice_unsupported" in events


def test_resolution_no_forced_tool_choice_warning_for_capable_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.agentos_router import llm_judge as llm_judge_module

    events: list[str] = []
    monkeypatch.setattr(
        llm_judge_module.log,
        "warning",
        lambda event, *a, **k: events.append(event),
    )

    strategy = LLMJudgeStrategy(
        router_cfg=_router_cfg(),
        llm_cfg=_llm_cfg(provider="bankr"),
        provider_factory=lambda *_a, **_k: None,
    )
    strategy._resolve_target()

    assert "llm_judge.forced_tool_choice_unsupported" not in events


# ---------------------------------------------------------------------------
# Greeting/ack short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("greeting", ["hi", "Hello!", "thanks", "chào bạn", "cảm ơn", "你好"])
async def test_short_circuit_skips_llm_call(greeting: str) -> None:
    provider = FakeProvider([])  # any chat() call would raise
    strategy = _strategy(provider)

    tier, confidence, source, extra = await strategy.classify(greeting, ALL_TIERS)

    assert tier == "c0"
    assert confidence == 1.0
    assert source == "llm_judge"
    assert extra["route_class"] == "R0"
    assert provider.calls == []


async def test_short_circuit_rejects_long_and_nonexact_messages() -> None:
    provider = FakeProvider([_tool_call_events("R1"), _tool_call_events("R1")])
    strategy = _strategy(provider)

    await strategy.classify("hi, can you delete the prod db for me please", ALL_TIERS)
    await strategy.classify("hi there friend how are you doing today??", ALL_TIERS)

    assert len(provider.calls) == 2


async def test_short_circuit_configurable_off() -> None:
    provider = FakeProvider([_tool_call_events("R0")])
    strategy = _strategy(
        provider, router_cfg=_router_cfg(judge_short_circuit_enabled=False)
    )

    tier, _confidence, source, _extra = await strategy.classify("hi", ALL_TIERS)

    assert tier == "c0"
    assert source == "llm_judge"
    assert len(provider.calls) == 1


async def test_short_circuit_allowlist_is_additive_not_replacement() -> None:
    """Finding #1: judge_short_circuit_allowlist is documented as additive
    ("Extra ... phrases"). A configured list must EXTEND the built-in default
    set, not replace it — so an operator adding one phrase keeps every built-in
    greeting/ack, including the Vietnamese/Chinese terms. If it replaced the
    defaults, those trivial turns would hit the judge on every message."""
    provider = FakeProvider([])  # any chat() call would raise → judge must be skipped
    strategy = _strategy(
        provider, router_cfg=_router_cfg(judge_short_circuit_allowlist=["yep"])
    )

    # The configured extra phrase short-circuits.
    tier, _c, source, extra = await strategy.classify("yep", ALL_TIERS)
    assert tier == "c0"
    assert source == "llm_judge"
    assert extra["route_class"] == "R0"

    # Built-in defaults (en / vi / zh) still short-circuit — not replaced.
    for phrase in ("hi", "thanks", "cảm ơn", "你好"):
        tier, _c, source, extra = await strategy.classify(phrase, ALL_TIERS)
        assert tier == "c0", phrase
        assert extra["route_class"] == "R0", phrase

    # No judge call was ever made.
    assert provider.calls == []


async def test_short_circuit_skipped_for_agentic_ack_defers_to_judge() -> None:
    """Finding #1: a bare ack mid-workstream (tool_defs non-empty) must not
    short-circuit to R0 — spec D1 guarantees an agentic turn is never routed
    below R1. The judge must run so its 'agentic → not below R1' rule holds."""
    provider = FakeProvider([_tool_call_events("R1")])
    strategy = _strategy(provider)

    tier, _confidence, source, extra = await strategy.classify(
        "ok", ALL_TIERS, tool_defs=[{"name": "run_shell"}]
    )

    # The judge was consulted (no R0 short-circuit) and the R1 floor holds.
    assert len(provider.calls) == 1
    assert source == "llm_judge"
    assert tier == "c1"
    assert extra["route_class"] == "R1"
    assert extra["flags"]["agentic"] is True


async def test_short_circuit_still_applies_for_non_agentic_ack() -> None:
    """The agentic guard must not disable the short-circuit for ordinary
    trivial turns (no tool_defs) — those still skip the judge."""
    provider = FakeProvider([])  # any chat() call would raise
    strategy = _strategy(provider)

    tier, _confidence, source, extra = await strategy.classify("ok", ALL_TIERS)

    assert provider.calls == []
    assert tier == "c0"
    assert source == "llm_judge"
    assert extra["route_class"] == "R0"


# ---------------------------------------------------------------------------
# Judge model resolution chain (spec D2)
# ---------------------------------------------------------------------------


def test_resolution_explicit_judge_model_and_provider() -> None:
    router_cfg = _router_cfg(judge_model="judge-x", judge_provider="openrouter")
    assert resolve_judge_target(router_cfg, _llm_cfg()) == ("openrouter", "judge-x", "explicit")


def test_resolution_explicit_model_inherits_llm_provider() -> None:
    router_cfg = _router_cfg(judge_model="judge-x")
    assert resolve_judge_target(router_cfg, _llm_cfg()) == (
        "bankr",
        "judge-x",
        "explicit",
    )


def test_resolution_auto_uses_c0_tier() -> None:
    assert resolve_judge_target(_router_cfg(), _llm_cfg()) == (
        "bankr",
        "deepseek-v4-flash",
        "auto",
    )


def test_resolution_skips_image_only_and_missing_tiers() -> None:
    router_cfg = _router_cfg()
    router_cfg.tiers = {
        "c0": {"provider": "bankr", "model": "vision-x", "image_only": True},
        "c1": {"provider": "bankr", "model": ""},
        "c2": {"provider": "bankr", "model": "glm-5.2"},
    }
    assert resolve_judge_target(router_cfg, _llm_cfg()) == (
        "bankr",
        "glm-5.2",
        "auto",
    )


def test_resolution_nothing_resolvable_returns_none() -> None:
    router_cfg = _router_cfg()
    router_cfg.tiers = {
        "image_model": {"provider": "p", "model": "m", "image_only": True},
    }
    assert resolve_judge_target(router_cfg, _llm_cfg()) is None


def test_resolution_auto_is_strictly_none_not_empty_string() -> None:
    # AUTO is ``judge_model is None`` (spec D2). A blank/whitespace judge_model
    # (e.g. a hand-edited TOML with ``judge_model = ""``) must NOT resolve to an
    # unusable explicit target with an empty model id — it falls through to AUTO
    # rather than building a provider against model="".
    router_cfg = _router_cfg(judge_model="")
    provider, model, source = resolve_judge_target(router_cfg, _llm_cfg())
    assert source == "auto"
    assert model == "deepseek-v4-flash"

    router_cfg = _router_cfg(judge_model="   ")
    provider, model, source = resolve_judge_target(router_cfg, _llm_cfg())
    assert source == "auto"
    assert model == "deepseek-v4-flash"


# ---------------------------------------------------------------------------
# Local OpenAI-compatible judge endpoint (spec D2 "Local endpoint")
# ---------------------------------------------------------------------------


def test_resolution_local_endpoint_source_is_local() -> None:
    """A judge_base_url with an explicit judge_model resolves to source="local"
    against the generic local provider id, regardless of llm.provider."""
    from agentos.agentos_router.llm_judge import LOCAL_JUDGE_PROVIDER_ID

    router_cfg = _router_cfg(
        judge_model="llama3", judge_base_url="http://localhost:11434/v1"
    )
    assert resolve_judge_target(router_cfg, _llm_cfg()) == (
        LOCAL_JUDGE_PROVIDER_ID,
        "llama3",
        "local",
    )


def test_resolution_local_endpoint_ignored_without_model() -> None:
    """judge_base_url only takes effect when judge_model is set (spec): with no
    model it falls through to the normal AUTO tier scan."""
    router_cfg = _router_cfg(judge_base_url="http://localhost:11434/v1")
    provider, model, source = resolve_judge_target(router_cfg, _llm_cfg())
    assert source == "auto"
    assert model == "deepseek-v4-flash"


def test_local_endpoint_client_built_against_base_url() -> None:
    """The judge client is built against judge_base_url with judge_api_key,
    bypassing the credential-must-match-llm.provider constraint."""
    build_calls: list[dict[str, Any]] = []

    def _factory(provider: str, model: str, api_key: str, base_url: str) -> Any:
        build_calls.append(
            {"provider": provider, "model": model, "api_key": api_key, "base_url": base_url}
        )
        return FakeProvider([_tool_call_events("R2")])

    strategy = LLMJudgeStrategy(
        router_cfg=_router_cfg(
            judge_model="llama3",
            judge_base_url="http://localhost:11434/v1",
            judge_api_key="sk-my-local",
        ),
        # llm.provider deliberately DIFFERENT from the local endpoint.
        llm_cfg=_llm_cfg(provider="bankr"),
        provider_factory=_factory,
    )

    tier, _confidence, source, _extra = asyncio.run(
        strategy.classify("refactor this parser", ALL_TIERS)
    )

    assert source == "llm_judge"
    assert tier == "c2"
    assert len(build_calls) == 1
    assert build_calls[0]["model"] == "llama3"
    assert build_calls[0]["base_url"] == "http://localhost:11434/v1"
    assert build_calls[0]["api_key"] == "sk-my-local"


def test_local_endpoint_uses_placeholder_api_key_when_unset() -> None:
    """Local endpoints typically need no key; the client still receives a
    non-empty placeholder token so the openai_compat Authorization header is
    always present."""
    build_calls: list[dict[str, Any]] = []

    def _factory(provider: str, model: str, api_key: str, base_url: str) -> Any:
        build_calls.append({"api_key": api_key, "base_url": base_url})
        return FakeProvider([_tool_call_events("R1")])

    strategy = LLMJudgeStrategy(
        router_cfg=_router_cfg(
            judge_model="llama3", judge_base_url="http://localhost:11434/v1"
        ),
        llm_cfg=_llm_cfg(provider="bankr"),
        provider_factory=_factory,
    )

    asyncio.run(strategy.classify("write a parser", ALL_TIERS))

    assert build_calls[0]["api_key"]  # non-empty placeholder
    assert build_calls[0]["base_url"] == "http://localhost:11434/v1"


def test_local_endpoint_has_credentials_bypasses_provider_match() -> None:
    from agentos.agentos_router.llm_judge import judge_provider_has_credentials

    # A local target is credentialed even though its provider id never matches
    # llm.provider.
    assert judge_provider_has_credentials("vllm", _llm_cfg(provider="bankr"), "local")
    # Without the local source, the same mismatch is uncredentialed.
    assert not judge_provider_has_credentials("vllm", _llm_cfg(provider="bankr"))


async def test_unresolvable_judge_degrades_to_judge_unavailable() -> None:
    router_cfg = _router_cfg()
    router_cfg.tiers = {}
    strategy = LLMJudgeStrategy(
        router_cfg=router_cfg,
        llm_cfg=_llm_cfg(),
        provider_factory=lambda *_a, **_k: pytest.fail("must not build a provider"),
    )

    tier, _confidence, source, _extra = await strategy.classify("real question here", ALL_TIERS)

    assert tier == "c1"
    assert source == "judge_unavailable"


# ---------------------------------------------------------------------------
# Extra contract completeness (spec D3)
# ---------------------------------------------------------------------------


async def test_extra_contract_completeness_on_success() -> None:
    provider = FakeProvider([_tool_call_events("R2")])
    strategy = _strategy(provider)

    _tier, _confidence, _source, extra = await strategy.classify(
        "debug this failing production migration error", ALL_TIERS
    )

    assert set(extra) == EXPECTED_EXTRA_KEYS
    assert extra["top1_label"] == extra["route_class"]
    assert extra["confidence"] == 1.0
    assert extra["probabilities"] is None
    assert extra["margin"] is None
    assert extra["difficulty"] is None
    assert extra["thinking_mode"] is not None
    assert extra["prompt_policy"] is not None
    assert isinstance(extra["flags"], dict)
    assert extra["flags"]["high_risk"] is True
    assert extra["flags"]["debug"] is True


async def test_extra_contract_completeness_on_unavailable_and_short_circuit() -> None:
    hanging = HangingProvider()
    strategy = _strategy(hanging, router_cfg=_router_cfg(judge_timeout_seconds=0.1))
    _tier, _confidence, _source, unavailable_extra = await strategy.classify(
        "some question", ALL_TIERS
    )

    short_strategy = _strategy(FakeProvider([]))
    _tier, _confidence, _source, short_extra = await short_strategy.classify("hi", ALL_TIERS)

    for extra in (unavailable_extra, short_extra):
        assert set(extra) == EXPECTED_EXTRA_KEYS
        assert extra["thinking_mode"] is not None
        assert extra["prompt_policy"] is not None


# ---------------------------------------------------------------------------
# Prompt content: signals before truncation, recent decisions, agentic
# ---------------------------------------------------------------------------


async def test_signals_computed_before_truncation() -> None:
    provider = FakeProvider([_tool_call_events("R3")])
    strategy = _strategy(provider, router_cfg=_router_cfg(judge_input_max_chars=4000))
    # Risky keyword buried in the middle: outside head(800) and tail(1200).
    message = "x" * 2000 + " please delete the production database " + "y" * 3000

    await strategy.classify(message, ALL_TIERS)

    user_text = provider.calls[0]["messages"][0].content
    assert "high_risk" in user_text
    assert f"chars={len(message)}" in user_text
    assert "chars omitted" in user_text  # body actually truncated
    assert "delete the production database" not in user_text


async def test_no_truncation_within_budget() -> None:
    provider = FakeProvider([_tool_call_events("R1")])
    strategy = _strategy(provider)
    message = "a short message that fits the budget"

    await strategy.classify(message, ALL_TIERS)

    user_text = provider.calls[0]["messages"][0].content
    assert message in user_text
    assert "chars omitted" not in user_text


def test_truncate_body_small_budget_never_overlaps_or_bloats() -> None:
    # The head(800)+tail(1200) split budget is 2000 chars, but
    # judge_input_max_chars floors at 1000. A budget below HEAD+TAIL+marker
    # must hard-truncate: no overlap (duplicated middle), no output larger
    # than the input, and no negative "omitted" count.
    from agentos.agentos_router.llm_judge import _truncate_body

    text = "x" * 1500
    out = _truncate_body(text, 1000)

    assert out == text[:1000]
    assert len(out) == 1000  # never larger than max_chars
    assert len(out) < len(text)
    assert "chars omitted" not in out  # no elision marker, so no negative count
    assert "-" not in out


def test_truncate_body_head_tail_split_when_budget_is_large() -> None:
    # With a budget above HEAD+TAIL+marker, the head/tail elision path is used
    # and reports a correct, positive omitted count.
    from agentos.agentos_router.llm_judge import (
        _TRUNCATION_HEAD_CHARS,
        _TRUNCATION_TAIL_CHARS,
        _truncate_body,
    )

    text = "a" * 4000 + "MIDDLE" + "b" * 4000
    out = _truncate_body(text, 4000)

    assert out.startswith("a" * _TRUNCATION_HEAD_CHARS)
    assert out.endswith("b" * _TRUNCATION_TAIL_CHARS)
    assert "MIDDLE" not in out
    omitted = len(text) - _TRUNCATION_HEAD_CHARS - _TRUNCATION_TAIL_CHARS
    assert f"{omitted} chars omitted" in out
    assert omitted > 0


async def test_recent_decisions_and_agentic_signal_in_prompt() -> None:
    provider = FakeProvider([_tool_call_events("R2")])
    strategy = _strategy(provider)
    history = [
        {"final_route_class": "R1"},
        {"route_class": "R2"},
        {"final_route_class": "R2"},
    ]

    await strategy.classify(
        "continue the refactor",
        ALL_TIERS,
        routing_history=history,
        tool_defs=[object(), object()],
    )

    user_text = provider.calls[0]["messages"][0].content
    assert "[RECENT_DECISIONS: R1, R2, R2]" in user_text
    assert "agentic" in user_text
    assert "tools=2" in user_text


async def test_rubric_built_from_live_tier_descriptions() -> None:
    provider = FakeProvider([_tool_call_events("R1")])
    router_cfg = _router_cfg()
    router_cfg.tiers["c2"]["description"] = "custom-marker-description"
    strategy = _strategy(provider, router_cfg=router_cfg)

    await strategy.classify("write a parser", ALL_TIERS)

    system = provider.calls[0]["config"].system
    assert "custom-marker-description" in system
    assert "R2 (tier c2)" in system
    assert "choose the HIGHER" in system
    assert "Vietnamese" in system


async def test_flags_text_override_used_for_signals() -> None:
    provider = FakeProvider([_tool_call_events("R2")])
    strategy = _strategy(provider)

    _tier, _confidence, _source, extra = await strategy.classify(
        "short follow-up",
        ALL_TIERS,
        flags_text_override="Traceback (most recent call last): boom",
    )

    assert extra["flags"]["debug"] is True


def test_compute_flags_high_risk_and_debug() -> None:
    flags = compute_flags("please rollback the failed deploy, here is the traceback")
    assert flags["high_risk"] is True
    assert flags["debug"] is True
    assert flags["long_context"] is False


# ---------------------------------------------------------------------------
# Recursion absence (spec D7): the judge builds its own provider client and
# never re-enters the engine pipeline (apply_agentos_router / TurnRunner).
# ---------------------------------------------------------------------------


async def test_judge_uses_build_provider_and_never_reenters_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentos.agentos_router.llm_judge as llm_judge_module

    build_calls: list[dict[str, Any]] = []

    def _fake_build_provider(*, provider: str, model: str, api_key: str, base_url: str) -> Any:
        build_calls.append(
            {"provider": provider, "model": model, "api_key": api_key, "base_url": base_url}
        )
        return FakeProvider([_tool_call_events("R2")])

    monkeypatch.setattr(llm_judge_module, "build_provider", _fake_build_provider)

    # Structural recursion guard: the judge must build its own provider and
    # never reach back into the engine pipeline. A monkeypatch on
    # agentos_router_step.apply_agentos_router would be an inert guard — the
    # judge module never imports or references that symbol, so a patched
    # attribute is unreachable from the code under test and could never fire
    # even if a recursion were introduced via a fresh local import. Instead,
    # AST-parse the judge module and assert it contains no *import* of the
    # engine step / TurnRunner and no *name reference* to their entrypoints,
    # which is what actually makes recursion structurally impossible (spec D1).
    # (A raw substring scan would false-positive on prose in the module
    # docstring, e.g. "never re-enters TurnRunner".)
    import ast as _ast

    judge_source = Path(llm_judge_module.__file__).read_text(encoding="utf-8")
    tree = _ast.parse(judge_source)
    forbidden_modules = {
        "agentos.engine.steps.agentos_router",
        "agentos.engine.turn_runner",
    }
    forbidden_names = {"apply_agentos_router", "TurnRunner"}
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for alias in node.names:
                assert alias.name not in forbidden_modules, (
                    f"judge module imports {alias.name!r}; recursion into the "
                    "engine pipeline is possible"
                )
        elif isinstance(node, _ast.ImportFrom):
            module = node.module or ""
            assert module not in forbidden_modules, (
                f"judge module imports from {module!r}; recursion into the "
                "engine pipeline is possible"
            )
            for alias in node.names:
                assert alias.name not in forbidden_names, (
                    f"judge module imports {alias.name!r}; recursion into the "
                    "engine pipeline is possible"
                )
        elif isinstance(node, (_ast.Name, _ast.Attribute)):
            ref = node.id if isinstance(node, _ast.Name) else node.attr
            assert ref not in forbidden_names, (
                f"judge module references {ref!r}; recursion into the engine "
                "pipeline is possible"
            )

    # Build WITHOUT injecting a provider_factory so the strategy exercises its
    # real _default_provider_factory -> build_provider path.
    strategy = LLMJudgeStrategy(router_cfg=_router_cfg(), llm_cfg=_llm_cfg())

    tier, _confidence, source, _extra = await strategy.classify("refactor this parser", ALL_TIERS)

    assert source == "llm_judge"
    assert tier == "c2"
    assert len(build_calls) == 1
    assert build_calls[0]["model"] == "deepseek-v4-flash"
    assert build_calls[0]["api_key"] == "sk-test"
