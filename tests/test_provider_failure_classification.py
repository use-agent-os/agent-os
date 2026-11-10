from __future__ import annotations

import pytest

from agentos.engine.fallback import ProviderErrorKind, classify_error
from agentos.engine.policy import AttemptHistory, ProviderAttemptPolicy, Retry
from agentos.provider.failures import (
    ProviderFailureKind,
    ProviderRecoveryAction,
    classify_provider_error,
    decide_recovery_action,
)


def _retried(kind: ProviderErrorKind) -> bool:
    """Whether the attempt policy retries a first failure of *kind*."""
    decision = ProviderAttemptPolicy().decide(kind, AttemptHistory(), message="boom")
    return isinstance(decision, Retry)


@pytest.mark.parametrize(
    "provider",
    [
        "deepseek",
        "gemini",
        "dashscope",
        "bailian_coding",
        "moonshot",
        "mistral",
        "groq",
        "zhipu",
        "siliconflow",
        "volcengine",
        "byteplus",
        "qianfan",
        "aihubmix",
        "minimax_openai",
        "vllm",
        "lm_studio",
        "ovms",
    ],
)
def test_openai_compatible_providers_share_common_failure_classification(provider: str) -> None:
    assert (
        classify_provider_error(provider, 401, message="invalid api key")
        is ProviderFailureKind.AUTH_INVALID
    )
    assert (
        classify_provider_error(provider, 429, message="rate limit exceeded")
        is ProviderFailureKind.RATE_LIMITED
    )
    assert (
        classify_provider_error(provider, 404, message="model not found")
        is ProviderFailureKind.MODEL_NOT_FOUND
    )
    assert (
        classify_provider_error(provider, 400, message="unsupported parameter")
        is ProviderFailureKind.UNSUPPORTED_FEATURE
    )


@pytest.mark.parametrize("provider", ["minimax", "minimax_cn", "minimax_global"])
def test_minimax_region_profiles_use_anthropic_failure_classification(provider: str) -> None:
    assert (
        classify_provider_error(provider, 401, raw_code="authentication_error")
        is ProviderFailureKind.AUTH_INVALID
    )


@pytest.mark.parametrize(
    "message",
    [
        "Request error: connection reset by peer",
        "Request error: All connection attempts failed",
        "ReadTimeout while contacting provider",
        "ConnectTimeout while contacting provider",
    ],
)
def test_agent_fallback_retries_transport_transient_errors(message: str) -> None:
    kind = classify_error(message)

    assert kind is ProviderErrorKind.TRANSPORT_TRANSIENT
    assert _retried(kind) is True


def test_agent_fallback_retries_timeout_code_when_message_is_sparse() -> None:
    kind = classify_error(
        "Request timed out: ",
        provider_name="openrouter",
        raw_code="timeout",
    )

    assert kind is ProviderErrorKind.TRANSPORT_TRANSIENT
    assert _retried(kind) is True


@pytest.mark.parametrize(
    "message",
    [
        "HTTP 520: upstream provider returned an unknown error",
        "HTTP 522",
        "HTTP 523",
        "HTTP 524",
        "HTTP 504",
        "status_code: 523",
    ],
)
def test_agent_fallback_retries_gateway_transient_http_errors(message: str) -> None:
    kind = classify_error(message)

    assert kind is ProviderErrorKind.TRANSPORT_TRANSIENT
    assert _retried(kind) is True


@pytest.mark.parametrize(
    "message",
    [
        "Cloudflare returned 520",
        "upstream returned 522",
        "OpenRouter upstream error 520",
        "provider backend returned 524",
        "524 from backend failure",
    ],
)
def test_agent_fallback_retries_gateway_context_transient_codes(message: str) -> None:
    kind = classify_error(message)

    assert kind is ProviderErrorKind.TRANSPORT_TRANSIENT
    assert _retried(kind) is True


@pytest.mark.parametrize(
    "message",
    [
        "random value 520",
        "line 520",
        "520 tokens",
        "issue 520",
        "the provider sent 520 tokens",
        "edge case 520",
        "proxy line 520",
        "gateway request id 520",
        "openrouter model id 520",
        "upstream metadata 520",
        "Cloudflare is configured. " + ("x" * 120) + " value 520",
    ],
)
def test_agent_fallback_does_not_retry_unscoped_gateway_numbers(message: str) -> None:
    kind = classify_error(message)

    assert kind is ProviderErrorKind.UNKNOWN
    assert _retried(kind) is False


@pytest.mark.parametrize("provider", ["openrouter", "deepseek"])
@pytest.mark.parametrize(
    "message",
    [
        "Cloudflare returned 520",
        "upstream returned 522",
        "OpenRouter upstream error 520",
        "provider backend returned 524",
        "HTTP 523",
    ],
)
def test_provider_failure_classifies_gateway_transient_errors(provider: str, message: str) -> None:
    assert (
        classify_provider_error(provider, None, message=message)
        is ProviderFailureKind.PROVIDER_OVERLOADED
    )


@pytest.mark.parametrize(
    "message",
    [
        "random value 520",
        "line 520",
        "520 tokens",
        "the provider sent 520 tokens",
        "edge case 520",
        "proxy line 520",
        "gateway request id 520",
        "openrouter model id 520",
        "upstream metadata 520",
        "Cloudflare is configured. " + ("x" * 120) + " value 520",
    ],
)
def test_provider_failure_does_not_classify_unscoped_gateway_numbers(message: str) -> None:
    assert (
        classify_provider_error("openrouter", None, message=message) is ProviderFailureKind.UNKNOWN
    )


def test_agent_fallback_still_does_not_retry_auth_failures() -> None:
    kind = classify_error("invalid api key")

    assert kind is ProviderErrorKind.AUTH_FAILURE
    assert _retried(kind) is False


@pytest.mark.parametrize(
    ("raw_code", "message"),
    [
        ("empty_response", ""),
        ("", "Provider returned an empty response"),
        ("", "empty_response"),
    ],
)
def test_provider_failure_classifies_empty_responses(raw_code: str, message: str) -> None:
    assert (
        classify_provider_error("openrouter", None, raw_code=raw_code, message=message)
        is ProviderFailureKind.EMPTY_RESPONSE
    )


def test_provider_failure_keeps_empty_http_gateway_body_transient() -> None:
    assert (
        classify_provider_error(
            "openai",
            500,
            message="OpenAI chat request failed (HTTP 500): empty response body",
        )
        is ProviderFailureKind.PROVIDER_OVERLOADED
    )


def test_agent_fallback_identifies_but_does_not_retry_empty_responses() -> None:
    kind = classify_error("Provider returned an empty response")

    assert kind is ProviderErrorKind.EMPTY_RESPONSE
    assert _retried(kind) is False


@pytest.mark.parametrize(
    "status_code",
    [499, 500, 521, 529],
)
def test_new_transient_status_codes_classify_as_provider_overloaded(
    status_code: int,
) -> None:
    assert (
        classify_provider_error("openrouter", status_code, message="")
        is ProviderFailureKind.PROVIDER_OVERLOADED
    )
    assert (
        classify_provider_error("anthropic", status_code, message="")
        is ProviderFailureKind.PROVIDER_OVERLOADED
    )


@pytest.mark.parametrize("status_code", [520, 521])
def test_anthropic_gateway_status_codes_use_canonical_transient_set(
    status_code: int,
) -> None:
    assert (
        classify_provider_error("anthropic", status_code, message="")
        is ProviderFailureKind.PROVIDER_OVERLOADED
    )


def test_anthropic_gateway_status_codes_match_via_text() -> None:
    assert (
        classify_provider_error("anthropic", None, message="HTTP 520")
        is ProviderFailureKind.PROVIDER_OVERLOADED
    )


@pytest.mark.parametrize(
    "message",
    [
        "HTTP 499",
        "HTTP 500",
        "HTTP 502",
        "HTTP 503",
        "HTTP 521",
        "HTTP 529",
        "status_code: 500",
        "error code 521",
    ],
)
def test_new_transient_status_codes_match_via_text(message: str) -> None:
    assert (
        classify_provider_error("openrouter", None, message=message)
        is ProviderFailureKind.PROVIDER_OVERLOADED
    )


@pytest.mark.parametrize(
    "message",
    [
        "model not found",
        "model 'llama3' not found, try pulling it first",
        "pull the model first",
    ],
)
def test_ollama_missing_model_messages_classify_as_model_not_found(message: str) -> None:
    assert (
        classify_provider_error("ollama", None, message=message)
        is ProviderFailureKind.MODEL_NOT_FOUND
    )


@pytest.mark.parametrize(
    "message",
    [
        "pull failed",
        "model registry unavailable",
    ],
)
def test_ollama_partial_pull_model_matches_do_not_classify_as_model_not_found(
    message: str,
) -> None:
    assert (
        classify_provider_error("ollama", None, message=message)
        is not ProviderFailureKind.MODEL_NOT_FOUND
    )


@pytest.mark.parametrize(
    "message",
    [
        "connection refused",
        "connection error",
        "request error",
        "timeout",
    ],
)
def test_ollama_transport_messages_classify_as_transport_transient(message: str) -> None:
    assert (
        classify_provider_error("ollama", None, message=message)
        is ProviderFailureKind.TRANSPORT_TRANSIENT
    )


@pytest.mark.parametrize(
    ("provider", "status_code", "raw_code", "message"),
    [
        # Azure OpenAI: canonical content-management-policy wording. Note the words
        # "content" and "policy" are not adjacent, so the pre-existing
        # "content policy" marker never fired on this message.
        (
            "azure",
            400,
            "content_filter",
            "The response was filtered due to the prompt triggering Azure OpenAI's "
            "content management policy. Please modify your prompt and retry.",
        ),
        # OpenAI/Azure error code and finish_reason.
        ("openai", 400, "content_filter", "The response was filtered"),
        # Azure responsible AI policy code.
        (
            "azure",
            400,
            "responsible_ai_policy",
            "Your request was rejected by the responsible_ai_policy filter.",
        ),
        # "content filter" spelled with a space, e.g. "flagged by content filter".
        ("openai", 400, "", "This prompt was flagged by content filter."),
        # Google Gemini block reason.
        ("gemini", 400, "", "Candidate blocked by safety settings."),
    ],
)
def test_provider_specific_policy_markers_classify_as_policy_refusal(
    provider: str,
    status_code: int,
    raw_code: str,
    message: str,
) -> None:
    assert (
        classify_provider_error(provider, status_code, raw_code=raw_code, message=message)
        is ProviderFailureKind.POLICY_REFUSAL
    )


@pytest.mark.parametrize(
    ("provider", "status_code", "raw_code", "message", "expected"),
    [
        # A plain bad request must not be swept up by the new markers.
        (
            "openai",
            400,
            "invalid_request_error",
            "Unsupported parameter: 'max_tokens'.",
            ProviderFailureKind.UNSUPPORTED_FEATURE,
        ),
        (
            "azure",
            400,
            "invalid_request_error",
            "Missing required parameter: 'messages'.",
            ProviderFailureKind.BAD_REQUEST,
        ),
        # Rate limits stay rate limits.
        (
            "openai",
            429,
            "rate_limit_exceeded",
            "Rate limit reached for gpt-4o.",
            ProviderFailureKind.RATE_LIMITED,
        ),
        # Context overflow is checked before policy refusal and stays put.
        (
            "openai",
            400,
            "context_length_exceeded",
            "This model's maximum context length is 128000 tokens.",
            ProviderFailureKind.CONTEXT_OVERFLOW,
        ),
    ],
)
def test_policy_markers_do_not_capture_unrelated_failures(
    provider: str,
    status_code: int,
    raw_code: str,
    message: str,
    expected: ProviderFailureKind,
) -> None:
    assert (
        classify_provider_error(provider, status_code, raw_code=raw_code, message=message)
        is expected
    )


_ANTHROPIC_FAMILY = ["anthropic", "minimax", "minimax_cn", "minimax_global"]


@pytest.mark.parametrize("provider", _ANTHROPIC_FAMILY)
def test_anthropic_family_404_is_a_missing_model_not_unknown(provider: str) -> None:
    """A 404 from an Anthropic-shaped provider means the model is not there."""
    assert (
        classify_provider_error(
            provider,
            404,
            raw_code="not_found_error",
            message="model: claude-3-5-sonnet-fake not found",
        )
        is ProviderFailureKind.MODEL_NOT_FOUND
    )


@pytest.mark.parametrize("provider", _ANTHROPIC_FAMILY)
def test_anthropic_family_not_found_error_without_a_status_code(provider: str) -> None:
    """Transports that surface the error code but no status still classify."""
    assert (
        classify_provider_error(provider, None, raw_code="not_found_error")
        is ProviderFailureKind.MODEL_NOT_FOUND
    )


@pytest.mark.parametrize("provider", _ANTHROPIC_FAMILY)
def test_anthropic_family_bare_404_classifies_without_a_body(provider: str) -> None:
    """A 404 with an empty body is still a model-or-base-url problem."""
    assert classify_provider_error(provider, 404, message="") is ProviderFailureKind.MODEL_NOT_FOUND


def test_anthropic_missing_model_reaches_the_fallback_chain() -> None:
    """The point of the classification: SURFACE would halt the session."""
    kind = classify_provider_error("anthropic", 404, raw_code="not_found_error")

    assert decide_recovery_action(kind) is ProviderRecoveryAction.FALLBACK_PROVIDER


@pytest.mark.parametrize(
    ("status_code", "raw_code", "expected"),
    [
        (401, "authentication_error", ProviderFailureKind.AUTH_INVALID),
        (403, "authentication_error", ProviderFailureKind.AUTH_INVALID),
        (402, "billing_error", ProviderFailureKind.INSUFFICIENT_CREDITS),
        (429, "rate_limit_error", ProviderFailureKind.RATE_LIMITED),
        (529, "overloaded_error", ProviderFailureKind.PROVIDER_OVERLOADED),
        (400, "invalid_request_error", ProviderFailureKind.BAD_REQUEST),
    ],
)
def test_anthropic_other_failure_kinds_keep_their_classification(
    status_code: int, raw_code: str, expected: ProviderFailureKind
) -> None:
    """The new 404 arm sits between rate limiting and the transient set.

    Inserting a branch into an ordered if-chain can shadow the arms after it;
    these lock every neighbour that was already classified.
    """
    assert classify_provider_error("anthropic", status_code, raw_code=raw_code) is expected


def test_anthropic_404_does_not_outrank_an_exhausted_credit_balance() -> None:
    """Credit exhaustion is checked before the provider branches and stays first.

    A depleted account needs FAIL-style visibility, not a silent hop to the
    next provider that will bill the same card.
    """
    assert (
        classify_provider_error(
            "anthropic", 404, raw_code="billing_error", message="credit balance is too low"
        )
        is ProviderFailureKind.INSUFFICIENT_CREDITS
    )


@pytest.mark.parametrize("provider", ["ollama", "some_unregistered_provider"])
def test_not_found_error_stays_unknown_outside_the_anthropic_branch(provider: str) -> None:
    """The 404 arm is scoped to Anthropic-shaped providers, not global.

    A global 404 rule would reclassify every provider that answers an ordinary
    missing resource with 404 and send the turn to a second provider that
    cannot serve it either.
    """
    assert (
        classify_provider_error(provider, 404, raw_code="not_found_error")
        is ProviderFailureKind.UNKNOWN
    )


@pytest.mark.parametrize(
    ("status", "code", "message"),
    [
        (529, "529", "HTTP 529"),
        (None, "overloaded_error", "Overloaded"),
        (503, "503", "The model is overloaded, try again later"),
    ],
)
def test_agent_fallback_classifies_explicit_overload_as_overloaded(
    status: int | None, code: str, message: str
) -> None:
    kind = classify_error(message, status_code=status, raw_code=code)

    assert kind is ProviderErrorKind.OVERLOADED
    assert _retried(kind) is True


@pytest.mark.parametrize("provider", ["ollama", "my-custom-gateway", ""])
def test_401_is_an_auth_failure_that_is_never_retried(provider: str) -> None:
    """The family-agnostic 401 rule, end to end: the classifier says
    ``AUTH_INVALID``, the policy fails fast and names the fix."""
    from agentos.engine.policy import Fail
    from agentos.engine.policy.attempt import AUTH_FAILURE_GUIDANCE

    assert (
        classify_provider_error(provider, 401, message="nope") is ProviderFailureKind.AUTH_INVALID
    )
    kind = classify_error("nope", provider_name=provider, status_code=401, raw_code="401")
    assert kind is ProviderErrorKind.AUTH_FAILURE
    decision = ProviderAttemptPolicy().decide(kind, AttemptHistory(), message="nope")
    assert isinstance(decision, Fail)
    assert AUTH_FAILURE_GUIDANCE in decision.message


def test_401_from_an_unlisted_provider_family_is_auth_invalid() -> None:
    assert (
        classify_provider_error("my-custom-gateway", 401, raw_code="401", message="nope")
        is ProviderFailureKind.AUTH_INVALID
    )
    # 403 stays unclassified: providers use it for policy / region refusals too.
    assert (
        classify_provider_error("my-custom-gateway", 403, raw_code="403", message="nope")
        is not ProviderFailureKind.AUTH_INVALID
    )
