from __future__ import annotations

from agentos.provider.failures import ProviderFailureKind, classify_provider_error


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
    # Gemini's real, canonical context-overflow error. Numbers vary per
    # request but the surrounding phrasing is fixed. Before the fix this
    # matched none of the context-overflow markers and fell through to
    # BAD_REQUEST, so the runtime never triggered COMPACT_AND_RETRY.
    message = (
        "The input token count (5911388) exceeds the maximum number of "
        "tokens allowed (1048576)."
    )

    assert (
        classify_provider_error(
            provider_name="gemini",
            status_code=400,
            raw_code="400",
            message=message,
        )
        is ProviderFailureKind.CONTEXT_OVERFLOW
    )


def test_gemini_input_token_count_message_is_context_overflow_regardless_of_token_counts() -> (
    None
):
    # Same shape, different digit counts — guards against a marker that
    # accidentally depends on a specific number of digits.
    message = (
        "The input token count (132478) exceeds the maximum number of "
        "tokens allowed (131072)."
    )

    assert (
        classify_provider_error(
            provider_name="gemini",
            status_code=400,
            message=message,
        )
        is ProviderFailureKind.CONTEXT_OVERFLOW
    )
