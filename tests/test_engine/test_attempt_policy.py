"""Unit tests for the provider attempt policy: one decision per failed attempt."""

from __future__ import annotations

import pytest

from agentos.engine.fallback import ProviderErrorKind
from agentos.engine.policy import (
    AttemptBudget,
    AttemptHistory,
    CompactThenRetry,
    Fail,
    Failover,
    ProviderAttemptPolicy,
    Retry,
)
from agentos.engine.policy.attempt import AUTH_FAILURE_GUIDANCE, TIMEOUT_GUIDANCE, seeded_jitter

RATE = ProviderErrorKind.RATE_LIMIT
OVERLOADED = ProviderErrorKind.OVERLOADED
TRANSPORT = ProviderErrorKind.TRANSPORT_TRANSIENT


def _policy(**budget: object) -> ProviderAttemptPolicy:
    return ProviderAttemptPolicy(budget=AttemptBudget(**budget), jitter=seeded_jitter(7))


def _drain(
    policy: ProviderAttemptPolicy,
    kind: ProviderErrorKind,
    *,
    failover_available: bool = False,
    retry_after_s: float | None = None,
) -> tuple[list[float], object]:
    """Keep failing until the policy stops retrying; return delays and the end."""

    history = AttemptHistory()
    delays: list[float] = []
    for _ in range(200):
        decision = policy.decide(
            kind,
            history,
            message="boom",
            retry_after_s=retry_after_s,
            failover_available=failover_available,
        )
        if not isinstance(decision, Retry):
            return delays, decision
        delays.append(decision.delay_s)
        history.note_retry(decision.delay_s, timeout=False)
    raise AssertionError("policy never stopped retrying")


@pytest.mark.parametrize("kind", [RATE, OVERLOADED, TRANSPORT])
def test_transient_kinds_retry_on_the_same_model(kind: ProviderErrorKind) -> None:
    decision = _policy().decide(kind, AttemptHistory(), message="boom")

    assert isinstance(decision, Retry)
    assert decision.delay_s > 0


def test_default_budget_allows_at_least_six_retries_within_ninety_seconds() -> None:
    delays, end = _drain(_policy(), RATE)

    assert len(delays) >= 6 or sum(delays) > 60
    assert sum(delays) <= 90
    assert isinstance(end, Fail)


def test_configured_budget_bounds_total_waiting() -> None:
    delays, end = _drain(_policy(max_retries=50, budget_seconds=30), RATE)

    assert sum(delays) <= 30
    assert len(delays) < 50
    assert isinstance(end, Fail)
    assert end.reason == "budget_spent"


def test_count_bounds_retries_when_budget_is_disabled() -> None:
    delays, end = _drain(_policy(max_retries=3, budget_seconds=0), TRANSPORT)

    assert len(delays) == 3
    assert isinstance(end, Fail) and end.reason == "retries_spent"


def test_exhausted_retries_fail_over_once_then_fail() -> None:
    policy = _policy(max_retries=1)
    history = AttemptHistory()
    history.note_retry(1.0, timeout=False)

    first = policy.decide(RATE, history, message="boom", failover_available=True)
    history.failed_over = True
    second = policy.decide(RATE, history, message="boom", failover_available=True)

    assert isinstance(first, Failover)
    assert isinstance(second, Fail)


def test_short_retry_after_is_a_minimum_delay() -> None:
    decision = _policy().decide(RATE, AttemptHistory(), message="slow", retry_after_s=5.0)

    assert isinstance(decision, Retry)
    assert decision.delay_s >= 5.0
    assert decision.reason == "retry_after"


def test_retry_after_over_the_cap_fails_over_without_waiting() -> None:
    decision = _policy(retry_after_cap_s=30).decide(
        RATE, AttemptHistory(), message="slow", retry_after_s=120.0, failover_available=True
    )

    assert isinstance(decision, Failover)
    assert decision.reason == "retry_after_over_cap"


def test_retry_after_over_the_cap_without_candidate_fails() -> None:
    decision = _policy(retry_after_cap_s=30).decide(
        RATE, AttemptHistory(), message="slow", retry_after_s=120.0, failover_available=False
    )

    assert isinstance(decision, Fail)


def test_backoff_grows_with_multiplicative_jitter() -> None:
    delays, _ = _drain(_policy(max_retries=4, budget_seconds=0, max_backoff_ms=10**9), RATE)

    for attempt, delay in enumerate(delays):
        nominal = 2**attempt
        assert 0.5 * nominal <= delay <= 1.5 * nominal


def test_timeout_retries_once_then_compacts_then_fails_with_guidance() -> None:
    policy = _policy(max_timeout_retries=1)
    history = AttemptHistory()

    first = policy.decide(TRANSPORT, history, message="timed out", is_timeout=True)
    assert isinstance(first, Retry)
    history.note_retry(first.delay_s, timeout=True)

    second = policy.decide(TRANSPORT, history, message="timed out", is_timeout=True)
    assert isinstance(second, CompactThenRetry)
    history.timeout_compacted = True

    third = policy.decide(
        TRANSPORT, history, message="timed out", is_timeout=True, failover_available=False
    )
    assert isinstance(third, Fail)
    assert TIMEOUT_GUIDANCE in third.message


def test_timeout_context_recovery_can_be_disabled() -> None:
    policy = _policy(max_timeout_retries=0, timeout_context_recovery=False)

    decision = policy.decide(
        TRANSPORT,
        AttemptHistory(),
        message="timed out",
        is_timeout=True,
        failover_available=False,
    )

    assert isinstance(decision, Fail)


def test_auth_failure_fails_fast_and_names_the_fix() -> None:
    decision = _policy().decide(
        ProviderErrorKind.AUTH_FAILURE, AttemptHistory(), message="invalid api key"
    )

    assert isinstance(decision, Fail)
    assert decision.message.startswith("invalid api key")
    assert AUTH_FAILURE_GUIDANCE in decision.message


def test_unknown_failure_is_not_retried() -> None:
    decision = _policy().decide(ProviderErrorKind.UNKNOWN, AttemptHistory(), message="??")

    assert isinstance(decision, Fail)
    assert decision.reason == "not_retryable:unknown"


def test_every_decision_names_its_action() -> None:
    assert {Retry(1.0, "x").action, Failover("x").action} == {"retry", "failover"}
    assert CompactThenRetry("x").action == "compact_then_retry"
    assert Fail("m", "x").action == "fail"
