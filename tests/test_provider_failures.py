from __future__ import annotations

from agentos.provider.failures import (
    ProviderFailureKind,
    ProviderRecoveryAction,
    classify_provider_error,
    decide_recovery_action,
)


def test_provider_request_budget_exhausted_is_context_overflow() -> None:
    assert (
        classify_provider_error(
            provider_name="openrouter",
            status_code=None,
            raw_code="provider_request_budget_exhausted",
            message='{"fallback_reason":"provider_request_budget_exhausted"}',
        )
        is ProviderFailureKind.CONTEXT_OVERFLOW
    )


def test_gemini_input_token_count_message_is_context_overflow() -> None:
    """Gemini's real context-overflow error should be classified as CONTEXT_OVERFLOW."""
    assert (
        classify_provider_error(
            provider_name="gemini",
            status_code=400,
            message=(
                "the input token count (12345) exceeds the maximum "
                "number of tokens allowed (8192)."
            ),

        )
        is ProviderFailureKind.CONTEXT_OVERFLOW
    )


def test_anthropic_prompt_too_long_is_context_overflow() -> None:
    assert (
        classify_provider_error(
            provider_name="anthropic",
            status_code=400,
            raw_code="prompt_too_long",
            message="prompt_too_long: prompt is longer than the maximum allowed length",
        )
        is ProviderFailureKind.CONTEXT_OVERFLOW
    )


def test_gemini_input_token_count_message_is_context_overflow_different_counts() -> None:
    """Same message with different token counts must still match."""
    assert (
        classify_provider_error(
            provider_name="gemini",
            status_code=400,
            message=(
                "the input token count (512) exceeds the maximum "
                "number of tokens allowed (4096)."
            ),

        )
        is ProviderFailureKind.CONTEXT_OVERFLOW
    )


def test_anthropic_exceed_context_limit_is_context_overflow() -> None:
    assert (
        classify_provider_error(
            provider_name="anthropic",
            status_code=400,
            raw_code="invalid_request_error",
            message="input length and max_tokens exceed context limit: 200000 > 199999",
        )
        is ProviderFailureKind.CONTEXT_OVERFLOW
    )


def test_anthropic_request_too_large_is_context_overflow() -> None:
    assert (
        classify_provider_error(
            provider_name="anthropic",
            status_code=413,
            raw_code="request_too_large",
            message="request_too_large: request body is too large",
        )
        is ProviderFailureKind.CONTEXT_OVERFLOW
    )


def test_anthropic_request_size_exceeds_is_context_overflow() -> None:
    assert (
        classify_provider_error(
            provider_name="anthropic",
            status_code=413,
            raw_code="invalid_request_error",
            message="request size exceeds the 131072 byte limit",
        )
        is ProviderFailureKind.CONTEXT_OVERFLOW
    )


# ── INSUFFICIENT_CREDITS regressions ─────────────────────────────────


def test_openai_insufficient_quota_429_is_credits() -> None:
    """OpenAI returns insufficient_quota with HTTP 429.

    Before this fix the 429 status code was caught by the rate-limit check
    first, misclassifying the error as RATE_LIMITED.  RATE_LIMITED is a
    circuit-breaker-tripping kind; INSUFFICIENT_CREDITS is not, so the
    misclassification could park a healthy provider for a billing fault
    that a cooldown can never heal.
    """
    assert (
        classify_provider_error(
            provider_name="openai",
            status_code=429,
            raw_code="insufficient_quota",
            message=(
                "You exceeded your current quota, please check your plan "
                "and billing details."
            ),
        )
        is ProviderFailureKind.INSUFFICIENT_CREDITS
    )


def test_openai_exceeded_quota_message_is_credits() -> None:
    """Quota message without the raw code should still be caught."""
    assert (
        classify_provider_error(
            provider_name="openai",
            status_code=429,
            message="You exceeded your current quota.",
        )
        is ProviderFailureKind.INSUFFICIENT_CREDITS
    )


def test_anthropic_billing_error_402_is_credits() -> None:
    """Anthropic billing_error with HTTP 402 was previously UNKNOWN."""
    assert (
        classify_provider_error(
            provider_name="anthropic",
            status_code=402,
            raw_code="billing_error",
            message="Your credit balance is too low to access the Anthropic API.",
        )
        is ProviderFailureKind.INSUFFICIENT_CREDITS
    )


def test_anthropic_credit_balance_too_low_is_credits() -> None:
    """Anthropic credit balance message without status code should match."""
    assert (
        classify_provider_error(
            provider_name="anthropic",
            status_code=None,
            message="Your credit balance is too low to access the Anthropic API.",
        )
        is ProviderFailureKind.INSUFFICIENT_CREDITS
    )


def test_openrouter_insufficient_quota_is_credits() -> None:
    """Same insufficient_quota pattern routed through OpenRouter."""
    assert (
        classify_provider_error(
            provider_name="openrouter",
            status_code=429,
            raw_code="insufficient_quota",
            message="You exceeded your current quota.",
        )
        is ProviderFailureKind.INSUFFICIENT_CREDITS
    )


def test_deepseek_insufficient_quota_is_credits() -> None:
    """DeepSeek quota exhaustion should not be classified as RATE_LIMITED."""
    assert (
        classify_provider_error(
            provider_name="deepseek",
            status_code=429,
            raw_code="insufficient_quota",
            message="Insufficient quota to complete the request.",
        )
        is ProviderFailureKind.INSUFFICIENT_CREDITS
    )


# --- #1359: an unavailable model has to reach the fallback chain -------------

#: The body Google returns when the configured model is not on the key's
#: allowlist. Reproduced verbatim from the issue.
_GEMINI_404_BODY = (
    "models/gemini-2.5-pro is not found for API version v1beta, "
    "or is not supported for generateContent."
)


def test_gemini_404_unavailable_model_is_model_not_found() -> None:
    """The classification, not just the recovery action.

    ``gemini`` is in the ``openai_compat`` family, whose branch matched only
    ``"no endpoints found"`` and ``"model not found"``. Google's wording matches
    neither and 404 was not a transient status, so the turn fell through to
    ``UNKNOWN``.
    """
    assert (
        classify_provider_error(
            provider_name="gemini",
            status_code=404,
            raw_code="NOT_FOUND",
            message=_GEMINI_404_BODY,
        )
        is ProviderFailureKind.MODEL_NOT_FOUND
    )


def test_gemini_404_unavailable_model_falls_back_instead_of_surfacing() -> None:
    """The consequence the operator actually sees: the next model is tried."""
    kind = classify_provider_error(
        provider_name="gemini",
        status_code=404,
        raw_code="NOT_FOUND",
        message=_GEMINI_404_BODY,
    )

    assert decide_recovery_action(kind) is ProviderRecoveryAction.FALLBACK_PROVIDER


def test_gemini_unavailable_model_is_model_not_found_without_a_status_code() -> None:
    """Some transports hand the body up with no status attached.

    The status code carries the match on its own, so this pins the *text*
    marker: without it the same failure classifies differently depending on how
    far the status survived the transport.
    """
    assert (
        classify_provider_error(
            provider_name="gemini",
            status_code=None,
            message=_GEMINI_404_BODY,
        )
        is ProviderFailureKind.MODEL_NOT_FOUND
    )


def test_openai_model_does_not_exist_is_model_not_found() -> None:
    """OpenAI's own phrasing for the same failure, which also fell through."""
    assert (
        classify_provider_error(
            provider_name="openai",
            status_code=404,
            raw_code="model_not_found",
            message="The model `gpt-5` does not exist or you do not have access to it.",
        )
        is ProviderFailureKind.MODEL_NOT_FOUND
    )


def test_a_missing_file_is_still_a_bad_request() -> None:
    """Guard: the missing-resource marker must not widen past models.

    ``FALLBACK_PROVIDER`` on a malformed request burns a second provider on a
    call that is malformed against every one of them, so "does not exist" only
    counts when the message is about a model.
    """
    assert (
        classify_provider_error(
            provider_name="openai",
            status_code=400,
            raw_code="invalid_request_error",
            message="The file you provided does not exist",
        )
        is ProviderFailureKind.BAD_REQUEST
    )


def test_an_unrelated_model_mention_is_not_a_missing_model() -> None:
    """Guard: naming a model is not the same as reporting one missing."""
    assert (
        classify_provider_error(
            provider_name="openrouter",
            status_code=None,
            message="openrouter model id 520",
        )
        is ProviderFailureKind.UNKNOWN
    )


def test_404_outside_the_openai_compatible_family_is_unchanged() -> None:
    """Guard: the change is scoped to the family the issue reports."""
    assert (
        classify_provider_error(
            provider_name="anthropic",
            status_code=404,
            raw_code="not_found_error",
            message="a resource was not found",
        )
        is ProviderFailureKind.UNKNOWN
    )
