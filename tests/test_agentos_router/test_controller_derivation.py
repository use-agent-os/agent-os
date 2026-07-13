"""Direct unit tests for the D6 thinking-mode / prompt-policy derivation.

``derive_thinking_mode``, ``derive_prompt_policy``, ``normalize_decisions``,
and ``synthetic_one_hot`` are the entire D6 surface: every judge classification
routes its ``thinking_mode``/``prompt_policy`` through them via
``LLMJudgeStrategy._build_extra``. The ML postprocess tests that used to pin
this behavior were deleted with the ML bundle (finding #6); these re-create the
load-bearing assertions against the kept controller functions so a regression
that makes a risky turn stop forcing P2/T3 — or breaks the T2/T3+P0
contradiction guard — fails CI instead of shipping green.
"""

from __future__ import annotations

import pytest

from agentos.agentos_router.controller import (
    derive_prompt_policy,
    derive_thinking_mode,
    normalize_decisions,
    synthetic_one_hot,
)
from agentos.router_tiers import TEXT_TIERS


def _one_hot(route_class: str) -> list[float]:
    """Map an R0-R3 route class to the peaked probability vector the judge
    feeds the controller (via ROUTE_CLASS_TO_TIER + synthetic_one_hot)."""
    tier = f"c{route_class[1:]}"
    return synthetic_one_hot(tier)


# -- synthetic_one_hot ------------------------------------------------------


def test_synthetic_one_hot_peaks_on_requested_tier() -> None:
    for idx, tier in enumerate(TEXT_TIERS):
        probs = synthetic_one_hot(tier)
        assert len(probs) == len(TEXT_TIERS)
        assert max(range(len(probs)), key=lambda i: probs[i]) == idx
        assert probs[idx] == pytest.approx(0.85)


def test_synthetic_one_hot_unknown_tier_defaults_to_index_one() -> None:
    probs = synthetic_one_hot("nonexistent")
    assert max(range(len(probs)), key=lambda i: probs[i]) == 1


# -- derive_thinking_mode ---------------------------------------------------


def test_thinking_mode_top_class_forces_t3() -> None:
    # R3 peaks on the last tier -> T3 regardless of flags.
    assert derive_thinking_mode(_one_hot("R3")) == "T3"


def test_thinking_mode_trivial_r0_is_t0() -> None:
    assert derive_thinking_mode(_one_hot("R0")) == "T0"


def test_thinking_mode_high_risk_forces_t3_on_mid_class() -> None:
    """High-risk (a _DEEP_FLAG) on a class >= t3_min_idx must escalate to T3 —
    the behavior the deleted ML test asserted for risky low-probability routes.
    """
    # R2 peaks on index 2 (== t3_min_idx); high_risk flag escalates to T3.
    assert derive_thinking_mode(_one_hot("R2"), {"high_risk": True}) == "T3"
    # debug is also a _DEEP_FLAG.
    assert derive_thinking_mode(_one_hot("R2"), {"debug": True}) == "T3"


def test_thinking_mode_deep_flag_without_high_class_does_not_force_t3() -> None:
    # R0 is below t3_min_idx, so a deep flag alone does not force T3.
    assert derive_thinking_mode(_one_hot("R0"), {"high_risk": True}) == "T0"


# -- derive_prompt_policy ---------------------------------------------------


def test_prompt_policy_full_prompt_flag_forces_p2() -> None:
    """Any _FULL_PROMPT_FLAG (high_risk/long_context/debug/strict_format) must
    force P2 — the anti-under-prompting guard for risky turns."""
    for flag in ("high_risk", "long_context", "debug", "strict_format"):
        assert derive_prompt_policy(_one_hot("R0"), {flag: True}) == "P2", flag


def test_prompt_policy_trivial_r0_is_p0() -> None:
    # Trivial, high-margin, no risk flags -> compressible P0.
    assert derive_prompt_policy(_one_hot("R0")) == "P0"


def test_prompt_policy_hard_class_without_flags_is_p1() -> None:
    # R3 is above max_difficulty so it is not P0, and no full-prompt flag -> P1.
    assert derive_prompt_policy(_one_hot("R3")) == "P1"


# -- normalize_decisions (T2/T3 + P0 contradiction guard) -------------------


@pytest.mark.parametrize("mode", ["T2", "T3"])
def test_normalize_forbids_deep_thinking_with_p0(mode: str) -> None:
    thinking, policy = normalize_decisions(mode, "P0")
    assert thinking == mode
    assert policy == "P1", "deep thinking must not pair with compressed P0"


@pytest.mark.parametrize("mode", ["T0", "T1"])
def test_normalize_keeps_p0_for_shallow_thinking(mode: str) -> None:
    assert normalize_decisions(mode, "P0") == (mode, "P0")


def test_normalize_leaves_non_p0_untouched() -> None:
    assert normalize_decisions("T3", "P2") == ("T3", "P2")
    assert normalize_decisions("T2", "P1") == ("T2", "P1")


# -- end-to-end derivation per route class ----------------------------------


def test_trivial_ack_r0_derives_t0_p0() -> None:
    """R0 with no flags -> T0/P0 (trivial turns answer directly, short
    thinking) — the second behavior the deleted ML test pinned."""
    probs = _one_hot("R0")
    thinking = derive_thinking_mode(probs)
    policy = derive_prompt_policy(probs)
    thinking, policy = normalize_decisions(thinking, policy)
    assert (thinking, policy) == ("T0", "P0")


def test_high_risk_r2_derives_t3_p2_after_normalize() -> None:
    """A risky R2 turn -> T3 (deep) + P2 (full prompt); the contradiction
    guard leaves P2 intact (only P0 is downgraded)."""
    probs = _one_hot("R2")
    flags = {"high_risk": True}
    thinking = derive_thinking_mode(probs, flags)
    policy = derive_prompt_policy(probs, flags)
    thinking, policy = normalize_decisions(thinking, policy)
    assert (thinking, policy) == ("T3", "P2")
