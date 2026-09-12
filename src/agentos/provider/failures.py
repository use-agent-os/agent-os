"""Provider failure classification and runtime recovery decisions."""

from __future__ import annotations

import re
from enum import StrEnum


class ProviderFailureKind(StrEnum):
    RATE_LIMITED = "rate_limited"
    PROVIDER_OVERLOADED = "provider_overloaded"
    AUTH_INVALID = "auth_invalid"
    CONTEXT_OVERFLOW = "context_overflow"
    UNSUPPORTED_FEATURE = "unsupported_feature"
    INSUFFICIENT_CREDITS = "insufficient_credits"
    MODEL_NOT_FOUND = "model_not_found"
    TRANSPORT_TRANSIENT = "transport_transient"
    POLICY_REFUSAL = "policy_refusal"
    EMPTY_RESPONSE = "empty_response"
    MALFORMED_RESPONSE = "malformed_response"
    BAD_REQUEST = "bad_request"
    UNKNOWN = "unknown"


class ProviderRecoveryAction(StrEnum):
    RETRY = "retry"
    RETRY_THEN_FALLBACK = "retry_then_fallback"
    FALLBACK_PROVIDER = "fallback_provider"
    COMPACT_AND_RETRY = "compact_and_retry"
    FAIL_CONFIG = "fail_config"
    SURFACE = "surface"


def _openai_compat_provider_ids() -> frozenset[str]:
    """Provider ids whose registry spec declares the OpenAI-compatible family.

    Derived from ``provider/registry.py`` rather than hand-kept. The literal
    list this replaces drifted twice — a provider was registered with
    ``failure_family="openai_compat"`` and never added here, so its 401/402/429
    fell through to ``UNKNOWN`` and lost the retry and credit semantics every
    sibling gets. ``failure_family`` is already the field that answers this
    question; reading it keeps the two files from disagreeing a third time.
    """
    from agentos.provider.registry import list_provider_specs

    return frozenset(
        spec.provider_id for spec in list_provider_specs() if spec.failure_family == "openai_compat"
    )


_OPENAI_COMPAT_PROVIDERS: frozenset[str] = _openai_compat_provider_ids()

_GATEWAY_TRANSIENT_STATUS_CODES = {499, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}
_GATEWAY_CODES = r"(?:499|500|502|503|504|520|521|522|523|524|529)"
_GATEWAY_CONTEXT = r"(?:cloudflare|openrouter|upstream|gateway|backend)"
_GATEWAY_ERROR_TERMS = (
    r"(?:error|returned|returning|failed|failure|unreachable|timeout|timed out|"
    r"overload(?:ed)?|bad gateway|origin)"
)
_GATEWAY_TRANSIENT_RE = re.compile(
    r"\b(?:http(?: status)?|status(?:[_ -]?code)?|error code|code)\s*[:=]?\s*"
    rf"{_GATEWAY_CODES}\b"
    rf"|\b{_GATEWAY_CONTEXT}\b[^\n]{{0,80}}\b{_GATEWAY_ERROR_TERMS}\b[^\n]{{0,80}}\b{_GATEWAY_CODES}\b"
    rf"|\b{_GATEWAY_CONTEXT}\b[^\n]{{0,80}}\b{_GATEWAY_CODES}\b[^\n]{{0,80}}\b{_GATEWAY_ERROR_TERMS}\b"
    rf"|\b{_GATEWAY_CODES}\b[^\n]{{0,80}}\b{_GATEWAY_CONTEXT}\b[^\n]{{0,80}}\b{_GATEWAY_ERROR_TERMS}\b"
)


def _joined(status_code: int | None, raw_code: str, message: str) -> str:
    return f"{status_code or ''} {raw_code or ''} {message or ''}".lower()


def _is_context_overflow(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            # OpenAI-compatible providers
            "context length",
            "context window",
            "maximum context",
            "prompt is too long",
            "input is too long",
            "input exceeds",
            "exceeds the maximum number of tokens",
            "provider_request_budget_exhausted",
            "too many tokens",
            # Anthropic-specific markers
            "prompt_too_long",
            "exceed context limit",
            "request_too_large",
            "request size exceeds",
        )
    )


def _is_policy_refusal(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "content policy",
            "policy violation",
            "safety policy",
            "moderation",
            "refusal",
            "blocked by policy",
            # Azure OpenAI / OpenAI error code and finish_reason
            "content_filter",
            # e.g. "flagged by content filter"
            "content filter",
            # Azure responsible AI policy code
            "responsible_ai_policy",
            # Azure OpenAI canonical phrasing; "content" and "policy" are not
            # adjacent here, so "content policy" above does not cover it
            "content management policy",
            # Google Gemini block reason
            "blocked by safety",
        )
    )


def _is_empty_response(raw_code: str, message: str) -> bool:
    normalized_code = (raw_code or "").strip().lower()
    normalized_message = (message or "").strip().lower()
    return normalized_code == "empty_response" or normalized_message in {
        "empty_response",
        "empty response",
        "provider returned an empty response",
    }


def _is_insufficient_credits(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            # OpenAI / OpenAI-compatible providers
            "insufficient_quota",
            "insufficient quota",
            "exceeded your current quota",
            # Anthropic
            "billing_error",
            "credit balance is too low",
            # Common across gateways
            "billing hard limit",
            "exceeded spend limit",
        )
    )


def _is_gateway_transient(text: str) -> bool:
    return bool(_GATEWAY_TRANSIENT_RE.search(text))


#: Phrases a provider uses to say the thing it was asked for is not there.
#: Deliberately not a bare ``"not_found"``: that substring turns up in unrelated
#: error codes, and every one it caught would be routed to a second provider.
_MISSING_MARKERS: tuple[str, ...] = (
    # OpenAI error code, and its message:
    # "The model `gpt-5` does not exist or you do not have access to it."
    "model_not_found",
    "does not exist",
    # Google: "models/gemini-2.5-pro is not found for API version v1beta"
    "is not found",
    "was not found",
)


def _names_a_missing_model(text: str) -> bool:
    """Return whether *text* says a **model** is missing, not some other resource.

    The model word is load-bearing, not decoration. OpenAI answers an ordinary
    bad request with ``400 invalid_request_error`` and "The file you provided
    does not exist"; on the marker alone that would classify as
    ``MODEL_NOT_FOUND`` and earn ``FALLBACK_PROVIDER``, which burns a second
    provider on a request that is malformed against every one of them.
    """
    return "model" in text and any(marker in text for marker in _MISSING_MARKERS)


def classify_provider_error(
    provider_name: str,
    status_code: int | None,
    raw_code: str = "",
    message: str = "",
) -> ProviderFailureKind:
    """Classify a provider error into a stable runtime failure kind."""

    provider = (provider_name or "").lower()
    text = _joined(status_code, raw_code, message)

    if _is_context_overflow(text):
        return ProviderFailureKind.CONTEXT_OVERFLOW
    if _is_policy_refusal(text):
        return ProviderFailureKind.POLICY_REFUSAL
    if _is_empty_response(raw_code, message):
        return ProviderFailureKind.EMPTY_RESPONSE
    if _is_insufficient_credits(text):
        return ProviderFailureKind.INSUFFICIENT_CREDITS

    if provider in _OPENAI_COMPAT_PROVIDERS:
        if status_code in {401, 403} or "invalid api key" in text or "unauthorized" in text:
            return ProviderFailureKind.AUTH_INVALID
        if status_code == 402 or "insufficient credits" in text or "no credits" in text:
            return ProviderFailureKind.INSUFFICIENT_CREDITS
        if status_code == 429 or "rate limit" in text or "rate_limit" in text:
            return ProviderFailureKind.RATE_LIMITED
        # A 404 from a chat-completions endpoint is a model or base-url problem,
        # and falling through to the next configured model is the right recovery
        # for both. Google answers an unavailable model with 404 and a body that
        # matches neither literal above, so the whole turn surfaced as UNKNOWN
        # and the fallback chain never ran (#1359).
        if (
            status_code == 404
            or "no endpoints found" in text
            or "model not found" in text
            or _names_a_missing_model(text)
        ):
            return ProviderFailureKind.MODEL_NOT_FOUND
        if "does not support" in text or "unsupported" in text:
            return ProviderFailureKind.UNSUPPORTED_FEATURE
        if (
            status_code in _GATEWAY_TRANSIENT_STATUS_CODES
            or "overloaded" in text
            or _is_gateway_transient(text)
        ):
            return ProviderFailureKind.PROVIDER_OVERLOADED
        if status_code == 400 or "invalid_request" in text:
            return ProviderFailureKind.BAD_REQUEST

    if provider in {"anthropic", "minimax", "minimax_cn", "minimax_global"}:
        if status_code in {401, 403} or "authentication_error" in text:
            return ProviderFailureKind.AUTH_INVALID
        if status_code == 402 or "billing_error" in text:
            return ProviderFailureKind.INSUFFICIENT_CREDITS
        if status_code == 429 or "rate_limit_error" in text:
            return ProviderFailureKind.RATE_LIMITED
        if status_code in _GATEWAY_TRANSIENT_STATUS_CODES or "overloaded_error" in text:
            return ProviderFailureKind.PROVIDER_OVERLOADED
        if "invalid_request_error" in text:
            return ProviderFailureKind.BAD_REQUEST

    if provider == "ollama":
        if "model not found" in text or ("pull" in text and "model" in text):
            return ProviderFailureKind.MODEL_NOT_FOUND
        if (
            "connection refused" in text
            or "connection error" in text
            or "request error" in text
            or "timeout" in text
        ):
            return ProviderFailureKind.TRANSPORT_TRANSIENT

    if status_code == 429 or "rate limit" in text:
        return ProviderFailureKind.RATE_LIMITED
    if status_code in _GATEWAY_TRANSIENT_STATUS_CODES or _is_gateway_transient(text):
        return ProviderFailureKind.PROVIDER_OVERLOADED
    if "malformed" in text or "invalid json" in text:
        return ProviderFailureKind.MALFORMED_RESPONSE
    if "timeout" in text or "request error" in text:
        return ProviderFailureKind.TRANSPORT_TRANSIENT

    return ProviderFailureKind.UNKNOWN


def decide_recovery_action(kind: ProviderFailureKind) -> ProviderRecoveryAction:
    """Map classified provider failure to the first runtime recovery action."""

    if kind is ProviderFailureKind.CONTEXT_OVERFLOW:
        return ProviderRecoveryAction.COMPACT_AND_RETRY
    if kind in {
        ProviderFailureKind.PROVIDER_OVERLOADED,
        ProviderFailureKind.TRANSPORT_TRANSIENT,
    }:
        return ProviderRecoveryAction.RETRY_THEN_FALLBACK
    if kind in {
        ProviderFailureKind.RATE_LIMITED,
        ProviderFailureKind.INSUFFICIENT_CREDITS,
        ProviderFailureKind.MODEL_NOT_FOUND,
        ProviderFailureKind.UNSUPPORTED_FEATURE,
    }:
        return ProviderRecoveryAction.FALLBACK_PROVIDER
    if kind is ProviderFailureKind.AUTH_INVALID:
        return ProviderRecoveryAction.FAIL_CONFIG
    return ProviderRecoveryAction.SURFACE


def is_transport_timeout(raw_code: str = "", message: str = "") -> bool:
    """Return True when the error is specifically a request timeout.

    Transport-transient covers both brief blips (connection reset, DNS hiccup)
    and hard timeouts (upstream hangs for the full read-timeout window).
    Timeouts are far less likely to resolve on a simple same-model retry —
    the upstream is usually genuinely unreachable — so callers can use this
    to cap retries more aggressively.
    """
    code = (raw_code or "").strip().lower()
    msg = (message or "").strip().lower()
    if code == "timeout":
        return True
    return "timed out" in msg or "request timed out" in msg
