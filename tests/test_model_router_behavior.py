import pytest

from agentos.engine.pipeline import TurnContext
from agentos.engine.steps import agentos_router as agentos_router_step
from agentos.engine.steps.agentos_router import apply_agentos_router
from agentos.gateway.config import GatewayConfig


class FakeStrategy:
    requires_history = True

    def __init__(self, tier: str, confidence: float, extra: dict) -> None:
        self.tier = tier
        self.confidence = confidence
        self.extra = extra
        self.calls = 0
        self.messages: list[str] = []

    async def classify(
        self,
        message: str,
        valid_tiers: list[str],
        routing_history: list[dict] | None = None,
    ) -> tuple[str, float, str, dict]:
        self.calls += 1
        self.messages.append(message)
        assert self.tier in valid_tiers
        return self.tier, self.confidence, "llm_judge", dict(self.extra)


class ContextAwareFakeStrategy(FakeStrategy):
    def __init__(self, tier: str, confidence: float, extra: dict) -> None:
        super().__init__(tier, confidence, extra)
        self.contexts: list[dict] = []

    async def classify(
        self,
        message: str,
        valid_tiers: list[str],
        routing_history: list[dict] | None = None,
        prev_assistant_text: str | None = None,
        prev_assistant_usage: dict | None = None,
        history_user_texts: list[str] | None = None,
        flags_text_override: str | None = None,
    ) -> tuple[str, float, str, dict]:
        self.calls += 1
        self.messages.append(message)
        self.contexts.append(
            {
                "routing_history": [dict(entry) for entry in routing_history or []],
                "prev_assistant_text": prev_assistant_text,
                "prev_assistant_usage": dict(prev_assistant_usage or {}),
                "history_user_texts": list(history_user_texts or []),
                "flags_text_override": flags_text_override,
            }
        )
        assert self.tier in valid_tiers
        return self.tier, self.confidence, "llm_judge", dict(self.extra)


class ExplodingJudgeStrategy:
    def __init__(self, *args, **kwargs) -> None:
        raise RuntimeError("failed to initialize LLM judge: no provider credentials")


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


def make_context(
    message: str,
    *,
    rollout_phase: str = "full",
    session_key: str = "test-session",
    raw_message: str | None = None,
    attachments: list[dict] | None = None,
) -> TurnContext:
    config = GatewayConfig()
    config.agentos_router.rollout_phase = rollout_phase
    return TurnContext(
        message=message,
        session_key=session_key,
        config=config,
        provider=None,
        model=config.llm.model,
        tool_defs=[],
        system_prompt="system",
        raw_message=raw_message,
        attachments=attachments or [],
    )


def fake_strategy(
    monkeypatch: pytest.MonkeyPatch,
    tier: str,
    confidence: float,
    extra: dict,
) -> FakeStrategy:
    strategy = FakeStrategy(tier, confidence, extra)
    monkeypatch.setattr(
        agentos_router_step, "_get_strategy", lambda _config, _llm_cfg=None: strategy
    )
    return strategy


def context_aware_fake_strategy(
    monkeypatch: pytest.MonkeyPatch,
    tier: str,
    confidence: float,
    extra: dict,
) -> ContextAwareFakeStrategy:
    strategy = ContextAwareFakeStrategy(tier, confidence, extra)
    monkeypatch.setattr(
        agentos_router_step, "_get_strategy", lambda _config, _llm_cfg=None: strategy
    )
    return strategy


@pytest.mark.asyncio
async def test_full_rollout_applies_routed_model_thinking_and_p0_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(
        monkeypatch,
        "c1",
        0.91,
        {
            "route_class": "R1",
            "thinking_mode": "T1",
            "prompt_policy": "P0",
        },
    )
    ctx = make_context("Summarize this short note.")
    baseline_model = ctx.model

    routed = await apply_agentos_router(ctx)

    assert routed.model == "minimax/minimax-m3"
    assert routed.metadata["routed_tier"] == "c1"
    assert routed.metadata["routed_model"] == "minimax/minimax-m3"
    assert routed.metadata["routing_applied"] is True
    assert routed.metadata["applied_model"] == "minimax/minimax-m3"
    assert routed.metadata["baseline_model"] == baseline_model
    assert routed.metadata["routing_confidence"] == 0.91
    assert routed.metadata["routing_source"] == "llm_judge"
    assert "savings_pct" in routed.metadata
    assert "savings_max_price_per_m" in routed.metadata
    assert "savings_routed_price_per_m" in routed.metadata
    assert routed.metadata["thinking_mode"] == "T1"
    assert routed.metadata["thinking_requested"] is True
    assert routed.metadata["thinking_level"] == "low"
    assert routed.metadata["prompt_policy"] == "P0"
    assert "[RESPONSE_POLICY: Answer directly" in routed.message


@pytest.mark.asyncio
async def test_router_reports_provider_state_loss_without_changing_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(
        monkeypatch,
        "c1",
        0.91,
        {
            "route_class": "R1",
            "thinking_mode": "T1",
            "prompt_policy": "P0",
        },
    )
    ctx = make_context("Continue the long task.")
    ctx.metadata["session_context_states"] = [
        {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "state_kind": "anthropic_compaction_block",
            "valid": True,
            "portable": False,
        },
        {
            "provider": "portable",
            "model": "",
            "state_kind": "structured_summary_v1",
            "valid": True,
            "portable": True,
        },
    ]

    routed = await apply_agentos_router(ctx)

    assert routed.model == "minimax/minimax-m3"
    diagnostic = routed.metadata["provider_state_continuity"]
    assert diagnostic["decision"] == "use_portable_fallback"
    assert diagnostic["provider_state_loss_risk"] is True
    assert diagnostic["candidate_provider"] == "openrouter"


@pytest.mark.asyncio
async def test_router_continuity_diagnostic_ignores_expired_provider_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(
        monkeypatch,
        "c1",
        0.91,
        {
            "route_class": "R1",
            "thinking_mode": "T1",
            "prompt_policy": "P0",
        },
    )
    ctx = make_context("Continue the long task.")
    ctx.metadata["session_context_states"] = [
        {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "state_kind": "anthropic_compaction_block",
            "created_at": 100,
            "expires_at": 150,
            "valid": True,
            "portable": False,
        },
        {
            "provider": "portable",
            "model": "",
            "state_kind": "structured_summary_v1",
            "created_at": 90,
            "valid": True,
            "portable": True,
        },
    ]

    routed = await apply_agentos_router(ctx)

    diagnostic = routed.metadata["provider_state_continuity"]
    assert diagnostic["decision"] == "use_portable_fallback"
    assert diagnostic["provider_state_loss_risk"] is False
    assert diagnostic["active_state_provider"] is None
    assert diagnostic["portable_fallback_available"] is True


@pytest.mark.asyncio
async def test_p2_prompt_hint_is_recorded_but_not_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(
        monkeypatch,
        "c3",
        0.97,
        {
            "route_class": "R3",
            "thinking_mode": "T3",
            "prompt_policy": "P2",
            "prompt_hint": "Use a careful plan before answering.",
        },
    )
    ctx = make_context("Plan a risky multi-step migration.")

    routed = await apply_agentos_router(ctx)

    assert routed.model == "anthropic/claude-opus-4.8"
    assert routed.metadata["routed_tier"] == "c3"
    assert routed.metadata["thinking_level"] == "high"
    assert routed.metadata["prompt_policy"] == "P2"
    assert routed.metadata["routing_extra"]["prompt_hint"] == "Use a careful plan before answering."
    assert "[RESPONSE_POLICY:" not in routed.message


@pytest.mark.asyncio
async def test_strategy_thinking_mode_overrides_explicit_tier_thinking_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(
        monkeypatch,
        "c2",
        0.92,
        {
            "route_class": "R2",
            "thinking_mode": "T2",
            "prompt_policy": "P1",
        },
    )
    ctx = make_context("Analyze this implementation path.")

    routed = await apply_agentos_router(ctx)

    assert routed.metadata["routed_tier"] == "c2"
    assert routed.metadata["thinking_mode"] == "T2"
    assert routed.metadata["thinking_requested"] is True
    assert routed.metadata["thinking_level"] == "medium"


@pytest.mark.asyncio
async def test_confidence_gate_promotes_low_confidence_t0_to_default_t1_and_reconciles_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(
        monkeypatch,
        "c0",
        0.1,
        {
            "route_class": "R0",
            "thinking_mode": "T0",
            "prompt_policy": "P0",
        },
    )
    ctx = make_context("Maybe simple, but classifier is uncertain.")

    routed = await apply_agentos_router(ctx)
    extra = routed.metadata["routing_extra"]

    assert routed.metadata["routed_tier"] == "c1"
    assert routed.model == "minimax/minimax-m3"
    assert extra["confidence_gate_applied"] is True
    assert extra["base_tier"] == "c0"
    assert extra["final_tier"] == "c1"
    assert routed.metadata["thinking_mode"] == "T1"
    assert routed.metadata["thinking_level"] == "low"
    assert "[RESPONSE_POLICY: Answer directly" in routed.message


@pytest.mark.asyncio
async def test_confidence_gate_falls_back_low_confidence_non_default_text_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(
        monkeypatch,
        "c2",
        0.1,
        {
            "route_class": "R2",
            "thinking_mode": "T2",
            "prompt_policy": "P1",
        },
    )
    ctx = make_context("Classifier is uncertain but picked an expensive tier.")

    routed = await apply_agentos_router(ctx)
    extra = routed.metadata["routing_extra"]

    assert routed.metadata["routed_tier"] == "c1"
    assert routed.model == "minimax/minimax-m3"
    assert extra["confidence_gate_applied"] is True
    assert extra["pre_confidence_tier"] == "c2"
    assert extra["final_tier"] == "c1"


@pytest.mark.asyncio
async def test_large_material_estimate_floors_low_router_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(monkeypatch, "c1", 0.91, {"route_class": "R1"})
    ctx = make_context("Please process the attached pasted text.")
    ctx.metadata["input_normalization"] = {
        "guard_action": "generated_text_attachment",
        "material_estimated_tokens": 45_000,
    }
    ctx.metadata["material_estimated_tokens"] = 45_000

    routed = await apply_agentos_router(ctx)

    assert routed.metadata["routed_tier"] == "c2"
    assert routed.metadata["routing_source"] == "large_context_floor"
    assert routed.metadata["large_context_floor_from_tier"] == "c1"
    assert routed.metadata["large_context_material_tokens"] == 45_000
    assert routed.metadata["routing_extra"]["final_tier"] == "c2"


@pytest.mark.asyncio
async def test_large_material_ratio_floors_low_router_tier_to_t3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(monkeypatch, "c1", 0.91, {"route_class": "R1"})
    ctx = make_context("Please process the attached pasted text.")
    object.__setattr__(ctx.config.agentos_router, "context_window_tokens", 100_000)
    ctx.metadata["input_normalization"] = {
        "guard_action": "generated_text_attachment",
        "material_estimated_tokens": 40_000,
    }

    routed = await apply_agentos_router(ctx)

    assert routed.metadata["routed_tier"] == "c3"
    assert routed.metadata["routing_source"] == "large_context_floor"
    assert routed.metadata["large_context_floor_from_tier"] == "c1"
    assert routed.metadata["large_context_material_tokens"] == 40_000


@pytest.mark.asyncio
async def test_anti_downgrade_keeps_recent_higher_tier_despite_confidence_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx1 = make_context("Hard first turn.", session_key="test-confidence-history")
    fake_strategy(
        monkeypatch,
        "c2",
        0.9,
        {
            "route_class": "R2",
            "thinking_mode": "T2",
            "prompt_policy": "P1",
        },
    )
    routed1 = await apply_agentos_router(ctx1)
    assert routed1.metadata["routed_tier"] == "c2"

    fake_strategy(
        monkeypatch,
        "c0",
        0.1,
        {
            "route_class": "R0",
            "thinking_mode": "T0",
            "prompt_policy": "P0",
        },
    )
    ctx2 = make_context("Uncertain follow-up.", session_key="test-confidence-history")

    routed2 = await apply_agentos_router(ctx2)
    extra = routed2.metadata["routing_extra"]

    assert routed2.metadata["routed_tier"] == "c2"
    assert routed2.model == "z-ai/glm-5.2"
    assert extra["confidence_gate_applied"] is True
    assert extra["pre_confidence_tier"] == "c0"
    assert extra["final_tier"] == "c2"
    assert extra["anti_downgrade_applied"] is True
    assert extra["previous_tier"] == "c2"

    fake_strategy(
        monkeypatch,
        "c1",
        0.9,
        {
            "route_class": "R1",
            "thinking_mode": "T1",
            "prompt_policy": "P1",
        },
    )
    ctx3 = make_context("Normal follow-up.", session_key="test-confidence-history")

    routed3 = await apply_agentos_router(ctx3)
    extra3 = routed3.metadata["routing_extra"]

    assert routed3.metadata["routed_tier"] == "c2"
    assert routed3.model == "z-ai/glm-5.2"
    assert extra3["confidence_gate_applied"] is False
    assert extra3["anti_downgrade_applied"] is True
    assert extra3["previous_tier"] == "c2"


@pytest.mark.asyncio
async def test_anti_downgrade_uses_previous_turn_not_window_highest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "test-previous-not-highest"
    fake_strategy(
        monkeypatch,
        "c3",
        0.9,
        {
            "route_class": "R3",
            "thinking_mode": "T3",
            "prompt_policy": "P2",
        },
    )
    routed1 = await apply_agentos_router(make_context("Very hard turn.", session_key=session_key))
    assert routed1.metadata["routed_tier"] == "c3"

    ctx2 = make_context("Less hard turn.", session_key=session_key)
    ctx2.config.agentos_router.kv_cache_anti_downgrade_enabled = False
    fake_strategy(
        monkeypatch,
        "c2",
        0.9,
        {
            "route_class": "R2",
            "thinking_mode": "T2",
            "prompt_policy": "P1",
        },
    )
    routed2 = await apply_agentos_router(ctx2)
    assert routed2.metadata["routed_tier"] == "c2"

    ctx3 = make_context("Easy follow-up.", session_key=session_key)
    fake_strategy(
        monkeypatch,
        "c1",
        0.9,
        {
            "route_class": "R1",
            "thinking_mode": "T1",
            "prompt_policy": "P1",
        },
    )
    routed3 = await apply_agentos_router(ctx3)
    extra3 = routed3.metadata["routing_extra"]

    assert routed3.metadata["routed_tier"] == "c2"
    assert routed3.model == "z-ai/glm-5.2"
    assert extra3["anti_downgrade_applied"] is True
    assert extra3["previous_tier"] == "c2"


@pytest.mark.asyncio
async def test_anti_downgrade_keeps_previous_high_tier_without_margin_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "test-anti-downgrade-ignore-margin"
    fake_strategy(
        monkeypatch,
        "c3",
        0.95,
        {
            "route_class": "R3",
            "thinking_mode": "T3",
            "prompt_policy": "P2",
            "margin": 0.99,
        },
    )
    routed1 = await apply_agentos_router(
        make_context("Architecture review.", session_key=session_key)
    )
    assert routed1.metadata["routed_tier"] == "c3"

    fake_strategy(
        monkeypatch,
        "c1",
        0.99,
        {
            "route_class": "R1",
            "thinking_mode": "T1",
            "prompt_policy": "P1",
            "margin": 0.99,
        },
    )
    routed2 = await apply_agentos_router(make_context("Follow-up.", session_key=session_key))
    extra = routed2.metadata["routing_extra"]

    assert routed2.metadata["routed_tier"] == "c3"
    assert routed2.model == "anthropic/claude-opus-4.8"
    assert extra["anti_downgrade_applied"] is True
    assert extra["previous_tier"] == "c3"
    assert extra["kv_cache_window_seconds"] == 600


@pytest.mark.asyncio
async def test_complaint_upgrade_promotes_tier_thinking_and_blocks_compressed_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(
        monkeypatch,
        "c1",
        0.9,
        {
            "route_class": "R1",
            "thinking_mode": "T1",
            "prompt_policy": "P0",
        },
    )
    ctx = make_context("不对，重新回答")

    routed = await apply_agentos_router(ctx)
    extra = routed.metadata["routing_extra"]

    assert routed.metadata["routed_tier"] == "c2"
    assert routed.model == "z-ai/glm-5.2"
    assert extra["complaint_detected"] is True
    assert extra["complaint_upgrade_applied"] is True
    assert routed.metadata["thinking_mode"] == "T2"
    assert routed.metadata["thinking_level"] == "medium"
    assert routed.metadata["prompt_policy"] == "P1"
    assert "[RESPONSE_POLICY:" not in routed.message


@pytest.mark.asyncio
async def test_complaint_upgrade_starts_from_previous_experienced_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "test-complaint-upgrade-previous-tier"
    fake_strategy(
        monkeypatch,
        "c2",
        0.9,
        {
            "route_class": "R2",
            "thinking_mode": "T2",
            "prompt_policy": "P1",
        },
    )
    routed1 = await apply_agentos_router(
        make_context("Analyze this tricky failure.", session_key=session_key)
    )
    assert routed1.metadata["routed_tier"] == "c2"

    fake_strategy(
        monkeypatch,
        "c1",
        0.9,
        {
            "route_class": "R1",
            "thinking_mode": "T1",
            "prompt_policy": "P0",
        },
    )
    routed2 = await apply_agentos_router(make_context("答非所问", session_key=session_key))
    extra = routed2.metadata["routing_extra"]

    assert routed2.metadata["routed_tier"] == "c3"
    assert routed2.model == "anthropic/claude-opus-4.8"
    assert extra["previous_tier"] == "c2"
    assert extra["complaint_detected"] is True
    assert extra["complaint_upgrade_applied"] is True
    assert extra["anti_downgrade_applied"] is False


@pytest.mark.asyncio
async def test_router_classifies_raw_semantic_input_but_injects_prompt_into_display_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = fake_strategy(
        monkeypatch,
        "c0",
        0.92,
        {
            "route_class": "R0",
            "thinking_mode": "T0",
            "prompt_policy": "P0",
        },
    )
    ctx = make_context(
        "Displayed prompt wrapper",
        raw_message="Summarize the underlying user input.",
    )

    routed = await apply_agentos_router(ctx)

    assert strategy.messages == ["Summarize the underlying user input."]
    assert routed.metadata["routed_tier"] == "c0"
    assert routed.metadata["prompt_policy"] == "P0"
    assert routed.message.startswith("Displayed prompt wrapper")
    assert "Summarize the underlying user input." not in routed.message
    assert "[RESPONSE_POLICY: Answer directly" in routed.message


@pytest.mark.asyncio
async def test_router_passes_transcript_context_into_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = context_aware_fake_strategy(
        monkeypatch,
        "c2",
        0.88,
        {
            "route_class": "R2",
            "thinking_mode": "T2",
            "prompt_policy": "P1",
        },
    )
    ctx = make_context("Continue from the previous answer.")
    ctx.metadata.update(
        {
            "router_prev_assistant_text": "Previous assistant answer.",
            "router_prev_assistant_usage": {"output_tokens": 321},
            "router_history_user_texts": ["First user question.", "Second user question."],
            "router_flags_text_override": "Continue from the previous answer.",
            "routing_history": [
                {
                    "text": "First user question.",
                    "route_class": "R1",
                    "final_route_class": "R1",
                    "difficulty": 1.0,
                    "margin": 0.5,
                }
            ],
        }
    )

    routed = await apply_agentos_router(ctx)

    assert routed.metadata["routed_tier"] == "c2"
    assert strategy.messages == ["Continue from the previous answer."]
    assert strategy.contexts == [
        {
            "routing_history": [
                {
                    "text": "First user question.",
                    "route_class": "R1",
                    "final_route_class": "R1",
                    "difficulty": 1.0,
                    "margin": 0.5,
                    "_ts": pytest.approx(strategy.contexts[0]["routing_history"][0]["_ts"]),
                }
            ],
            "prev_assistant_text": "Previous assistant answer.",
            "prev_assistant_usage": {"output_tokens": 321},
            "history_user_texts": ["First user question.", "Second user question."],
            "flags_text_override": "Continue from the previous answer.",
        }
    ]


@pytest.mark.asyncio
async def test_image_input_routes_directly_to_vision_model_without_prompt_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agentos_router_step,
        "_get_strategy",
        lambda _config, _llm_cfg=None: pytest.fail(
            "image routing should not invoke text strategy"
        ),
    )
    ctx = make_context(
        "What is in this screenshot?",
        attachments=[{"type": "image", "mime_type": "image/png"}],
    )

    routed = await apply_agentos_router(ctx)

    assert routed.model == "minimax/minimax-m3"
    assert routed.metadata["routed_tier"] == "image_model"
    assert routed.metadata["routed_model"] == "minimax/minimax-m3"
    assert routed.metadata["routing_applied"] is True
    assert routed.metadata["routing_confidence"] == 1.0
    assert routed.metadata["routing_source"] == "image_route"
    assert routed.metadata["route_max_history_turns"] == 1
    assert routed.metadata["thinking_requested"] is True
    assert routed.metadata["thinking_level"] == "medium"
    assert "[RESPONSE_POLICY:" not in routed.message


@pytest.mark.asyncio
async def test_image_attachment_without_image_tier_fails_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agentos_router_step,
        "_get_strategy",
        lambda _config, _llm_cfg=None: pytest.fail(
            "image routing without image tier should not invoke text strategy"
        ),
    )
    ctx = make_context(
        "What is in this screenshot?",
        attachments=[{"type": "image", "mime_type": "image/png"}],
    )
    ctx.config.agentos_router.tiers["image_model"]["supports_image"] = False

    with pytest.raises(
        RuntimeError,
        match="No image-capable AgentOS Router tier is configured",
    ):
        await apply_agentos_router(ctx)


@pytest.mark.asyncio
async def test_non_image_attachment_does_not_force_vision_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = fake_strategy(
        monkeypatch,
        "c1",
        0.91,
        {
            "route_class": "R1",
            "thinking_mode": "T1",
            "prompt_policy": "P0",
        },
    )
    ctx = make_context(
        "Summarize the attached PDF text.",
        attachments=[{"type": "application/pdf", "mime_type": "application/pdf"}],
    )

    routed = await apply_agentos_router(ctx)

    assert strategy.calls == 1
    assert routed.metadata["routing_source"] == "llm_judge"
    assert routed.metadata["routed_tier"] == "c1"


@pytest.mark.asyncio
async def test_observe_rollout_records_decisions_without_applying_model_or_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(
        monkeypatch,
        "c2",
        0.93,
        {
            "route_class": "R2",
            "thinking_mode": "T2",
            "prompt_policy": "P2",
            "prompt_hint": "Use extra care.",
        },
    )
    ctx = make_context("Analyze this code path.", rollout_phase="observe")
    baseline_model = ctx.model

    routed = await apply_agentos_router(ctx)

    assert routed.model == baseline_model
    assert routed.metadata["routed_tier"] == "c2"
    assert routed.metadata["routed_model"] == "z-ai/glm-5.2"
    assert routed.metadata["routing_applied"] is False
    assert routed.metadata["thinking_mode"] == "T2"
    assert routed.metadata["thinking_level"] == "medium"
    assert "[RESPONSE_POLICY:" not in routed.message


@pytest.mark.asyncio
async def test_repeated_message_across_sessions_is_classified_each_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = fake_strategy(
        monkeypatch,
        "c1",
        0.91,
        {
            "route_class": "R1",
            "thinking_mode": "T1",
            "prompt_policy": "P0",
        },
    )

    first = await apply_agentos_router(make_context("Repeat this.", session_key="session-a"))
    second = await apply_agentos_router(make_context("Repeat this.", session_key="session-b"))

    assert first.metadata["routing_source"] == "llm_judge"
    assert second.metadata["routing_source"] == "llm_judge"
    assert strategy.calls == 2


@pytest.mark.asyncio
async def test_judge_strategy_build_failure_falls_back_to_default_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentos.agentos_router.llm_judge as llm_judge

    monkeypatch.setattr(llm_judge, "LLMJudgeStrategy", ExplodingJudgeStrategy)
    ctx = make_context("Explain the setup steps.")
    # The default strategy is now v4_phase3; select llm_judge explicitly so the
    # judge build-failure path (ExplodingJudgeStrategy) is the one exercised.
    ctx.config.agentos_router.strategy = "llm_judge"

    routed = await apply_agentos_router(ctx)

    assert routed.metadata["routing_source"] == "judge_unavailable"
    assert routed.metadata["routed_tier"] == "c1"
    assert routed.metadata["routing_confidence"] == 0.0


@pytest.mark.asyncio
async def test_v4_phase3_without_bundle_degrades_to_default_tier() -> None:
    """v4_phase3 is the reintegrated default strategy, but its ML bundle is
    git-ignored and absent in CI/public checkouts. When the bundle is missing,
    V4Phase3Strategy stays unavailable and classify degrades to the default tier
    (c1) with routing_source="v4_unavailable" and confidence 0.0 — it does NOT
    raise (require_router_runtime defaults False). Tested directly against a
    nonexistent bundle path for determinism regardless of the local machine."""
    from agentos.agentos_router.v4_phase3 import V4Phase3Strategy

    strategy = V4Phase3Strategy(bundle_dir="/nonexistent/path")

    assert strategy._available is False

    tier, confidence, source, _extra = await strategy.classify(
        "Explain the setup steps.", ["c0", "c1", "c2", "c3"]
    )

    assert tier == "c1"
    assert confidence == 0.0
    assert source == "v4_unavailable"


def _real_judge_strategy(monkeypatch: pytest.MonkeyPatch, route_class: str) -> None:
    """Install the REAL LLMJudgeStrategy with only _judge mocked, so route-class
    -> thinking_mode/prompt_policy derivation runs end-to-end via _build_extra
    (finding #8: prove a real difficulty signal yields the specific policy that
    the injector then applies, instead of hardcoding prompt_policy in a fake)."""
    import agentos.agentos_router.llm_judge as llm_judge
    from agentos.engine.steps import agentos_router as router_step

    async def _scripted_judge(self, message, routing_history, flags, tool_defs):
        return llm_judge._JudgeVerdict(
            route_class=route_class, confidence=0.9, reason="scripted"
        )

    monkeypatch.setattr(llm_judge.LLMJudgeStrategy, "_judge", _scripted_judge)
    # Disable the greeting short-circuit so the judge path (and derivation) runs
    # even for short trivial prompts.
    monkeypatch.setattr(
        llm_judge.LLMJudgeStrategy, "_short_circuit", lambda self, *a, **k: None
    )
    router_step._strategy = None
    router_step._strategy_key = None


@pytest.mark.asyncio
async def test_trivial_route_class_derives_p0_and_injects_hint_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R0 with no risk flags -> T0/P0 through the real derivation, and the
    router injects the localized P0 hint into the display message."""
    _real_judge_strategy(monkeypatch, "R0")
    ctx = make_context("thanks, that works")
    # Default strategy is now v4_phase3; select the judge explicitly so the real
    # LLMJudgeStrategy derivation (mocked _judge) is the path under test.
    ctx.config.agentos_router.strategy = "llm_judge"

    routed = await apply_agentos_router(ctx)

    assert routed.metadata["routing_source"] == "llm_judge"
    assert routed.metadata["routing_extra"]["route_class"] == "R0"
    # Derived, not hardcoded: R0 -> T0/P0.
    assert routed.metadata["thinking_mode"] == "T0"
    assert routed.metadata["prompt_policy"] == "P0"
    assert "[RESPONSE_POLICY: Answer directly" in routed.message


@pytest.mark.asyncio
async def test_trivial_chinese_route_class_injects_localized_p0_hint_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Chinese R0 turn -> P0 derived, and the injected hint is localized to
    Chinese (restores the deleted localized-P0 coverage)."""
    _real_judge_strategy(monkeypatch, "R0")
    ctx = make_context("谢谢，这个可以了")
    # Default strategy is now v4_phase3; select the judge explicitly so the real
    # LLMJudgeStrategy localized-hint derivation is the path under test.
    ctx.config.agentos_router.strategy = "llm_judge"

    routed = await apply_agentos_router(ctx)

    assert routed.metadata["prompt_policy"] == "P0"
    assert "[RESPONSE_POLICY: 直接作答" in routed.message


@pytest.mark.asyncio
async def test_complex_route_class_derives_t3_p2_without_p0_injection_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 -> T3 (deep thinking) and P2 (full prompt) through the real
    derivation; P2 is recorded but never injected as a RESPONSE_POLICY hint."""
    _real_judge_strategy(monkeypatch, "R3")
    ctx = make_context(
        "Diagnose this intermittent production data-corruption bug across "
        "services and plan a safe rollback."
    )
    # Default strategy is now v4_phase3; select the judge explicitly so the real
    # LLMJudgeStrategy derivation (mocked _judge) is the path under test.
    ctx.config.agentos_router.strategy = "llm_judge"

    routed = await apply_agentos_router(ctx)

    assert routed.metadata["routing_source"] == "llm_judge"
    assert routed.metadata["routing_extra"]["route_class"] == "R3"
    assert routed.metadata["routed_tier"] == "c3"
    assert routed.metadata["thinking_mode"] == "T3"
    assert routed.metadata["prompt_policy"] == "P2"
    assert routed.metadata["thinking_level"] == "high"
    assert "[RESPONSE_POLICY:" not in routed.message
