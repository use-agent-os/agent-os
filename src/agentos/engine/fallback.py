"""Provider error kinds and the delay helpers of the retry policy.

What to do after a failed attempt is decided by
:class:`agentos.engine.policy.ProviderAttemptPolicy`; this module only
classifies the failure and computes waits.
"""

from __future__ import annotations

import random
from enum import StrEnum

from agentos.provider.failures import ProviderFailureKind, classify_provider_error


class ProviderErrorKind(StrEnum):
    RATE_LIMIT = "rate_limit"
    AUTH_FAILURE = "auth_failure"
    OVERLOADED = "overloaded"
    CONTEXT_OVERFLOW = "context_overflow"
    TRANSPORT_TRANSIENT = "transport_transient"
    EMPTY_RESPONSE = "empty_response"
    UNKNOWN = "unknown"


_FAILURE_KIND_MAP: dict[ProviderFailureKind, ProviderErrorKind] = {
    ProviderFailureKind.PROVIDER_OVERLOADED: ProviderErrorKind.TRANSPORT_TRANSIENT,
    ProviderFailureKind.TRANSPORT_TRANSIENT: ProviderErrorKind.TRANSPORT_TRANSIENT,
    ProviderFailureKind.RATE_LIMITED: ProviderErrorKind.RATE_LIMIT,
    ProviderFailureKind.AUTH_INVALID: ProviderErrorKind.AUTH_FAILURE,
    ProviderFailureKind.CONTEXT_OVERFLOW: ProviderErrorKind.CONTEXT_OVERFLOW,
    ProviderFailureKind.EMPTY_RESPONSE: ProviderErrorKind.EMPTY_RESPONSE,
}


def _reports_overload(status_code: int | None, raw_code: str, message: str) -> bool:
    code = str(raw_code or "").strip().lower()
    if status_code == 529 or code == "529":
        return True
    return "overload" in code or "overload" in str(message or "").lower()


def classify_error(
    message: str,
    *,
    provider_name: str = "openrouter",
    status_code: int | None = None,
    raw_code: str = "",
) -> ProviderErrorKind:
    """Classify a provider error into the kind the attempt policy decides on."""
    kind = classify_provider_error(
        provider_name,
        status_code,
        raw_code=raw_code,
        message=message,
    )
    if kind is ProviderFailureKind.PROVIDER_OVERLOADED and _reports_overload(
        status_code, raw_code, message
    ):
        # The provider family lumps every 5xx together; a gateway blip
        # (502/504/52x) stays transport-transient, an explicit overload
        # (529, ``overloaded_error``) is its own kind.
        return ProviderErrorKind.OVERLOADED
    return _FAILURE_KIND_MAP.get(kind, ProviderErrorKind.UNKNOWN)


def backoff_sleep(
    attempt: int,
    base_ms: int = 1000,
    max_ms: int = 30_000,
    _fake: bool = False,
) -> float:
    """Calculate exponential backoff delay with multiplicative jitter.

    The nominal delay ``base_ms * 2**attempt`` is multiplied by a random factor
    in ``[0.5, 1.5]`` and then capped at ``max_ms``. A multiplicative spread
    keeps sessions that hit the same limit at the same moment from retrying in
    lockstep at every later attempt too (an additive jitter of a fixed size
    becomes negligible once the delay grows).

    Args:
        attempt: 0-indexed retry attempt number.
        base_ms: Base delay in milliseconds.
        max_ms: Maximum delay cap in milliseconds.
        _fake: If True, return the delay without actually sleeping (for testing).

    Returns:
        Calculated delay in seconds.
    """
    import structlog

    factor = random.uniform(0.5, 1.5)  # noqa: S311
    delay_ms = min(base_ms * (2**attempt) * factor, max_ms)
    delay_s = delay_ms / 1000.0

    if not _fake:
        structlog.get_logger().info("backoff_sleep", delay_s=round(delay_s, 2), attempt=attempt)

    return float(delay_s)


def retry_after_delay(retry_after_seconds: float) -> float:
    """Seconds to sleep for a provider ``Retry-After`` hint, plus a small jitter.

    The hint is honoured exactly; the jitter (up to 10% of the hint, between
    0.1 s and 1 s) only keeps concurrent turns that got the same hint from
    landing on the provider in the same instant.
    """
    base = max(0.0, float(retry_after_seconds))
    jitter_cap = min(1.0, max(0.1, base * 0.1))
    return base + random.uniform(0.0, jitter_cap)  # noqa: S311
