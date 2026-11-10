"""What to do after a provider request fails: one decision per failed attempt.

The agent loop classifies a failed attempt, records it in an
:class:`AttemptHistory`, and asks :meth:`ProviderAttemptPolicy.decide` for
exactly one :data:`RecoveryDecision`:

* :class:`Retry` -- wait ``delay_s`` and send the same request to the same model;
* :class:`Failover` -- switch to the next candidate model (the loop answers
  whether one exists; if not, it asks again with ``failover_available=False``);
* :class:`CompactThenRetry` -- shrink the context, then retry;
* :class:`Fail` -- end the turn with ``message``.

Rules (OpenSpec ``provider-attempt-recovery``):

* Rate-limit, overload and transient transport failures retry on the same
  model, bounded by an attempt count *and* a wall-clock budget. Once either is
  spent the policy fails over, and fails the turn when no candidate remains.
* The budget charges the waits plus each failed attempt's duration, capped at
  ``max_attempt_charge_s``: a long stream that drops late is one attempt, not
  the whole budget. The first retry is only refused by the budget when its
  wait alone is longer than the budget.
* A ``Retry-After`` hint is a minimum delay. A hint longer than the cap is not
  waited out: the policy fails over instead.
* Delays without a hint grow exponentially with multiplicative jitter.
* A timeout is retried at most ``max_timeout_retries`` times; after that the
  policy asks for one context reduction (when enabled and the caller says the
  request is large enough to be worth reducing), then fails over, then fails.
* Authentication failures fail fast with a message that names the fix.
  Unclassified failures fail without retrying.

Context overflow, empty responses and invalid (reasoning-only, malformed)
responses keep their dedicated handling in the loop; this policy owns the
generic transient path.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field

from agentos.engine.fallback import ProviderErrorKind, backoff_sleep, retry_after_delay

_TRANSIENT_KINDS = frozenset(
    {
        ProviderErrorKind.RATE_LIMIT,
        ProviderErrorKind.OVERLOADED,
        ProviderErrorKind.TRANSPORT_TRANSIENT,
    }
)

AUTH_FAILURE_GUIDANCE = (
    "The provider rejected the credentials. Check the API key configured for "
    "this provider (`agentos doctor` reports which one is in use)."
)

TIMEOUT_GUIDANCE = (
    "The upstream model endpoint did not respond within the timeout window. "
    "Try switching to a different model tier with /c0, /c2, or /auto to "
    "restore automatic routing."
)


@dataclass(frozen=True)
class Retry:
    delay_s: float
    reason: str
    action: str = field(default="retry", init=False)


@dataclass(frozen=True)
class Failover:
    reason: str
    action: str = field(default="failover", init=False)


@dataclass(frozen=True)
class CompactThenRetry:
    reason: str
    action: str = field(default="compact_then_retry", init=False)


@dataclass(frozen=True)
class Fail:
    message: str
    reason: str
    action: str = field(default="fail", init=False)


RecoveryDecision = Retry | Failover | CompactThenRetry | Fail


@dataclass(frozen=True)
class AttemptBudget:
    """Limits for same-model recovery within one provider call of a turn."""

    max_retries: int = 6
    #: Wall-clock seconds same-model retries may spend (waits plus the time the
    #: failed attempts took, each capped at ``max_attempt_charge_s``), counted
    #: from the first failure. ``<= 0`` means the count alone bounds retries.
    budget_seconds: float = 90.0
    #: The most one failed attempt is charged to the budget.
    max_attempt_charge_s: float = 10.0
    base_backoff_ms: int = 1000
    max_backoff_ms: int = 30_000
    #: Longest ``Retry-After`` worth waiting out on the same model.
    retry_after_cap_s: float = 30.0
    max_timeout_retries: int = 1
    #: Shrink the context once after timeouts before failing.
    timeout_context_recovery: bool = True


@dataclass
class AttemptHistory:
    """What already happened to the current provider call."""

    #: Same-model retries already made (any transient kind).
    retries: int = 0
    timeout_retries: int = 0
    #: Seconds already charged to recovery: waits plus failed attempts.
    spent_s: float = 0.0
    failed_over: bool = False
    timeout_compacted: bool = False

    def note_failed_attempt(self, elapsed_s: float, *, cap_s: float) -> None:
        """Charge a failed attempt that took *elapsed_s*, at most *cap_s*."""
        self.spent_s += min(max(0.0, elapsed_s), max(0.0, cap_s))

    def note_retry(self, delay_s: float, *, timeout: bool) -> None:
        self.retries += 1
        self.spent_s += max(0.0, delay_s)
        if timeout:
            self.timeout_retries += 1


@dataclass(frozen=True)
class ProviderAttemptPolicy:
    budget: AttemptBudget = field(default_factory=AttemptBudget)
    #: Injected so tests can pin the jitter.
    jitter: Callable[[int, int, int], float] = field(
        default=lambda attempt, base_ms, max_ms: backoff_sleep(attempt, base_ms, max_ms, _fake=True)
    )

    def decide(
        self,
        kind: ProviderErrorKind,
        history: AttemptHistory,
        *,
        message: str,
        retry_after_s: float | None = None,
        is_timeout: bool = False,
        failover_available: bool = True,
        context_reducible: bool = True,
    ) -> RecoveryDecision:
        """The one recovery action for a failed attempt of *kind*.

        *context_reducible* says whether compacting the context could make a
        timed-out request meaningfully smaller; when it cannot, a spent
        timeout goes straight to failover (or fails).
        """

        if kind is ProviderErrorKind.AUTH_FAILURE:
            return Fail(message=f"{message}. {AUTH_FAILURE_GUIDANCE}", reason="auth_failure")
        if kind not in _TRANSIENT_KINDS:
            return Fail(message=message, reason=f"not_retryable:{kind.value}")

        if is_timeout:
            return self._after_timeout(history, message, failover_available, context_reducible)

        budget = self.budget
        if retry_after_s is not None and retry_after_s > budget.retry_after_cap_s:
            return self._exhausted(
                history, message, failover_available, reason="retry_after_over_cap"
            )
        if history.retries >= budget.max_retries:
            return self._exhausted(history, message, failover_available, reason="retries_spent")

        if retry_after_s is not None and retry_after_s >= 0:
            delay = retry_after_delay(retry_after_s)
            reason = "retry_after"
        else:
            delay = self.jitter(history.retries, budget.base_backoff_ms, budget.max_backoff_ms)
            reason = "backoff"
        # The first retry is always worth one wait: time a slow first attempt
        # took is not held against it, only a wait longer than the budget is.
        spent = history.spent_s if history.retries else 0.0
        if budget.budget_seconds > 0 and spent + delay > budget.budget_seconds:
            return self._exhausted(history, message, failover_available, reason="budget_spent")
        return Retry(delay_s=delay, reason=reason)

    def _after_timeout(
        self,
        history: AttemptHistory,
        message: str,
        failover_available: bool,
        context_reducible: bool,
    ) -> RecoveryDecision:
        budget = self.budget
        if history.timeout_retries < budget.max_timeout_retries and (
            history.retries < budget.max_retries
        ):
            delay = self.jitter(history.retries, budget.base_backoff_ms, budget.max_backoff_ms)
            return Retry(delay_s=delay, reason="timeout")
        if budget.timeout_context_recovery and context_reducible and not history.timeout_compacted:
            return CompactThenRetry(reason="timeout")
        return self._exhausted(
            history,
            f"{message}. {TIMEOUT_GUIDANCE}",
            failover_available,
            reason="timeout_retries_spent",
        )

    @staticmethod
    def _exhausted(
        history: AttemptHistory, message: str, failover_available: bool, *, reason: str
    ) -> RecoveryDecision:
        if failover_available and not history.failed_over:
            return Failover(reason=reason)
        return Fail(message=message, reason=reason)


def seeded_jitter(seed: int) -> Callable[[int, int, int], float]:
    """A deterministic jitter for tests: same seed, same delays."""

    rng = random.Random(seed)  # noqa: S311 - not security sensitive

    def _jitter(attempt: int, base_ms: int, max_ms: int) -> float:
        nominal_ms = float(base_ms * (2**attempt))
        return min(nominal_ms * rng.uniform(0.5, 1.5), float(max_ms)) / 1000.0

    return _jitter
