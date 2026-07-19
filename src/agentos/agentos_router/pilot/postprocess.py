"""Pilot router post-processing — under-routing safety net (T3).

The binding contract (Pilot router spec, Rev 4, §4.1/§4.2):

* **Escalation mass**: ``m = P(R2) + P(R3)`` — the calibrated probability
  mass on the two "needs a stronger model" classes, combined.
* **Effective threshold**: ``t_eff = max(safety_net_threshold,
  confidence_threshold)``. The two thresholds are coupled by construction:
  an uncoupled ``pilot.safety_net_threshold`` of e.g. 0.45 sitting below the
  engine's ``router.confidence_threshold`` of 0.5 would let the confidence
  gate immediately undo a fired bump (the bumped route would report a
  confidence the gate then rejects). Taking the ``max`` guarantees a fired
  bump's confidence always clears the gate.
* **Rule**: if the calibrated argmax class is ``R0`` or ``R1`` (i.e.
  strictly before ``R2`` in the pinned class order) *and* ``m > t_eff``,
  the safety net fires and the reported route is bumped up to ``R2``.
  Otherwise the argmax class is reported unchanged. Note the comparison is
  strict (``>``, not ``>=``): a mass exactly equal to ``t_eff`` does not
  fire.
* **Confidence contract** (exactly two cases — ``m`` is *not* a class
  probability, so it is only ever surfaced as the confidence value in the
  fired case):

  - safety net **not** fired → ``confidence = calibrated P(top-1 class)``
    (the ordinary argmax probability).
  - safety net **fired** → ``confidence = m`` (the escalation mass), which
    by construction exceeds ``t_eff >= confidence_threshold`` — so the
    engine's confidence gate can never immediately undo a fired bump.

This is the *only* probability-space adjustment Pilot owns. No sticky
tier, no history logic, no complaint-driven upgrade — those live in the
engine. This module is a pure function: no I/O, no config loading, no
imports from gateway config. The strategy layer (a later task) is
responsible for reading ``pilot.safety_net_threshold`` /
``router.confidence_threshold`` from live config and passing the resolved
floats in here.

Reference defaults (documented here, not read from config): the engine's
``router.confidence_threshold`` defaults to ``0.5``
(``agentos.gateway.config``); ``pilot.safety_net_threshold`` defaults to
``0.5`` as well.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from agentos.agentos_router.pilot.model import PILOT_CLASSES

#: Index of the "R2" class in the pinned order — the safety net's bump
#: target and the argmax cutoff (argmax < this index is eligible to fire).
_R2_INDEX = PILOT_CLASSES.index("R2")


@dataclass(frozen=True, slots=True)
class PostprocessResult:
    """Result of applying the under-routing safety net to one prediction.

    ``route_class`` is a label from :data:`PILOT_CLASSES`. ``confidence``
    follows the two-case contract documented on :func:`apply_safety_net`.
    ``safety_net_applied`` is ``True`` iff the safety net fired (the
    strategy layer's ``extra`` dict surfaces this flag downstream).
    """

    route_class: str
    confidence: float
    safety_net_applied: bool


def apply_safety_net(
    probs: Sequence[float],
    *,
    safety_net_threshold: float,
    confidence_threshold: float,
) -> PostprocessResult:
    """Apply the Pilot under-routing safety net to calibrated probabilities.

    ``probs`` holds calibrated per-class probabilities in the pinned order
    ``["R0", "R1", "R2", "R3"]`` (see ``PILOT_CLASSES``). The values are
    used exactly as given — this function does not renormalize, so
    probabilities that don't sum to 1 are passed through as-is (the mass
    ``m`` and the reported confidence reflect whatever was passed in).

    On a tie for the argmax, the first (lowest-index) class wins, matching
    ``numpy.argmax`` / Python ``max`` first-wins semantics.

    See the module docstring for the full rule and confidence contract.
    """
    values = list(probs)
    argmax_index = max(range(len(values)), key=lambda i: values[i])
    m = values[_R2_INDEX] + values[_R2_INDEX + 1]
    t_eff = max(safety_net_threshold, confidence_threshold)

    if argmax_index < _R2_INDEX and m > t_eff:
        return PostprocessResult(
            route_class=PILOT_CLASSES[_R2_INDEX],
            confidence=m,
            safety_net_applied=True,
        )

    return PostprocessResult(
        route_class=PILOT_CLASSES[argmax_index],
        confidence=values[argmax_index],
        safety_net_applied=False,
    )
