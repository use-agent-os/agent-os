"""Deterministic guards and history must fire under ``strategy="llm_judge"``.

Spec D3 ungating: the router step used to gate the confidence gate,
complaint upgrade, kv-cache anti-downgrade, and history load/accumulation on
the literal strategy name ``"v4_phase3"``. These tests prove the guards run
for the LLM-judge strategy (with the judge call mocked) via the
``requires_history`` capability flag.
"""

from types import SimpleNamespace

import pytest

from agentos.agentos_router import llm_judge as llm_judge_module
from agentos.agentos_router.llm_judge import LLMJudgeStrategy, compute_flags
from agentos.engine.pipeline import TurnContext
from agentos.engine.steps import agentos_router as agentos_router_step
from agentos.engine.steps.agentos_router import (
    _detect_complaint,
    _strategy_cache_key,
    apply_agentos_router,
)
from agentos.gateway.config import GatewayConfig

# The D3 stable extra shape both judge-unavailable paths must emit: the
# LLMJudgeStrategy runtime path (pinned by test_llm_judge_strategy.py) and the
# _UnavailableJudgeStrategy construction-failure fallback, which hand-builds
# the same dict independently. Kept identical for logs/replay.
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


@pytest.fixture(autouse=True)
def reset_agentos_router_state(monkeypatch: pytest.MonkeyPatch) -> None:
    agentos_router_step._history_store.clear()
    agentos_router_step._strategy = None
    agentos_router_step._strategy_key = None
    yield
    agentos_router_step._history_store.clear()
    agentos_router_step._strategy = None
    agentos_router_step._strategy_key = None
    monkeypatch.undo()


def make_context(message: str, *, session_key: str = "test-judge-session") -> TurnContext:
    config = GatewayConfig()
    config.agentos_router.rollout_phase = "full"
    config.agentos_router.strategy = "llm_judge"
    return TurnContext(
        message=message,
        session_key=session_key,
        config=config,
        provider=None,
        model=config.llm.model,
        tool_defs=[],
        system_prompt="system",
    )


def mock_judge(monkeypatch: pytest.MonkeyPatch, route_classes: list[str]) -> list[str]:
    """Script LLMJudgeStrategy._judge to emit one verdict per call, in order."""
    remaining = list(route_classes)

    async def _scripted_judge(self, message, routing_history, flags, tool_defs):
        assert remaining, "judge called more times than scripted"
        return llm_judge_module._JudgeVerdict(
            route_class=remaining.pop(0),
            confidence=0.9,
            reason="scripted verdict",
        )

    monkeypatch.setattr(LLMJudgeStrategy, "_judge", _scripted_judge)
    return remaining


@pytest.mark.asyncio
async def test_anti_downgrade_fires_under_llm_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_judge(monkeypatch, ["R2", "R0"])
    session_key = "test-judge-anti-downgrade"

    routed1 = await apply_agentos_router(
        make_context("Diagnose this intermittent service failure.", session_key=session_key)
    )
    assert routed1.metadata["routing_source"] == "llm_judge"
    assert routed1.metadata["routed_tier"] == "c2"
    assert routed1.metadata["routing_history"], "history must accumulate under llm_judge"

    routed2 = await apply_agentos_router(
        make_context("and what about the retry path?", session_key=session_key)
    )
    extra = routed2.metadata["routing_extra"]

    assert routed2.metadata["routed_tier"] == "c2"
    assert extra["base_tier"] == "c0"
    assert extra["anti_downgrade_applied"] is True
    assert extra["previous_tier"] == "c2"
    assert extra["final_tier"] == "c2"


@pytest.mark.asyncio
async def test_complaint_upgrade_fires_under_llm_judge_with_vietnamese_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_judge(monkeypatch, ["R1"])

    routed = await apply_agentos_router(make_context("không đúng, làm lại đi"))
    extra = routed.metadata["routing_extra"]

    assert routed.metadata["routing_source"] == "llm_judge"
    assert routed.metadata["routed_tier"] == "c2"
    assert extra["complaint_detected"] is True
    assert extra["complaint_upgrade_applied"] is True
    assert any(term in {"không đúng", "làm lại"} for term in extra["complaint_terms"])


@pytest.mark.asyncio
async def test_complaint_upgrade_fires_under_llm_judge_with_english_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_judge(monkeypatch, ["R1"])

    routed = await apply_agentos_router(make_context("that's not right, try again"))
    extra = routed.metadata["routing_extra"]

    assert routed.metadata["routed_tier"] == "c2"
    assert extra["complaint_detected"] is True
    assert extra["complaint_upgrade_applied"] is True


@pytest.mark.asyncio
async def test_judge_strategy_constructed_for_llm_judge_strategy_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_judge(monkeypatch, ["R1"])
    ctx = make_context("Summarize this design doc in three bullet points.")

    routed = await apply_agentos_router(ctx)

    assert isinstance(agentos_router_step._strategy, LLMJudgeStrategy)
    assert routed.metadata["routing_source"] == "llm_judge"
    assert routed.metadata["routed_tier"] == "c1"


@pytest.mark.asyncio
async def test_judge_construction_failure_degrades_to_judge_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _exploding_init(self, *args, **kwargs) -> None:
        raise RuntimeError("judge construction boom")

    monkeypatch.setattr(LLMJudgeStrategy, "__init__", _exploding_init)
    ctx = make_context("Explain the difference between TCP and UDP.")

    routed = await apply_agentos_router(ctx)

    assert isinstance(agentos_router_step._strategy, agentos_router_step._UnavailableJudgeStrategy)
    assert routed.metadata["routing_source"] == "judge_unavailable"
    # The unavailable judge is still history-aware: guards produce final_tier.
    assert routed.metadata["routing_extra"]["final_tier"] == routed.metadata["routed_tier"]


@pytest.mark.asyncio
async def test_unavailable_judge_extra_matches_d3_contract() -> None:
    # The construction-failure fallback hand-builds the D3 extra dict
    # independently of LLMJudgeStrategy; pin its raw key-set so a dropped or
    # renamed key desyncs from the runtime judge-unavailable path loudly.
    strategy = agentos_router_step._UnavailableJudgeStrategy(RuntimeError("boom"))
    _tier, _confidence, source, extra = await strategy.classify("anything", ["c0", "c1", "c2"])

    assert source == "judge_unavailable"
    assert set(extra) == EXPECTED_EXTRA_KEYS
    assert extra["top1_label"] == extra["route_class"]
    assert extra["thinking_mode"] is not None
    assert extra["prompt_policy"] is not None
    assert extra["probabilities"] is None
    assert extra["margin"] is None
    assert extra["difficulty"] is None


def test_strategy_name_dispatches_and_defaults_to_llm_judge() -> None:
    assert agentos_router_step._strategy_name(SimpleNamespace(strategy="llm_judge")) == "llm_judge"
    assert agentos_router_step._strategy_name(SimpleNamespace(strategy="v4_phase3")) == "v4_phase3"
    assert agentos_router_step._strategy_name(SimpleNamespace(strategy=None)) == "llm_judge"
    assert agentos_router_step._strategy_name(SimpleNamespace(strategy="bogus")) == "llm_judge"
    assert agentos_router_step._strategy_name(SimpleNamespace()) == "llm_judge"


@pytest.mark.asyncio
async def test_tool_defs_forwarded_from_step_to_judge_classify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """apply_agentos_router must plumb ctx.tool_defs into strategy.classify so
    the agentic signal / R1 floor is live in production (not just in the unit
    test that calls classify directly)."""
    captured: dict[str, object] = {}

    async def _capturing_classify(self, message, valid_tiers, routing_history=None, **kwargs):
        captured["tool_defs"] = kwargs.get("tool_defs")
        return (
            "c1",
            1.0,
            "llm_judge",
            llm_judge_module.LLMJudgeStrategy._build_extra(
                self,
                route_class="R1",
                final_route_class="R1",
                confidence=1.0,
                flags=compute_flags(message),
                reason="captured",
            ),
        )

    monkeypatch.setattr(LLMJudgeStrategy, "classify", _capturing_classify)

    ctx = make_context("continue the agentic refactor")
    ctx.tool_defs = [object(), object(), object()]

    await apply_agentos_router(ctx)

    assert captured["tool_defs"] == ctx.tool_defs
    assert len(captured["tool_defs"]) == 3


@pytest.mark.asyncio
async def test_get_strategy_rebuilds_and_clears_history_on_judge_model_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A judge_model change must replace the cached strategy AND clear the
    per-session history store (spec D7 cache-invalidation case)."""
    config = GatewayConfig()
    config.agentos_router.strategy = "llm_judge"
    llm_cfg = config.llm

    strategy_a = agentos_router_step._get_strategy(config.agentos_router, llm_cfg)
    assert isinstance(strategy_a, LLMJudgeStrategy)

    # Seed history so we can prove the store is cleared on rebuild.
    agentos_router_step._history_store.set("some-session", [{"route_class": "R2"}])
    assert agentos_router_step._history_store.length("some-session") == 1

    # Same key -> same cached instance, history untouched.
    strategy_same = agentos_router_step._get_strategy(config.agentos_router, llm_cfg)
    assert strategy_same is strategy_a
    assert agentos_router_step._history_store.length("some-session") == 1

    # Change judge_model -> new strategy instance AND cleared history.
    config.agentos_router.judge_model = "different-judge-model"
    strategy_b = agentos_router_step._get_strategy(config.agentos_router, llm_cfg)
    assert isinstance(strategy_b, LLMJudgeStrategy)
    assert strategy_b is not strategy_a
    assert agentos_router_step._history_store.length("some-session") == 0


@pytest.mark.asyncio
async def test_get_strategy_rebuilds_on_auto_tier_model_change() -> None:
    """Under AUTO (judge_model=None, the default), editing a tier's model with no
    judge_*/llm.* change must still replace the cached strategy so the judge does
    not keep resolving against the OLD tier model until process restart."""
    config = GatewayConfig()
    config.agentos_router.strategy = "llm_judge"
    assert config.agentos_router.judge_model is None  # AUTO
    llm_cfg = config.llm

    strategy_a = agentos_router_step._get_strategy(config.agentos_router, llm_cfg)
    assert isinstance(strategy_a, LLMJudgeStrategy)

    # Same config -> same cached instance.
    assert agentos_router_step._get_strategy(config.agentos_router, llm_cfg) is strategy_a

    # Edit the cheapest text tier's model (no judge_*/llm.* field touched).
    tiers = dict(config.agentos_router.tiers)
    first_text = next(iter(tiers))
    tiers[first_text] = {**tiers[first_text], "model": "totally-different-model"}
    config.agentos_router.tiers = tiers

    strategy_b = agentos_router_step._get_strategy(config.agentos_router, llm_cfg)
    assert isinstance(strategy_b, LLMJudgeStrategy)
    assert strategy_b is not strategy_a, (
        "AUTO tier model edit must rebuild the cached strategy"
    )


def _cache_key_base() -> dict[str, object]:
    return {
        "strategy": "llm_judge",
        "confidence_threshold": 0.5,
        "judge_model": None,
        "judge_provider": None,
        "judge_base_url": None,
        "judge_api_key": None,
        "judge_input_max_chars": 4000,
        "judge_timeout_seconds": None,
        "routing_timeout_seconds": 10.0,
        "judge_short_circuit_enabled": True,
        "judge_short_circuit_allowlist": [],
    }


def test_strategy_cache_key_includes_judge_model_and_provider() -> None:
    base = _cache_key_base()
    llm_cfg = SimpleNamespace(provider="bankr")
    key_auto = _strategy_cache_key(SimpleNamespace(**base), llm_cfg)
    key_model = _strategy_cache_key(SimpleNamespace(**{**base, "judge_model": "judge-x"}), llm_cfg)
    key_provider = _strategy_cache_key(
        SimpleNamespace(**{**base, "judge_model": "judge-x", "judge_provider": "openrouter"}),
        llm_cfg,
    )

    assert key_auto != key_model
    assert key_model != key_provider
    assert len({key_auto, key_model, key_provider}) == 3


def test_strategy_cache_key_includes_judge_base_url_and_api_key() -> None:
    """Repointing the local judge endpoint (judge_base_url) or rotating its
    judge_api_key must rebuild the cached strategy, or _ensure_provider keeps a
    client built against the stale endpoint/key until process restart."""
    base = _cache_key_base()
    llm_cfg = SimpleNamespace(provider="bankr")
    explicit = {**base, "judge_model": "llama3"}
    key_no_local = _strategy_cache_key(SimpleNamespace(**explicit), llm_cfg)
    key_local = _strategy_cache_key(
        SimpleNamespace(**{**explicit, "judge_base_url": "http://localhost:11434/v1"}),
        llm_cfg,
    )
    key_local_moved = _strategy_cache_key(
        SimpleNamespace(**{**explicit, "judge_base_url": "http://localhost:1234/v1"}),
        llm_cfg,
    )
    key_local_keyed = _strategy_cache_key(
        SimpleNamespace(
            **{
                **explicit,
                "judge_base_url": "http://localhost:11434/v1",
                "judge_api_key": "sk-local",
            }
        ),
        llm_cfg,
    )

    assert len({key_no_local, key_local, key_local_moved, key_local_keyed}) == 4


def test_strategy_cache_key_includes_judge_tuning_fields() -> None:
    """A hot config reload that only changes a judge tuning field the strategy
    snapshots at __init__ must rebuild the cached strategy (findings #4/#12):
    the cache key changes for each of them so _get_strategy returns a fresh
    instance rather than silently ignoring the new value until restart."""
    base = _cache_key_base()
    llm_cfg = SimpleNamespace(provider="bankr")
    baseline = _strategy_cache_key(SimpleNamespace(**base), llm_cfg)

    variants = {
        "judge_input_max_chars": 8000,
        "judge_timeout_seconds": 2.5,
        "routing_timeout_seconds": 20.0,
        "judge_short_circuit_enabled": False,
        "judge_short_circuit_allowlist": ["ok", "thanks"],
    }
    keys = {baseline}
    for field, value in variants.items():
        key = _strategy_cache_key(SimpleNamespace(**{**base, field: value}), llm_cfg)
        assert key != baseline, f"cache key must change when {field} changes"
        keys.add(key)
    # Every field produces a distinct key (no collisions).
    assert len(keys) == len(variants) + 1


def test_strategy_cache_key_includes_llm_credential_fields() -> None:
    """The judge inherits llm.* credentials via _credentials_for (api_key,
    api_key_env, base_url) in addition to provider. A same-provider credential
    or endpoint change (e.g. rotating a leaked api_key) must rebuild the cached
    strategy so _ensure_provider does not keep a client built with the OLD
    credentials — otherwise every judge call authenticates with the revoked key
    and degrades to judge_unavailable until process restart."""
    base = _cache_key_base()
    router = SimpleNamespace(**base)

    def _llm(**overrides: object) -> SimpleNamespace:
        fields = {
            "provider": "bankr",
            "api_key": "sk-old",
            "api_key_env": "OLD_ENV",
            "base_url": "https://old.example",
        }
        fields.update(overrides)
        return SimpleNamespace(**fields)

    baseline = _strategy_cache_key(router, _llm())
    variants = {
        "api_key": "sk-rotated",
        "api_key_env": "NEW_ENV",
        "base_url": "https://new.example",
    }
    keys = {baseline}
    for field, value in variants.items():
        key = _strategy_cache_key(router, _llm(**{field: value}))
        assert key != baseline, f"cache key must change when llm.{field} changes"
        keys.add(key)
    assert len(keys) == len(variants) + 1


def _auto_tiers() -> dict[str, object]:
    return {
        "c0": {"model": "cheap-a", "provider": "bankr", "description": "cheap"},
        "c1": {"model": "mid", "provider": "bankr", "description": "mid"},
    }


def test_strategy_cache_key_includes_tiers_under_auto() -> None:
    """AUTO judge (judge_model=None) derives its target/rubric/fallback tier from
    the live tiers config, not from any keyed judge_* scalar. A same-provider tier
    model edit that touches no judge_*/llm.* field must still rebuild the cached
    strategy, or _get_strategy keeps returning an instance resolved against the
    OLD tier model until process restart."""
    base = {**_cache_key_base(), "tiers": _auto_tiers()}
    llm_cfg = SimpleNamespace(provider="bankr")
    baseline = _strategy_cache_key(SimpleNamespace(**base), llm_cfg)

    edited = _auto_tiers()
    edited["c0"] = {**edited["c0"], "model": "cheap-b"}
    key_model_edit = _strategy_cache_key(
        SimpleNamespace(**{**base, "tiers": edited}), llm_cfg
    )
    assert key_model_edit != baseline, "tier c0.model edit must rebuild the strategy"

    rubric = _auto_tiers()
    rubric["c1"] = {**rubric["c1"], "description": "changed description"}
    key_rubric_edit = _strategy_cache_key(
        SimpleNamespace(**{**base, "tiers": rubric}), llm_cfg
    )
    assert key_rubric_edit != baseline, "tier description edit must rebuild the strategy"


def test_strategy_cache_key_includes_tier_profile_and_default_tier() -> None:
    """Switching tier_profile (which rebuilds tiers) or default_tier (used by the
    unavailable fallback) must rebuild the cached strategy even when no judge_*
    scalar changes."""
    base = {
        **_cache_key_base(),
        "tiers": _auto_tiers(),
        "tier_profile": "bankr",
        "default_tier": "c1",
    }
    llm_cfg = SimpleNamespace(provider="bankr")
    baseline = _strategy_cache_key(SimpleNamespace(**base), llm_cfg)

    key_profile = _strategy_cache_key(
        SimpleNamespace(**{**base, "tier_profile": "openrouter"}), llm_cfg
    )
    key_default = _strategy_cache_key(
        SimpleNamespace(**{**base, "default_tier": "c2"}), llm_cfg
    )
    assert key_profile != baseline, "tier_profile change must rebuild the strategy"
    assert key_default != baseline, "default_tier change must rebuild the strategy"


def test_strategy_cache_key_tiers_fingerprint_is_hashable() -> None:
    """A nested tiers dict must not raise when the key is used as a dict key."""
    key = _strategy_cache_key(
        SimpleNamespace(**{**_cache_key_base(), "tiers": _auto_tiers()}),
        SimpleNamespace(provider="bankr"),
    )
    assert isinstance(hash(key), int)


def test_strategy_cache_key_allowlist_is_hashable() -> None:
    """A list allowlist must not raise when the key is used as a dict key."""
    base = _cache_key_base()
    key = _strategy_cache_key(
        SimpleNamespace(**{**base, "judge_short_circuit_allowlist": ["hi", "yo"]}),
        SimpleNamespace(provider="bankr"),
    )
    assert isinstance(hash(key), int)


def test_history_store_append_is_atomic_against_concurrent_clear() -> None:
    """Finding #2: the router step runs in a worker thread while
    commit_deferred_router_history runs on the main loop, so append and clear
    race across OS threads. The store must guard every access with its own lock
    so a concurrent clear cannot interleave *between* append's ``setdefault``
    (which hands back a list reference) and its follow-up ``history.append`` —
    which would orphan the just-appended entry (mutate a list no longer reachable
    as ``_entries[session]``), losing it from the store while ``append`` still
    reports it landed.

    This must be a *lock-only* invariant — it has to fail if ``with self._lock``
    is stripped from ``append``/``clear`` (the exact regression six review rounds
    fixed), not merely lean on the GIL making individual dict ops atomic.

    Construction: we drive a ``clear`` at the precise mid-append instant by
    seeding the session with a list subclass whose ``__len__`` — evaluated by
    ``append`` *after* ``setdefault`` returns the reference but *before* the
    entry is appended — calls ``store.clear()`` once.

    * With the real (non-reentrant) lock, ``append`` already holds ``_lock``, so
      this in-window ``clear()`` blocks — modelling how a cross-thread clear must
      wait rather than split the critical section. The appender never completes
      within the timeout: proof the lock serializes clear against append.
    * Without the lock, the in-window ``clear()`` succeeds, wipes ``_entries``,
      and the entry is appended to the orphaned list — lost from the store even
      though ``append`` returns a snapshot claiming success. (Verified: 100/100
      iterations orphan the entry with the lock stripped; 0/100 with it.)
    """
    import contextlib
    import threading

    from agentos.engine.steps.agentos_router import RoutingHistoryStore

    def _run_once(strip_lock: bool) -> tuple[bool, dict[str, object]]:
        store = RoutingHistoryStore()
        if strip_lock:
            store._lock = contextlib.nullcontext()  # type: ignore[assignment]

        class _ClearOnceList(list):
            armed = False

            def __len__(self) -> int:
                if _ClearOnceList.armed:
                    _ClearOnceList.armed = False
                    # A concurrent clear landing between setdefault and append.
                    # Under the real lock this same-thread call blocks (append
                    # holds the non-reentrant lock); without it, it orphans us.
                    store.clear()
                return super().__len__()

        store._entries["s"] = _ClearOnceList()  # type: ignore[assignment]
        result: dict[str, object] = {}

        def _appender() -> None:
            _ClearOnceList.armed = True
            entry, snapshot = store.append("s", {"route_class": "R2"}, max_entries=5)
            result["entry"] = entry
            result["snapshot"] = snapshot
            result["after"] = store.get("s")

        worker = threading.Thread(target=_appender, daemon=True)
        worker.start()
        worker.join(0.5)
        return worker.is_alive(), result

    # Real lock: the in-window clear blocks on _lock, so append never completes
    # within the timeout. This is the serialization the fix provides.
    blocked, _partial = _run_once(strip_lock=False)
    assert blocked, (
        "append did not serialize the mid-append clear behind its lock — the "
        "RoutingHistoryStore lock is missing or ineffective on append/clear"
    )

    # Sanity floor: the same construction with the lock stripped orphans the
    # entry — append reports success but the store loses it. This proves the
    # test is non-vacuous (it exercises the exact race the lock guards).
    stripped_blocked, stripped = _run_once(strip_lock=True)
    assert not stripped_blocked, "without a lock the mid-append clear must not block"
    entry = stripped["entry"]
    snapshot = stripped["snapshot"]
    after = stripped["after"]
    assert entry in snapshot  # append still reports the entry landed
    assert after is None or entry not in after, (
        "expected the lockless mid-append clear to orphan the entry"
    )

    # Quiescent append (no competing clear) still lands the entry exactly once.
    store = RoutingHistoryStore()
    _entry, history = store.append("s", {"route_class": "R3"}, max_entries=5)
    assert history == [history[0]]
    assert history[0]["route_class"] == "R3"
    assert history[0]["turn_index"] == 0


def test_history_store_append_trims_and_stamps_turn_index() -> None:
    from agentos.engine.steps.agentos_router import RoutingHistoryStore

    store = RoutingHistoryStore()
    last_history: list[dict] = []
    stamped: list[int] = []
    for i in range(8):
        entry, last_history = store.append("s", {"route_class": "R1", "n": i}, max_entries=5)
        stamped.append(entry["turn_index"])
    # Bounded to max_entries, keeping the most recent.
    assert len(last_history) == 5
    assert [e["n"] for e in last_history] == [3, 4, 5, 6, 7]
    # turn_index is a monotonic 0-based counter that keeps incrementing PAST the
    # trim boundary — it must not plateau at max_entries once the window fills.
    # (Regression guard for the round-9 finding: len(history)-derived indices
    # stamped 5,5,5,... for appends 5,6,7.)
    assert stamped == [0, 1, 2, 3, 4, 5, 6, 7]
    # The surviving window carries its true turn ordinals, not the list offsets.
    assert [e["turn_index"] for e in last_history] == [3, 4, 5, 6, 7]


def test_history_store_turn_index_resets_after_evict() -> None:
    from agentos.engine.steps.agentos_router import RoutingHistoryStore

    store = RoutingHistoryStore()
    for i in range(6):
        entry, _ = store.append("s", {"route_class": "R1", "n": i}, max_entries=5)
    assert entry["turn_index"] == 5
    # Evicting a terminated session must also drop its monotonic counter, so a
    # brand-new session reusing the key starts back at 0 rather than resuming
    # the stale count.
    assert store.evict("s") is True
    entry, _ = store.append("s", {"route_class": "R1", "n": 99}, max_entries=5)
    assert entry["turn_index"] == 0

    # clear() likewise resets counters across all sessions.
    for _ in range(3):
        entry, _ = store.append("s2", {"route_class": "R1"}, max_entries=5)
    store.clear()
    entry, _ = store.append("s2", {"route_class": "R1"}, max_entries=5)
    assert entry["turn_index"] == 0


@pytest.mark.asyncio
async def test_confidence_gate_is_inert_under_llm_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding #5: the judge always returns a fixed confidence of 1.0, so the
    deterministic confidence gate (fires only when confidence < threshold and
    tier != default_tier) can never downgrade a real judge verdict. Pin that
    intended-inert behavior so a future regression that makes confidence
    variable is caught: even at the maximum sane threshold (1.0) with a verdict
    above default_tier, the gate stays inert and the judge's tier survives.
    """
    mock_judge(monkeypatch, ["R3"])
    ctx = make_context("Diagnose this production outage and plan a safe rollback.")
    # 1.0 is the strongest a sane confidence threshold gets; 1.0 < 1.0 is
    # False, so the fixed-1.0 judge verdict is never gated down to default.
    ctx.config.agentos_router.confidence_threshold = 1.0
    ctx.config.agentos_router.default_tier = "c0"

    routed = await apply_agentos_router(ctx)
    extra = routed.metadata["routing_extra"]

    assert routed.metadata["routing_source"] == "llm_judge"
    assert extra["confidence"] == 1.0
    assert routed.metadata["routed_tier"] == "c3"
    assert extra.get("confidence_gate_applied") is False


def test_confidence_threshold_rejects_gate_disabling_value() -> None:
    """The confidence gate is inert under llm_judge only while threshold <= 1.0
    (the judge pins confidence=1.0). The test above pins the runtime inertness;
    this pins the enforcement that makes it a guarantee: the config must reject a
    threshold >1.0 so no deployment can silently turn the gate into a
    kill-switch that downgrades every non-default judged turn to default_tier."""
    from agentos.gateway.config import AgentOSRouterConfig

    with pytest.raises(ValueError, match="confidence_threshold"):
        AgentOSRouterConfig(confidence_threshold=2.0)


def test_detect_complaint_matches_vietnamese_terms() -> None:
    assert _detect_complaint("sai rồi, viết lại giúp mình")
    assert _detect_complaint("câu trả lời lạc đề quá")
    assert _detect_complaint("không phải cái mình hỏi")
    assert not _detect_complaint("viết giúp mình một hàm python đọc file csv")


def test_detect_complaint_ascii_terms_require_word_boundaries() -> None:
    """Finding #2: short ASCII tokens like ``sb`` must not fire inside
    innocuous words. Plain substring containment matched ``sb`` ⊂ ``husband``,
    spuriously bumping the tier and promoting the prompt policy."""
    # "sb" is a substring of "husband" but not a standalone word here.
    assert not _detect_complaint("my husband asked me to update the config")
    # The whole-word ASCII terms still match.
    assert _detect_complaint("sb")
    assert _detect_complaint("this is completely wrong, redo it")
    assert _detect_complaint("please try again")
    # "redo" the verb matches, but it must not fire inside "redone".
    assert not _detect_complaint("the migration was redone last night")


def test_compute_flags_vietnamese_high_risk_and_debug() -> None:
    flags = compute_flags("xoá bảng users trên database production rồi migrate lại")
    assert flags["high_risk"] is True

    flags = compute_flags("triển khai bản mới cho khách hàng vào tối nay")
    assert flags["high_risk"] is True

    flags = compute_flags("gỡ lỗi giúp mình đoạn code này, nó báo lỗi khi chạy")
    assert flags["debug"] is True

    flags = compute_flags("viết một bài thơ về mùa thu")
    assert flags["high_risk"] is False
    assert flags["debug"] is False
