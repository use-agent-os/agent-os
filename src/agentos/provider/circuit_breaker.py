"""Provider circuit breaker: outage-aware failover with cooldown + half-open probe.

Reactive failover pays the full timeout on a dead provider *every* turn,
because nothing remembers that the last N turns already failed there. This
module adds that memory: consecutive provider-health failures trip a breaker,
an open breaker is skipped by :class:`~agentos.provider.selector.ModelSelector`
for a cooldown window, and a single half-open probe re-closes it once the
provider recovers.

State is keyed by the *configured* provider id (``"openrouter"``,
``"deepseek"``), not the wire-protocol backend class, so an OpenRouter outage
never mutes a separately-configured OpenAI fallback.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .failures import ProviderFailureKind

DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_COOLDOWN_SECONDS = 60.0
DEFAULT_MAX_COOLDOWN_SECONDS = 600.0

_REASON_MAX_CHARS = 200


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


#: Failure kinds that mean *this provider is unhealthy right now*.
#:
#: Deliberately narrow. ``MODEL_NOT_FOUND`` / ``UNSUPPORTED_FEATURE`` /
#: ``BAD_REQUEST`` / ``CONTEXT_OVERFLOW`` are request-shaped, not
#: provider-shaped — tripping on them would park a healthy provider because one
#: model id was wrong. ``AUTH_INVALID`` and ``INSUFFICIENT_CREDITS`` are
#: credential/billing faults that a cooldown cannot heal; they already route to
#: ``FAIL_CONFIG`` / ``FALLBACK_PROVIDER`` and surface their own doctor
#: findings.
TRIPPING_FAILURE_KINDS: frozenset[ProviderFailureKind] = frozenset(
    {
        ProviderFailureKind.PROVIDER_OVERLOADED,
        ProviderFailureKind.TRANSPORT_TRANSIENT,
        ProviderFailureKind.RATE_LIMITED,
    }
)


def trips_breaker(kind: ProviderFailureKind | None) -> bool:
    """True when ``kind`` counts toward opening a provider's breaker."""
    return kind is not None and kind in TRIPPING_FAILURE_KINDS


@dataclass(frozen=True)
class BreakerSettings:
    """Tunables for :class:`ProviderCircuitBreaker`.

    Values are clamped rather than rejected so a hand-edited config can never
    take the runtime down: a nonsensical threshold degrades to the default
    behavior instead of raising at boot.
    """

    enabled: bool = True
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    max_cooldown_seconds: float = DEFAULT_MAX_COOLDOWN_SECONDS

    def __post_init__(self) -> None:
        threshold = max(1, int(self.failure_threshold))
        cooldown = max(1.0, float(self.cooldown_seconds))
        max_cooldown = max(cooldown, float(self.max_cooldown_seconds))
        object.__setattr__(self, "failure_threshold", threshold)
        object.__setattr__(self, "cooldown_seconds", cooldown)
        object.__setattr__(self, "max_cooldown_seconds", max_cooldown)

    @classmethod
    def from_config(cls, config: Any) -> BreakerSettings:
        """Build settings from a config object exposing the same field names.

        Missing attributes fall back to the defaults, so this accepts both the
        real ``llm.circuit_breaker`` config model and ``None``.
        """
        if config is None:
            return cls()
        return cls(
            enabled=bool(getattr(config, "enabled", True)),
            failure_threshold=int(
                getattr(config, "failure_threshold", DEFAULT_FAILURE_THRESHOLD)
            ),
            cooldown_seconds=float(
                getattr(config, "cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)
            ),
            max_cooldown_seconds=float(
                getattr(config, "max_cooldown_seconds", DEFAULT_MAX_COOLDOWN_SECONDS)
            ),
        )


@dataclass
class _Entry:
    """Mutable per-provider breaker state."""

    consecutive_failures: int = 0
    state: BreakerState = BreakerState.CLOSED
    opened_at: float | None = None
    probe_started_at: float | None = None
    #: Consecutive open cycles; drives exponential cooldown backoff.
    open_cycles: int = 0
    total_failures: int = 0
    total_trips: int = 0
    last_failure_kind: str = ""
    last_failure_reason: str = ""


@dataclass(frozen=True)
class ProviderBreakerStatus:
    """Operator-facing snapshot of one provider's breaker."""

    provider: str
    state: BreakerState
    consecutive_failures: int
    failure_threshold: int
    cooldown_remaining_seconds: float
    cooldown_seconds: float
    total_failures: int
    total_trips: int
    last_failure_kind: str = ""
    last_failure_reason: str = ""

    @property
    def healthy(self) -> bool:
        return self.state is BreakerState.CLOSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "state": str(self.state),
            "consecutiveFailures": self.consecutive_failures,
            "failureThreshold": self.failure_threshold,
            "cooldownRemainingSeconds": round(self.cooldown_remaining_seconds, 3),
            "cooldownSeconds": round(self.cooldown_seconds, 3),
            "totalFailures": self.total_failures,
            "totalTrips": self.total_trips,
            "lastFailureKind": self.last_failure_kind,
            "lastFailureReason": self.last_failure_reason,
        }


class ProviderCircuitBreaker:
    """Per-provider consecutive-failure breaker with a half-open probe.

    Lifecycle for one provider::

        closed --(N tripping failures)--> open
        open --(cooldown elapsed, one caller admitted)--> half_open
        half_open --(success)--> closed
        half_open --(failure)--> open   # with a longer cooldown

    All methods are safe to call from multiple threads and from concurrent
    turns; a single lock guards the whole table.
    """

    def __init__(
        self,
        settings: BreakerSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings or BreakerSettings()
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    @property
    def settings(self) -> BreakerSettings:
        return self._settings

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    # ── decisions ────────────────────────────────────────────────────

    def allow(self, provider: str) -> bool:
        """True when a request may be sent to ``provider`` right now.

        Admitting a half-open probe is a *state change*: exactly one caller
        gets through per cooldown window, so callers must not use this as a
        side-effect-free predicate. Use :meth:`status` for display.
        """
        if not self._settings.enabled or not provider:
            return True
        with self._lock:
            entry = self._entries.get(provider)
            if entry is None or entry.state is BreakerState.CLOSED:
                return True
            now = self._clock()
            window = self._cooldown_for(entry)
            if entry.state is BreakerState.OPEN:
                if entry.opened_at is None or now - entry.opened_at >= window:
                    entry.state = BreakerState.HALF_OPEN
                    entry.probe_started_at = now
                    return True
                return False
            # HALF_OPEN: one probe is already in flight. Re-admit only after a
            # full window, so a turn that is abandoned mid-stream (no success
            # and no failure recorded) cannot wedge the breaker forever.
            if entry.probe_started_at is None or now - entry.probe_started_at >= window:
                entry.probe_started_at = now
                return True
            return False

    def record_success(self, provider: str) -> None:
        """Close ``provider``'s breaker and clear its failure history."""
        if not provider:
            return
        with self._lock:
            entry = self._entries.get(provider)
            if entry is None:
                return
            entry.consecutive_failures = 0
            entry.state = BreakerState.CLOSED
            entry.opened_at = None
            entry.probe_started_at = None
            entry.open_cycles = 0
            entry.last_failure_kind = ""
            entry.last_failure_reason = ""

    def record_failure(
        self,
        provider: str,
        kind: ProviderFailureKind | None = None,
        reason: str = "",
    ) -> BreakerState:
        """Count a failure against ``provider`` and return the resulting state.

        Failures whose ``kind`` is not in :data:`TRIPPING_FAILURE_KINDS` are
        ignored — they say something about the request, not the provider.
        Passing ``kind=None`` means "unclassified provider fault" and counts.
        """
        if not self._settings.enabled or not provider:
            return BreakerState.CLOSED
        if kind is not None and not trips_breaker(kind):
            with self._lock:
                entry = self._entries.get(provider)
                return entry.state if entry else BreakerState.CLOSED
        with self._lock:
            entry = self._entries.setdefault(provider, _Entry())
            now = self._clock()
            entry.total_failures += 1
            entry.consecutive_failures += 1
            entry.last_failure_kind = str(kind) if kind is not None else ""
            entry.last_failure_reason = (reason or "")[:_REASON_MAX_CHARS]
            if entry.state is BreakerState.HALF_OPEN:
                # The probe failed: reopen immediately with a longer window.
                self._open(entry, now)
                return entry.state
            if entry.consecutive_failures >= self._settings.failure_threshold:
                self._open(entry, now)
            return entry.state

    # ── introspection ────────────────────────────────────────────────

    def state(self, provider: str) -> BreakerState:
        with self._lock:
            entry = self._entries.get(provider)
            return entry.state if entry else BreakerState.CLOSED

    def status(self, provider: str) -> ProviderBreakerStatus:
        """Side-effect-free snapshot for one provider (closed when untracked)."""
        with self._lock:
            entry = self._entries.get(provider)
            if entry is None:
                return ProviderBreakerStatus(
                    provider=provider,
                    state=BreakerState.CLOSED,
                    consecutive_failures=0,
                    failure_threshold=self._settings.failure_threshold,
                    cooldown_remaining_seconds=0.0,
                    cooldown_seconds=self._settings.cooldown_seconds,
                    total_failures=0,
                    total_trips=0,
                )
            return self._status_locked(provider, entry)

    def snapshot(self) -> list[ProviderBreakerStatus]:
        """Snapshot of every tracked provider, ordered by provider id."""
        with self._lock:
            return [
                self._status_locked(provider, entry)
                for provider, entry in sorted(self._entries.items())
            ]

    def reset(self, provider: str | None = None) -> None:
        """Clear breaker state for one provider, or all of them."""
        with self._lock:
            if provider is None:
                self._entries.clear()
            else:
                self._entries.pop(provider, None)

    # ── internals ────────────────────────────────────────────────────

    def _open(self, entry: _Entry, now: float) -> None:
        entry.state = BreakerState.OPEN
        entry.opened_at = now
        entry.probe_started_at = None
        entry.open_cycles += 1
        entry.total_trips += 1

    def _cooldown_for(self, entry: _Entry) -> float:
        """Exponential backoff across consecutive open cycles, capped."""
        exponent = max(0, entry.open_cycles - 1)
        # Cap the exponent before shifting so a very long outage cannot build
        # an astronomically large intermediate float.
        exponent = min(exponent, 32)
        window = self._settings.cooldown_seconds * float(2**exponent)
        return min(window, self._settings.max_cooldown_seconds)

    def _status_locked(self, provider: str, entry: _Entry) -> ProviderBreakerStatus:
        window = self._cooldown_for(entry)
        remaining = 0.0
        if entry.state is BreakerState.OPEN and entry.opened_at is not None:
            remaining = max(0.0, window - (self._clock() - entry.opened_at))
        return ProviderBreakerStatus(
            provider=provider,
            state=entry.state,
            consecutive_failures=entry.consecutive_failures,
            failure_threshold=self._settings.failure_threshold,
            cooldown_remaining_seconds=remaining,
            cooldown_seconds=window,
            total_failures=entry.total_failures,
            total_trips=entry.total_trips,
            last_failure_kind=entry.last_failure_kind,
            last_failure_reason=entry.last_failure_reason,
        )


def snapshot_payload(breaker: ProviderCircuitBreaker | None) -> list[dict[str, Any]]:
    """JSON-ready breaker snapshot; ``[]`` when there is no breaker."""
    if breaker is None:
        return []
    return [status.to_dict() for status in breaker.snapshot()]


__all__ = [
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_MAX_COOLDOWN_SECONDS",
    "TRIPPING_FAILURE_KINDS",
    "BreakerSettings",
    "BreakerState",
    "ProviderBreakerStatus",
    "ProviderCircuitBreaker",
    "snapshot_payload",
    "trips_breaker",
]
