"""Unit tests for ``StageOutcome`` invariants.

Exercises ``StageOutcome.success(...)``, ``StageOutcome.terminate_with(...)``,
and each invalid construction the ``__post_init__`` rejects.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from agentos.engine.turn_runner.outcome import StageOutcome
from agentos.engine.types import ErrorEvent


@dataclass(frozen=True)
class _DummyOutput:
    value: int


def test_success_constructs_with_output() -> None:
    out = _DummyOutput(value=7)
    oc = StageOutcome.success(out)
    assert oc.terminate is False
    assert oc.output is out
    assert oc.early_yield is None


def test_terminate_with_constructs_with_event() -> None:
    ev = ErrorEvent(message="No provider available", code="no_provider")
    oc = StageOutcome.terminate_with(ev)
    assert oc.terminate is True
    assert oc.early_yield is ev
    assert oc.output is None


def test_terminate_true_requires_early_yield() -> None:
    with pytest.raises(ValueError, match="requires early_yield"):
        StageOutcome(output=None, early_yield=None, terminate=True)


def test_terminate_true_forbids_output() -> None:
    out = _DummyOutput(value=1)
    ev = ErrorEvent(message="x", code="y")
    with pytest.raises(ValueError, match="must have output=None"):
        StageOutcome(output=out, early_yield=ev, terminate=True)


def test_terminate_false_forbids_early_yield() -> None:
    ev = ErrorEvent(message="x", code="y")
    out = _DummyOutput(value=1)
    with pytest.raises(ValueError, match="must have early_yield=None"):
        StageOutcome(output=out, early_yield=ev, terminate=False)


def test_terminate_false_requires_output() -> None:
    with pytest.raises(ValueError, match="requires output"):
        StageOutcome(output=None, early_yield=None, terminate=False)


def test_outcome_is_frozen() -> None:
    out = _DummyOutput(value=2)
    oc = StageOutcome.success(out)
    with pytest.raises(Exception):  # FrozenInstanceError, but message varies
        oc.output = _DummyOutput(value=3)  # type: ignore[misc]
