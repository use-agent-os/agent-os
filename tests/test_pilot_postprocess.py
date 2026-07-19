"""Pilot router post-processing tests — under-routing safety net + confidence
contract (T3, Pilot router spec Rev 4 §4.1/§4.2).

Table tests covering: safety-net firing/non-firing, the strict-`>` boundary,
threshold coupling (`t_eff = max(safety_net_threshold, confidence_threshold)`),
the two-case confidence contract, and pinned tie/degenerate-input behavior.
"""

from __future__ import annotations

import pytest

from agentos.agentos_router.pilot.model import PILOT_CLASSES
from agentos.agentos_router.pilot.postprocess import PostprocessResult, apply_safety_net

# --- Fires: argmax R0/R1, m > t_eff -----------------------------------------


@pytest.mark.parametrize(
    "probs, safety_net_threshold, confidence_threshold",
    [
        # argmax R0, escalation mass clearly above t_eff.
        ((0.35, 0.1, 0.35, 0.2), 0.5, 0.5),
        # argmax R1, escalation mass clearly above t_eff.
        ((0.1, 0.35, 0.35, 0.2), 0.5, 0.5),
        # argmax R0, mass just barely above t_eff.
        ((0.49, 0.0, 0.49, 0.02), 0.5, 0.5),
    ],
)
def test_safety_net_fires(probs, safety_net_threshold, confidence_threshold):
    result = apply_safety_net(
        probs,
        safety_net_threshold=safety_net_threshold,
        confidence_threshold=confidence_threshold,
    )
    m = probs[2] + probs[3]
    assert result.route_class == "R2"
    assert result.safety_net_applied is True
    assert result.confidence == pytest.approx(m)


# --- Does not fire: argmax R2/R3 regardless of m ----------------------------


@pytest.mark.parametrize(
    "probs, safety_net_threshold, confidence_threshold",
    [
        ((0.1, 0.1, 0.7, 0.1), 0.5, 0.5),  # argmax R2, m=0.8 > t_eff, but no fire
        ((0.1, 0.1, 0.1, 0.7), 0.5, 0.5),  # argmax R3, m=0.8 > t_eff, but no fire
        ((0.0, 0.0, 0.5, 0.5), 0.1, 0.1),  # argmax tie R2/R3, m=1.0
    ],
)
def test_safety_net_does_not_fire_when_argmax_already_r2_or_r3(
    probs, safety_net_threshold, confidence_threshold
):
    result = apply_safety_net(
        probs,
        safety_net_threshold=safety_net_threshold,
        confidence_threshold=confidence_threshold,
    )
    expected_class = PILOT_CLASSES[probs.index(max(probs))]
    assert result.route_class == expected_class
    assert result.safety_net_applied is False
    assert result.confidence == pytest.approx(max(probs))


# --- Does not fire: argmax R0/R1 but m <= t_eff (strict >) ------------------


@pytest.mark.parametrize(
    "probs, safety_net_threshold, confidence_threshold",
    [
        # m == t_eff exactly -> strict > means no fire.
        ((0.5, 0.0, 0.3, 0.2), 0.5, 0.5),
        # m < t_eff -> no fire.
        ((0.6, 0.0, 0.2, 0.1), 0.5, 0.5),
    ],
)
def test_safety_net_does_not_fire_at_or_below_threshold(
    probs, safety_net_threshold, confidence_threshold
):
    result = apply_safety_net(
        probs,
        safety_net_threshold=safety_net_threshold,
        confidence_threshold=confidence_threshold,
    )
    expected_class = PILOT_CLASSES[probs.index(max(probs))]
    assert result.route_class == expected_class
    assert result.safety_net_applied is False
    assert result.confidence == pytest.approx(max(probs))


# --- t_eff coupling ----------------------------------------------------------


def test_t_eff_uses_confidence_threshold_when_higher():
    """safety_net_threshold below confidence_threshold: t_eff must use the
    higher confidence_threshold, so a bump that fires always satisfies the
    engine's confidence gate."""
    probs = (0.4, 0.0, 0.35, 0.2)  # m = 0.55
    # With safety_net_threshold=0.3 alone this would fire (0.55 > 0.3), but
    # t_eff must couple up to confidence_threshold=0.6, so it must NOT fire.
    result = apply_safety_net(probs, safety_net_threshold=0.3, confidence_threshold=0.6)
    assert result.safety_net_applied is False
    assert result.route_class == "R0"


def test_t_eff_uses_safety_net_threshold_when_higher():
    """Reverse ordering: confidence_threshold below safety_net_threshold,
    t_eff must use the higher safety_net_threshold."""
    probs = (0.4, 0.0, 0.35, 0.2)  # m = 0.55
    # confidence_threshold alone (0.3) would allow firing, but t_eff couples
    # up to safety_net_threshold=0.6, so m=0.55 must NOT clear it.
    result = apply_safety_net(probs, safety_net_threshold=0.6, confidence_threshold=0.3)
    assert result.safety_net_applied is False
    assert result.route_class == "R0"


def test_t_eff_coupling_fires_when_mass_exceeds_the_max_of_both():
    """Sanity check for the coupling tests above: raising m past both
    thresholds fires regardless of which one is larger."""
    probs = (0.4, 0.0, 0.35, 0.35)  # argmax R0 (0.4); m = 0.7
    result = apply_safety_net(probs, safety_net_threshold=0.3, confidence_threshold=0.6)
    assert result.safety_net_applied is True
    assert result.route_class == "R2"
    assert result.confidence == pytest.approx(0.7)


# --- Fired confidence exceeds confidence_threshold by construction ----------


@pytest.mark.parametrize(
    "probs, safety_net_threshold, confidence_threshold",
    [
        ((0.35, 0.1, 0.35, 0.2), 0.5, 0.5),
        ((0.1, 0.35, 0.35, 0.2), 0.5, 0.5),
        ((0.4, 0.0, 0.35, 0.2), 0.3, 0.3),
    ],
)
def test_fired_confidence_exceeds_confidence_threshold(
    probs, safety_net_threshold, confidence_threshold
):
    result = apply_safety_net(
        probs,
        safety_net_threshold=safety_net_threshold,
        confidence_threshold=confidence_threshold,
    )
    assert result.safety_net_applied is True
    assert result.confidence > confidence_threshold


# --- Boundary / degenerate inputs --------------------------------------------


def test_argmax_tie_first_wins():
    """Ties in argmax: pin numpy's first-wins behavior (lowest index wins)."""
    probs = (0.25, 0.25, 0.25, 0.25)
    result = apply_safety_net(probs, safety_net_threshold=0.5, confidence_threshold=0.5)
    # argmax picks index 0 (R0) on a tie; m = 0.5 == t_eff, strict > fails.
    assert result.route_class == "R0"
    assert result.safety_net_applied is False
    assert result.confidence == pytest.approx(0.25)


def test_argmax_tie_between_r0_and_r1_with_fire():
    """Tie between R0 and R1 (both < R2): first-wins argmax is R0, and if
    m > t_eff the safety net still fires."""
    probs = (0.3, 0.3, 0.3, 0.1)  # m = 0.4
    result = apply_safety_net(probs, safety_net_threshold=0.3, confidence_threshold=0.3)
    assert result.safety_net_applied is True
    assert result.route_class == "R2"
    assert result.confidence == pytest.approx(0.4)


def test_probs_not_summing_to_one_are_not_renormalized():
    """The function does not renormalize; m is computed directly from the
    raw (possibly non-normalized) inputs passed in."""
    probs = (0.2, 0.1, 0.9, 0.9)  # sums to 2.1, m = 1.8
    result = apply_safety_net(probs, safety_net_threshold=0.5, confidence_threshold=0.5)
    # argmax is already R2/R3 (tie -> first-wins R2), so no fire regardless.
    assert result.route_class == "R2"
    assert result.safety_net_applied is False
    assert result.confidence == pytest.approx(0.9)


def test_probs_not_summing_to_one_fires_with_raw_mass():
    """Non-normalized probs where argmax is R0/R1: m is the raw (unnormalized)
    sum of P(R2)+P(R3), not rescaled to a proper probability."""
    probs = (0.9, 0.1, 0.5, 0.5)  # sums to 2.0, m = 1.0, argmax R0
    result = apply_safety_net(probs, safety_net_threshold=0.5, confidence_threshold=0.5)
    assert result.route_class == "R2"
    assert result.safety_net_applied is True
    assert result.confidence == pytest.approx(1.0)


# --- Result shape -------------------------------------------------------------


def test_result_is_immutable_and_carries_required_fields():
    result = apply_safety_net(
        (0.7, 0.1, 0.1, 0.1), safety_net_threshold=0.5, confidence_threshold=0.5
    )
    assert isinstance(result, PostprocessResult)
    assert result.route_class == "R0"
    assert result.confidence == pytest.approx(0.7)
    assert result.safety_net_applied is False
    with pytest.raises(AttributeError):
        result.route_class = "R1"  # type: ignore[misc]


def test_route_class_is_from_pinned_order():
    result = apply_safety_net(
        (0.1, 0.1, 0.1, 0.7), safety_net_threshold=0.5, confidence_threshold=0.5
    )
    assert result.route_class in PILOT_CLASSES
