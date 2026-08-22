"""Provider circuit breaker: cooldown, half-open probe, and selector skipping."""

from __future__ import annotations

import pytest

from agentos.provider.circuit_breaker import (
    BreakerSettings,
    BreakerState,
    ProviderCircuitBreaker,
    snapshot_payload,
    trips_breaker,
)
from agentos.provider.failures import ProviderFailureKind
from agentos.provider.selector import ModelSelector, ProviderConfig, SelectorConfig


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _breaker(
    clock: FakeClock,
    *,
    threshold: int = 3,
    cooldown: float = 60.0,
    max_cooldown: float = 600.0,
    enabled: bool = True,
) -> ProviderCircuitBreaker:
    return ProviderCircuitBreaker(
        BreakerSettings(
            enabled=enabled,
            failure_threshold=threshold,
            cooldown_seconds=cooldown,
            max_cooldown_seconds=max_cooldown,
        ),
        clock=clock,
    )


def _fail(breaker: ProviderCircuitBreaker, provider: str, times: int) -> BreakerState:
    state = BreakerState.CLOSED
    for _ in range(times):
        state = breaker.record_failure(
            provider, ProviderFailureKind.PROVIDER_OVERLOADED, "upstream 503"
        )
    return state


# ── failure counting / opening ───────────────────────────────────────


def test_breaker_opens_only_at_threshold() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, threshold=3)

    assert _fail(breaker, "openrouter", 2) is BreakerState.CLOSED
    assert breaker.allow("openrouter") is True

    assert _fail(breaker, "openrouter", 1) is BreakerState.OPEN
    assert breaker.allow("openrouter") is False


def test_success_resets_the_failure_run() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, threshold=3)

    _fail(breaker, "openrouter", 2)
    breaker.record_success("openrouter")
    assert breaker.status("openrouter").consecutive_failures == 0

    # Two more failures must not open: the run restarted at zero.
    assert _fail(breaker, "openrouter", 2) is BreakerState.CLOSED


def test_request_shaped_failures_do_not_trip_the_breaker() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, threshold=2)

    for kind in (
        ProviderFailureKind.MODEL_NOT_FOUND,
        ProviderFailureKind.CONTEXT_OVERFLOW,
        ProviderFailureKind.BAD_REQUEST,
        ProviderFailureKind.AUTH_INVALID,
        ProviderFailureKind.UNSUPPORTED_FEATURE,
    ):
        assert trips_breaker(kind) is False
        breaker.record_failure("openrouter", kind, "nope")

    assert breaker.state("openrouter") is BreakerState.CLOSED
    assert breaker.allow("openrouter") is True


@pytest.mark.parametrize(
    "kind",
    [
        ProviderFailureKind.PROVIDER_OVERLOADED,
        ProviderFailureKind.TRANSPORT_TRANSIENT,
        ProviderFailureKind.RATE_LIMITED,
    ],
)
def test_provider_health_failures_trip_the_breaker(kind: ProviderFailureKind) -> None:
    clock = FakeClock()
    breaker = _breaker(clock, threshold=1)
    assert trips_breaker(kind) is True
    assert breaker.record_failure("openrouter", kind, "boom") is BreakerState.OPEN


def test_unclassified_failure_counts() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, threshold=2)
    breaker.record_failure("openrouter")
    assert breaker.record_failure("openrouter") is BreakerState.OPEN


def test_breaker_state_is_per_provider() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, threshold=1)
    _fail(breaker, "openrouter", 1)

    assert breaker.allow("openrouter") is False
    assert breaker.allow("anthropic") is True


# ── cooldown / half-open probe ───────────────────────────────────────


def test_cooldown_admits_exactly_one_half_open_probe() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, threshold=1, cooldown=60.0)
    _fail(breaker, "openrouter", 1)

    clock.advance(59.0)
    assert breaker.allow("openrouter") is False

    clock.advance(1.0)
    assert breaker.allow("openrouter") is True
    assert breaker.state("openrouter") is BreakerState.HALF_OPEN
    # A second concurrent turn must not also probe.
    assert breaker.allow("openrouter") is False


def test_successful_probe_closes_the_breaker() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, threshold=1, cooldown=60.0)
    _fail(breaker, "openrouter", 1)

    clock.advance(60.0)
    assert breaker.allow("openrouter") is True
    breaker.record_success("openrouter")

    assert breaker.state("openrouter") is BreakerState.CLOSED
    assert breaker.allow("openrouter") is True
    assert breaker.status("openrouter").cooldown_remaining_seconds == 0.0


def test_failed_probe_reopens_with_a_longer_cooldown() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, threshold=1, cooldown=60.0, max_cooldown=600.0)
    _fail(breaker, "openrouter", 1)

    clock.advance(60.0)
    assert breaker.allow("openrouter") is True  # probe admitted
    assert _fail(breaker, "openrouter", 1) is BreakerState.OPEN

    # Backoff doubled: the old 60s window is no longer enough.
    clock.advance(60.0)
    assert breaker.allow("openrouter") is False
    clock.advance(60.0)
    assert breaker.allow("openrouter") is True


def test_cooldown_backoff_is_capped() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, threshold=1, cooldown=60.0, max_cooldown=120.0)
    for _ in range(6):
        _fail(breaker, "openrouter", 1)
        clock.advance(10_000.0)
        breaker.allow("openrouter")

    assert breaker.status("openrouter").cooldown_seconds == 120.0


def test_abandoned_probe_does_not_wedge_the_breaker() -> None:
    """A turn that neither succeeds nor fails must not block future probes."""
    clock = FakeClock()
    breaker = _breaker(clock, threshold=1, cooldown=60.0)
    _fail(breaker, "openrouter", 1)

    clock.advance(60.0)
    assert breaker.allow("openrouter") is True  # probe admitted, never resolved
    assert breaker.allow("openrouter") is False

    clock.advance(60.0)
    assert breaker.allow("openrouter") is True


def test_disabled_breaker_always_admits() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, threshold=1, enabled=False)

    assert breaker.record_failure("openrouter", ProviderFailureKind.RATE_LIMITED) is (
        BreakerState.CLOSED
    )
    assert breaker.allow("openrouter") is True
    assert breaker.snapshot() == []


def test_settings_clamp_nonsense_values() -> None:
    settings = BreakerSettings(
        failure_threshold=0, cooldown_seconds=-5.0, max_cooldown_seconds=0.0
    )
    assert settings.failure_threshold == 1
    assert settings.cooldown_seconds == 1.0
    assert settings.max_cooldown_seconds == 1.0


def test_reset_clears_state() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, threshold=1)
    _fail(breaker, "openrouter", 1)
    _fail(breaker, "anthropic", 1)

    breaker.reset("openrouter")
    assert breaker.allow("openrouter") is True
    assert breaker.allow("anthropic") is False

    breaker.reset()
    assert breaker.snapshot() == []


# ── snapshots ────────────────────────────────────────────────────────


def test_status_is_side_effect_free_while_open() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, threshold=1, cooldown=60.0)
    _fail(breaker, "openrouter", 1)

    clock.advance(60.0)
    assert breaker.status("openrouter").state is BreakerState.OPEN
    # Reading status must not consume the probe slot.
    assert breaker.allow("openrouter") is True


def test_snapshot_payload_shape() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, threshold=2, cooldown=60.0)
    _fail(breaker, "openrouter", 2)
    clock.advance(15.0)

    rows = snapshot_payload(breaker)
    assert [row["provider"] for row in rows] == ["openrouter"]
    row = rows[0]
    assert row["state"] == "open"
    assert row["consecutiveFailures"] == 2
    assert row["failureThreshold"] == 2
    assert row["cooldownRemainingSeconds"] == 45.0
    assert row["lastFailureKind"] == "provider_overloaded"
    assert row["lastFailureReason"] == "upstream 503"
    assert row["totalTrips"] == 1

    assert snapshot_payload(None) == []


def test_untracked_provider_reports_closed() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    status = breaker.status("anthropic")
    assert status.state is BreakerState.CLOSED
    assert status.healthy is True
    assert status.to_dict()["provider"] == "anthropic"


# ── selector integration ─────────────────────────────────────────────


def _selector(breaker: ProviderCircuitBreaker) -> ModelSelector:
    return ModelSelector(
        SelectorConfig(
            primary=ProviderConfig("openrouter", "openai/gpt-5.6-luna", api_key="k"),
            fallbacks=[ProviderConfig("ollama", "llama3")],
        ),
        breaker=breaker,
    )


def test_resolve_skips_a_provider_in_cooldown() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, threshold=2)
    selector = _selector(breaker)

    assert selector.active_provider_id == "openrouter"

    _fail(breaker, "openrouter", 2)
    selector.resolve()
    assert selector.active_provider_id == "ollama"


def test_resolve_stays_on_the_primary_when_the_whole_chain_is_open() -> None:
    """A provider in cooldown still beats having no provider at all."""
    clock = FakeClock()
    breaker = _breaker(clock, threshold=1)
    selector = _selector(breaker)

    _fail(breaker, "openrouter", 1)
    _fail(breaker, "ollama", 1)
    selector.resolve()
    assert selector.active_provider_id == "openrouter"


def test_resolve_returns_to_the_primary_after_the_probe_succeeds() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, threshold=1, cooldown=60.0)
    selector = _selector(breaker)

    _fail(breaker, "openrouter", 1)
    selector.resolve()
    assert selector.active_provider_id == "ollama"

    clock.advance(60.0)
    probe = _selector(breaker)
    probe.resolve()
    assert probe.active_provider_id == "openrouter"
    probe.record_provider_success()

    later = _selector(breaker)
    later.resolve()
    assert later.active_provider_id == "openrouter"


def test_breaker_state_survives_clone() -> None:
    """Per-turn clones share breaker state — that is the whole point."""
    clock = FakeClock()
    breaker = _breaker(clock, threshold=1)
    selector = _selector(breaker)

    turn_one = selector.clone()
    turn_one.record_provider_failure(ProviderFailureKind.PROVIDER_OVERLOADED, "503")

    turn_two = selector.clone()
    turn_two.resolve()
    assert turn_two.active_provider_id == "ollama"


def test_selector_defaults_to_its_own_breaker() -> None:
    selector = ModelSelector(
        SelectorConfig(primary=ProviderConfig("openrouter", "m", api_key="k"))
    )
    assert selector.circuit_breaker is not None
    assert selector.clone().circuit_breaker is selector.circuit_breaker


def test_record_provider_failure_targets_the_active_link() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, threshold=1)
    selector = _selector(breaker)

    selector.next_fallback()
    assert selector.active_provider_id == "ollama"
    selector.record_provider_failure(ProviderFailureKind.TRANSPORT_TRANSIENT, "refused")

    assert breaker.state("ollama") is BreakerState.OPEN
    assert breaker.state("openrouter") is BreakerState.CLOSED


def test_selector_snapshot_helpers() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, threshold=1)
    selector = _selector(breaker)
    selector.record_provider_failure(ProviderFailureKind.RATE_LIMITED, "429")

    assert selector.circuit_breaker_status("openrouter").state is BreakerState.OPEN
    assert [s.provider for s in selector.circuit_breaker_snapshot()] == ["openrouter"]


def test_repeated_resolve_keeps_the_probe_this_selector_was_granted() -> None:
    """A turn resolves twice when a model override is in play (see
    ``PromptAssemblerStage``). The second resolve must not see its own
    half-open probe as "already in flight" and skip to the fallback."""
    clock = FakeClock()
    breaker = _breaker(clock, threshold=1, cooldown=60.0)
    _fail(breaker, "openrouter", 1)
    clock.advance(60.0)

    selector = _selector(breaker)
    selector.resolve()
    assert selector.active_provider_id == "openrouter"  # probe granted

    selector.override_model("openai/other")
    selector.resolve()
    assert selector.active_provider_id == "openrouter"


def test_a_different_turn_does_not_share_the_held_probe() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, threshold=1, cooldown=60.0)
    _fail(breaker, "openrouter", 1)
    clock.advance(60.0)

    prober = _selector(breaker)
    prober.resolve()
    assert prober.active_provider_id == "openrouter"

    concurrent = _selector(breaker)
    concurrent.resolve()
    assert concurrent.active_provider_id == "ollama"


def test_reset_drops_the_held_admission() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, threshold=1)
    selector = _selector(breaker)
    selector.resolve()

    _fail(breaker, "openrouter", 1)
    selector.reset()
    selector.resolve()
    assert selector.active_provider_id == "ollama"
