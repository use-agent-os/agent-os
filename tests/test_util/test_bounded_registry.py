"""The one shared contract every bounded registry adopter relies on."""

from __future__ import annotations

import threading

import pytest

from agentos.util.bounded_registry import (
    BoundedRegistry,
    configure_registry_limits,
    drop_session_state,
    registry_limits,
    registry_stats,
    reset_registry_limits,
)


@pytest.fixture(autouse=True)
def _reset_limits():
    reset_registry_limits()
    yield
    reset_registry_limits()


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ── the property the issue asks every adopter to reuse ───────────────


@pytest.mark.parametrize("inserts,ceiling", [(100, 10), (1000, 8), (5, 5), (17, 1)])
def test_n_inserts_under_a_max_of_m_leaves_at_most_m(inserts: int, ceiling: int) -> None:
    registry: BoundedRegistry[str, int] = BoundedRegistry(max_entries=ceiling, register=False)

    for i in range(inserts):
        registry[f"session-{i}"] = i

    assert len(registry) == min(inserts, ceiling)
    assert registry.evictions == max(0, inserts - ceiling)


def test_explicit_discard_drops_immediately() -> None:
    registry: BoundedRegistry[str, int] = BoundedRegistry(max_entries=100, register=False)
    registry["s1"] = 1

    assert registry.discard("s1") is True
    assert "s1" not in registry
    assert len(registry) == 0
    assert registry.discard("s1") is False


# ── LRU semantics ────────────────────────────────────────────────────


def test_eviction_is_least_recently_used_not_first_inserted() -> None:
    registry: BoundedRegistry[str, int] = BoundedRegistry(max_entries=3, register=False)
    registry["a"] = 1
    registry["b"] = 2
    registry["c"] = 3

    registry.get("a")  # a is now the most recent
    registry["d"] = 4  # evicts b

    assert sorted(registry.keys()) == ["a", "c", "d"]


def test_rewriting_a_key_refreshes_it_without_growing() -> None:
    registry: BoundedRegistry[str, int] = BoundedRegistry(max_entries=2, register=False)
    registry["a"] = 1
    registry["b"] = 2
    registry["a"] = 3

    registry["c"] = 4

    assert sorted(registry.keys()) == ["a", "c"]
    assert registry.get("a") == 3


def test_setdefault_keeps_the_live_value() -> None:
    registry: BoundedRegistry[str, list[int]] = BoundedRegistry(max_entries=4, register=False)

    first = registry.setdefault("a", [])
    first.append(1)
    second = registry.setdefault("a", [])

    assert second is first
    assert second == [1]


def test_pop_returns_default_and_raises_without_one() -> None:
    registry: BoundedRegistry[str, int] = BoundedRegistry(max_entries=4, register=False)
    registry["a"] = 1

    assert registry.pop("a") == 1
    assert registry.pop("a", None) is None
    with pytest.raises(KeyError):
        registry.pop("a")


# ── TTL ──────────────────────────────────────────────────────────────


def test_ttl_expires_entries_without_touching_fresh_ones() -> None:
    clock = _Clock()
    registry: BoundedRegistry[str, int] = BoundedRegistry(
        max_entries=100, ttl_seconds=60, time_source=clock, register=False
    )
    registry["old"] = 1
    clock.advance(61)
    registry["new"] = 2

    assert registry.keys() == ["new"]
    assert registry.get("old") is None
    assert registry.expirations == 1


def test_ttl_is_measured_from_the_write_not_the_read() -> None:
    clock = _Clock()
    registry: BoundedRegistry[str, int] = BoundedRegistry(
        max_entries=100, ttl_seconds=60, time_source=clock, register=False
    )
    registry["a"] = 1
    clock.advance(30)
    assert registry.get("a") == 1
    clock.advance(31)

    assert registry.get("a") is None


def test_ttl_sweep_skips_non_evictable_entries() -> None:
    """A site-vetoed entry survives the TTL sweep, like every other path.

    Regression for the #1131 review: ``_expire`` was the one eviction path
    that did not consult ``_is_evictable``, so a running background process in
    a cache-shaped registry was dropped after the TTL while still alive.
    """
    clock = _Clock()
    registry: BoundedRegistry[str, str] = BoundedRegistry(
        max_entries=100,
        ttl_seconds=60,
        time_source=clock,
        register=False,
        evictable=lambda value: value != "busy",
    )
    registry["running"] = "busy"
    registry["idle"] = "free"
    clock.advance(61)

    # The sweep runs on the next read: the idle entry expires, the busy one stays.
    assert registry.keys() == ["running"]
    assert registry.get("running") == "busy"
    assert registry.expirations == 1


def test_ttl_sweep_still_expires_a_vetoed_entry_once_released() -> None:
    clock = _Clock()
    state = {"busy": True}
    registry: BoundedRegistry[str, str] = BoundedRegistry(
        max_entries=100,
        ttl_seconds=60,
        time_source=clock,
        register=False,
        evictable=lambda value: not state["busy"],
    )
    registry["proc"] = "value"
    clock.advance(61)
    assert registry.get("proc") == "value"  # vetoed, still pinned

    state["busy"] = False
    clock.advance(1)
    assert registry.get("proc") is None


def test_a_session_shaped_registry_has_no_ttl() -> None:
    registry: BoundedRegistry[str, int] = BoundedRegistry(shape="session", register=False)

    assert registry.ttl_seconds is None


# ── configured ceilings ──────────────────────────────────────────────


def test_ceilings_follow_configuration_even_for_existing_registries() -> None:
    registry: BoundedRegistry[str, int] = BoundedRegistry(shape="session", register=False)
    assert registry.max_entries == registry_limits().session_max_entries

    configure_registry_limits(session_max_entries=3)
    for i in range(10):
        registry[f"s{i}"] = i

    assert registry.max_entries == 3
    assert len(registry) == 3


def test_cache_shape_reads_the_cache_ceiling_and_ttl() -> None:
    configure_registry_limits(cache_max_entries=7, cache_ttl_seconds=42)
    registry: BoundedRegistry[str, int] = BoundedRegistry(shape="cache", register=False)

    assert registry.max_entries == 7
    assert registry.ttl_seconds == 42


def test_an_explicit_limit_wins_over_configuration() -> None:
    registry: BoundedRegistry[str, int] = BoundedRegistry(max_entries=2, register=False)
    configure_registry_limits(session_max_entries=1000)

    assert registry.max_entries == 2


@pytest.mark.parametrize("bad", [0, -1])
def test_a_nonpositive_ceiling_is_ignored_rather_than_disabling_the_bound(bad: int) -> None:
    before = registry_limits().session_max_entries

    configure_registry_limits(session_max_entries=bad)

    assert registry_limits().session_max_entries == before


# ── session teardown ─────────────────────────────────────────────────


def test_drop_session_state_reaches_every_registered_registry() -> None:
    plain: BoundedRegistry[str, int] = BoundedRegistry(
        name="plain", session_of=lambda key, _value: key, register=True
    )
    tupled: BoundedRegistry[tuple[str, str], int] = BoundedRegistry(
        name="tupled", session_of=lambda key, _value: key[0], register=True
    )
    unscoped: BoundedRegistry[str, int] = BoundedRegistry(name="unscoped", register=True)

    plain["s1"] = 1
    plain["s2"] = 2
    tupled[("s1", "a")] = 1
    tupled[("s1", "b")] = 2
    tupled[("s2", "a")] = 3
    unscoped["s1"] = 1

    removed = drop_session_state("s1")

    assert removed == 3
    assert plain.keys() == ["s2"]
    assert tupled.keys() == [("s2", "a")]
    assert unscoped.keys() == ["s1"], "a registry without session_of is left alone"


@pytest.mark.parametrize("session_key", ["", "   ", None])
def test_drop_session_state_ignores_an_empty_key(session_key: str | None) -> None:
    registry: BoundedRegistry[str, int] = BoundedRegistry(session_of=lambda key, _value: key)
    registry[""] = 1

    assert drop_session_state(session_key) == 0  # type: ignore[arg-type]
    assert len(registry) == 1


def test_discard_session_on_an_unscoped_registry_removes_nothing() -> None:
    registry: BoundedRegistry[str, int] = BoundedRegistry(register=False)
    registry["s1"] = 1

    assert registry.discard_session("s1") == 0
    assert len(registry) == 1


# ── observability ────────────────────────────────────────────────────


def test_stats_report_occupancy_and_evictions() -> None:
    registry: BoundedRegistry[str, int] = BoundedRegistry(name="demo", max_entries=2, register=True)
    for i in range(5):
        registry[f"s{i}"] = i

    stats = registry.stats()
    assert stats["name"] == "demo"
    assert stats["entries"] == 2
    assert stats["maxEntries"] == 2
    assert stats["evictions"] == 3
    assert any(row["name"] == "demo" for row in registry_stats())


# ── thread safety ────────────────────────────────────────────────────


def test_concurrent_writers_never_exceed_the_ceiling() -> None:
    registry: BoundedRegistry[str, int] = BoundedRegistry(max_entries=16, register=False)
    errors: list[BaseException] = []

    def worker(offset: int) -> None:
        try:
            for i in range(200):
                registry[f"s{offset}-{i}"] = i
                registry.get(f"s{offset}-{i}")
                registry.discard(f"s{offset}-{i - 5}")
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(registry) <= 16


# ── busy entries are never evicted ───────────────────────────────────


class _Lockish:
    def __init__(self, held: bool = False) -> None:
        self.held = held

    def locked(self) -> bool:
        return self.held


def test_a_busy_entry_survives_the_ceiling_sweep() -> None:
    registry: BoundedRegistry[str, _Lockish] = BoundedRegistry(
        max_entries=2, evictable=lambda lock: not lock.locked(), register=False
    )
    held = _Lockish(held=True)
    registry["busy"] = held
    registry["a"] = _Lockish()
    registry["b"] = _Lockish()
    registry["c"] = _Lockish()

    assert "busy" in registry
    assert len(registry) == 2


def test_an_all_busy_registry_stays_over_its_ceiling_rather_than_lying() -> None:
    registry: BoundedRegistry[str, _Lockish] = BoundedRegistry(
        max_entries=1, evictable=lambda lock: not lock.locked(), register=False
    )
    for i in range(4):
        registry[f"s{i}"] = _Lockish(held=True)

    assert len(registry) == 4
    assert registry.evictions == 0

    for key in registry.keys():
        registry.get(key).held = False  # type: ignore[union-attr]
    registry["s9"] = _Lockish()

    assert len(registry) == 1


def test_session_teardown_leaves_a_busy_entry_alone() -> None:
    registry: BoundedRegistry[str, _Lockish] = BoundedRegistry(
        session_of=lambda key, _value: key, evictable=lambda lock: not lock.locked(), register=False
    )
    registry["s1"] = _Lockish(held=True)

    assert registry.discard_session("s1") == 0
    assert "s1" in registry


def test_explicit_discard_ignores_the_busy_veto() -> None:
    registry: BoundedRegistry[str, _Lockish] = BoundedRegistry(
        evictable=lambda lock: not lock.locked(), register=False
    )
    registry["s1"] = _Lockish(held=True)

    assert registry.discard("s1") is True


def test_a_raising_evictable_predicate_does_not_pin_memory() -> None:
    def boom(_value: int) -> bool:
        raise RuntimeError("nope")

    registry: BoundedRegistry[str, int] = BoundedRegistry(
        max_entries=2, evictable=boom, register=False
    )
    for i in range(10):
        registry[f"s{i}"] = i

    assert len(registry) == 2


def test_session_of_can_read_the_session_id_out_of_the_value() -> None:
    """Some registries key by their own id and carry the session in the value."""

    class _Job:
        def __init__(self, owner: str) -> None:
            self.owner = owner

    registry: BoundedRegistry[str, _Job] = BoundedRegistry(
        session_of=lambda _key, job: job.owner, register=False
    )
    registry["job-1"] = _Job("s1")
    registry["job-2"] = _Job("s2")

    assert registry.discard_session("s1") == 1
    assert registry.keys() == ["job-2"]


def test_dict_round_trip_matches_the_mapping_surface_callers_already_use() -> None:
    registry: BoundedRegistry[str, int] = BoundedRegistry(max_entries=10, register=False)
    registry.update({"a": 1, "b": 2})

    snapshot = dict(registry)
    registry.clear()
    registry.update(snapshot)

    assert dict(registry) == {"a": 1, "b": 2}
    assert registry["a"] == 1
    with pytest.raises(KeyError):
        registry["missing"]


def test_the_teardown_table_does_not_itself_retain_dead_registries() -> None:
    """The table that exists to stop leaks must not become one."""
    import gc

    before = len(registry_stats())
    for _ in range(50):
        BoundedRegistry(name="ephemeral", session_of=lambda key, _value: key)
    gc.collect()

    assert len(registry_stats()) <= before + 1
