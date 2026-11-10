from __future__ import annotations

from agentos.engine.outcome import outcome_from_error


def test_max_iterations_is_partial_not_failed() -> None:
    outcome = outcome_from_error(code="max_iterations", message="hit cap")

    assert outcome.kind == "partial"
    assert outcome.reason == "max_iterations"
    assert outcome.error_class == "max_iterations"
    assert outcome.error_message == "hit cap"


def test_provider_request_budget_is_budget_limited_and_retryable() -> None:
    outcome = outcome_from_error(code="provider_request_too_large")

    assert outcome.kind == "budgetLimited"
    assert outcome.retryable is True


def test_unknown_error_remains_failed() -> None:
    outcome = outcome_from_error(code="boom")

    assert outcome.kind == "failed"
    assert outcome.retryable is False


def test_codes_from_the_retired_turn_control_module_are_blocked() -> None:
    # engine/turn_control.py named these as blocked reasons; outcome.py is now
    # the only taxonomy, so they must classify there.
    for code in ("permission_unavailable", "no_progress", "tool_policy_denied"):
        assert outcome_from_error(code=code).kind == "blocked", code


def test_cancellation_is_interrupted_with_reason_cancelled() -> None:
    outcome = outcome_from_error(code="cancelled")

    assert outcome.kind == "interrupted"
    assert outcome.reason == "cancelled"


def test_a_request_hook_veto_is_blocked() -> None:
    outcome = outcome_from_error(code="request_vetoed", message="Daily spend limit reached.")

    assert outcome.kind == "blocked"
    assert outcome.reason == "request_vetoed"


def test_a_spend_budget_refusal_is_budget_limited_not_failed() -> None:
    outcome = outcome_from_error(
        code="budget_exceeded", message="Daily spend limit of $5.00 reached."
    )

    assert outcome.kind == "budgetLimited"
    assert outcome.reason == "budget_exceeded"
    assert outcome.error_message == "Daily spend limit of $5.00 reached."
