"""Tests for BoundedSessionRegistry primitive and session teardown hooks."""

from __future__ import annotations

import asyncio
import gc
import time
from typing import Any

import pytest

from agentos.util.bounded_registry import (
    BoundedSessionRegistry,
    _discard_from_all,
    _register_session_scoped,
    _session_scoped_registries,
)

MAX_SAMPLES = [1, 2, 5, 10, 50]


class TestGenericProperties:
    @staticmethod
    def assert_registry_bound(
        reg: BoundedSessionRegistry[Any, Any], max_entries: int, n: int
    ) -> None:
        """Property: N inserts under max M leaves at most M entries."""
        for i in range(n):
            reg[f"k{i}"] = i
        assert len(reg) <= max_entries
        # Verify LRU: oldest keys were evicted first
        if n > max_entries:
            for i in range(n - max_entries):
                assert f"k{i}" not in reg, f"Oldest key k{i} should have been evicted"
            for i in range(n - max_entries, n):
                assert f"k{i}" in reg, f"Recent key k{i} should survive"

    @staticmethod
    def assert_discard_removes(reg: BoundedSessionRegistry[Any, Any]) -> None:
        """Insert a key, discard it, verify it's gone."""
        reg["sess:abc"] = 42
        assert "sess:abc" in reg
        reg.discard("sess:abc")
        assert "sess:abc" not in reg
        # Discard of missing key is a no-op
        reg.discard("nonexistent")


@pytest.mark.parametrize("max_entries", MAX_SAMPLES)
def test_bound_holds(max_entries: int) -> None:
    """N inserts under max M leaves at most M entries."""
    reg: BoundedSessionRegistry[str, int] = BoundedSessionRegistry(max_entries=max_entries)
    TestGenericProperties.assert_registry_bound(reg, max_entries, max_entries * 4)


@pytest.mark.parametrize("max_entries", [1, 5, 50])
def test_discard_after_insert(max_entries: int) -> None:
    """Explicit discard drops the entry immediately."""
    reg: BoundedSessionRegistry[str, int] = BoundedSessionRegistry(max_entries=max_entries)
    TestGenericProperties.assert_discard_removes(reg)


# ---------------------------------------------------------------------------
# TTL tests
# ---------------------------------------------------------------------------


class TestTTL:
    def test_ttl_eviction(self) -> None:
        reg: BoundedSessionRegistry[str, int] = BoundedSessionRegistry(
            max_entries=100, ttl_seconds=0.1
        )
        reg["sess:a"] = 1
        reg["sess:b"] = 2
        assert len(reg) == 2
        time.sleep(0.15)
        # Access triggers eviction
        _ = reg.get("sess:c", None)
        assert "sess:a" not in reg
        assert "sess:b" not in reg

    def test_ttl_no_eviction_within_window(self) -> None:
        reg: BoundedSessionRegistry[str, int] = BoundedSessionRegistry(
            max_entries=100, ttl_seconds=60
        )
        reg["sess:a"] = 1
        assert reg["sess:a"] == 1

    def test_ttl_zero_is_noop(self) -> None:
        reg: BoundedSessionRegistry[str, int] = BoundedSessionRegistry(
            max_entries=100, ttl_seconds=0
        )
        reg["sess:a"] = 1
        assert reg["sess:a"] == 1

    def test_ttl_with_access_eviction(self) -> None:
        reg: BoundedSessionRegistry[str, int] = BoundedSessionRegistry(
            max_entries=10, ttl_seconds=0.1, evict_on_access=True
        )
        reg["k1"] = 1
        time.sleep(0.15)
        # contains triggers eviction
        assert "k1" not in reg

    def test_ttl_evict_on_write_only(self) -> None:
        reg: BoundedSessionRegistry[str, int] = BoundedSessionRegistry(
            max_entries=10, ttl_seconds=0.1, evict_on_access=False
        )
        reg["k1"] = 1
        time.sleep(0.15)
        # No access eviction — get still works
        assert reg["k1"] == 1
        # Write triggers eviction
        reg["k2"] = 2
        assert "k1" not in reg


# ---------------------------------------------------------------------------
# Cap / LRU tests
# ---------------------------------------------------------------------------


class TestCapLRU:
    def test_at_max_no_eviction(self) -> None:
        reg: BoundedSessionRegistry[str, int] = BoundedSessionRegistry(max_entries=5)
        for i in range(5):
            reg[f"k{i}"] = i
        assert len(reg) == 5

    def test_one_over_triggers_eviction(self) -> None:
        reg: BoundedSessionRegistry[str, int] = BoundedSessionRegistry(max_entries=5)
        for i in range(6):
            reg[f"k{i}"] = i
        assert len(reg) == 5
        assert "k0" not in reg

    def test_evicts_oldest_first(self) -> None:
        reg: BoundedSessionRegistry[str, int] = BoundedSessionRegistry(max_entries=3)
        reg["a"] = 1
        reg["b"] = 2
        reg["c"] = 3
        reg["d"] = 4  # evicts 'a'
        assert "a" not in reg
        assert "b" in reg
        assert "c" in reg
        assert "d" in reg
        # access 'b' moves it to end; next insert evicts 'c'
        _ = reg["b"]
        reg["e"] = 5
        assert "c" not in reg
        assert "b" in reg

    def test_cap_zero_no_eviction(self) -> None:
        reg: BoundedSessionRegistry[str, int] = BoundedSessionRegistry(max_entries=0)
        for i in range(100):
            reg[f"k{i}"] = i
        assert len(reg) == 100


# ---------------------------------------------------------------------------
# Explicit discard (session terminal hook & composite keys)
# ---------------------------------------------------------------------------


class TestDiscard:
    def test_discard_existing(self) -> None:
        reg: BoundedSessionRegistry[str, str] = BoundedSessionRegistry(max_entries=10)
        reg["sess:1"] = "a"
        reg["sess:2"] = "b"
        reg.discard("sess:1")
        assert "sess:1" not in reg
        assert reg.get("sess:2") == "b"

    def test_discard_nonexistent_is_noop(self) -> None:
        reg: BoundedSessionRegistry[str, str] = BoundedSessionRegistry(max_entries=10)
        reg.discard("ghost")  # no crash

    def test_discard_then_reinsert(self) -> None:
        reg: BoundedSessionRegistry[str, int] = BoundedSessionRegistry(max_entries=5)
        reg["s"] = 1
        reg.discard("s")
        reg["s"] = 2
        assert reg["s"] == 2

    def test_discard_composite_tuple_keys(self) -> None:
        """Composite tuple keys containing the session_key are dropped on discard."""
        reg: BoundedSessionRegistry[tuple[str, ...], str] = BoundedSessionRegistry(max_entries=50)
        # TurnRunner memory snapshot style: (agent_id, session_key)
        reg[("agent_1", "sess_abc")] = "snap1"
        reg[("agent_2", "sess_abc")] = "snap2"
        reg[("agent_1", "sess_xyz")] = "snap3"
        # TurnRunner bootstrap snapshot style: (agent_id, session_key, mode)
        reg[("agent_1", "sess_abc", "mode_full")] = "boot1"
        reg[("agent_1", "sess_xyz", "mode_full")] = "boot2"

        # Discard sess_abc drops all tuples containing sess_abc
        reg.discard("sess_abc")

        assert ("agent_1", "sess_abc") not in reg
        assert ("agent_2", "sess_abc") not in reg
        assert ("agent_1", "sess_abc", "mode_full") not in reg
        # sess_xyz entries survive
        assert ("agent_1", "sess_xyz") in reg
        assert ("agent_1", "sess_xyz", "mode_full") in reg


# ---------------------------------------------------------------------------
# Session terminal event routing & WeakSet GC
# ---------------------------------------------------------------------------


class TestSessionTeardownRouting:
    def test_discard_from_all(self) -> None:
        reg1: BoundedSessionRegistry[str, int] = BoundedSessionRegistry(
            max_entries=10, session_scoped=True
        )
        reg2: BoundedSessionRegistry[tuple[str, str], str] = BoundedSessionRegistry(
            max_entries=10, session_scoped=True
        )
        non_scoped: BoundedSessionRegistry[str, int] = BoundedSessionRegistry(max_entries=10)

        reg1["sess_target"] = 100
        reg1["sess_other"] = 200
        reg2[("agent_1", "sess_target")] = "data"
        reg2[("agent_1", "sess_other")] = "keep"
        non_scoped["sess_target"] = 999

        _discard_from_all("sess_target")

        assert "sess_target" not in reg1
        assert "sess_other" in reg1
        assert ("agent_1", "sess_target") not in reg2
        assert ("agent_1", "sess_other") in reg2
        # Non-scoped is not touched by _discard_from_all
        assert "sess_target" in non_scoped

    def test_explicit_register_session_scoped(self) -> None:
        reg: BoundedSessionRegistry[str, int] = BoundedSessionRegistry(max_entries=10)
        _register_session_scoped(reg)
        reg["sess_exp"] = 42
        _discard_from_all("sess_exp")
        assert "sess_exp" not in reg

    def test_weakset_no_memory_leak(self) -> None:
        """Registries with session_scoped=True do not leak in _session_scoped_registries."""
        gc.collect()
        initial_count = len(_session_scoped_registries)

        def make_temp_registry() -> None:
            temp_reg = BoundedSessionRegistry[str, int](max_entries=10, session_scoped=True)
            assert temp_reg in _session_scoped_registries

        make_temp_registry()
        gc.collect()

        assert len(_session_scoped_registries) == initial_count


# ---------------------------------------------------------------------------
# Dict interface tests
# ---------------------------------------------------------------------------


class TestDictInterface:
    def test_dict_methods(self) -> None:
        reg: BoundedSessionRegistry[str, int] = BoundedSessionRegistry(max_entries=10)
        reg["a"] = 1
        reg["b"] = 2
        assert list(reg) == ["a", "b"]
        assert list(reg.keys()) == ["a", "b"]
        assert list(reg.values()) == [1, 2]
        assert list(reg.items()) == [("a", 1), ("b", 2)]

        reg.update({"c": 3, "d": 4})
        assert len(reg) == 4
        assert reg.get("c") == 3
        assert reg.get("missing", 99) == 99

        assert reg.pop("d") == 4
        assert "d" not in reg
        assert reg.pop("missing", 123) == 123

        val = reg.setdefault("e", 5)
        assert val == 5
        assert reg["e"] == 5
        val2 = reg.setdefault("e", 10)
        assert val2 == 5

        snap = reg.snapshot()
        assert type(snap) is dict
        assert snap["a"] == 1

        del reg["a"]
        assert "a" not in reg

        cleared = reg.clear()
        assert cleared == 3
        assert len(reg) == 0

    def test_stats_dict(self) -> None:
        reg: BoundedSessionRegistry[str, int] = BoundedSessionRegistry(
            max_entries=10, ttl_seconds=5
        )
        s = reg.stats()
        assert s["max_entries"] == 10
        assert s["ttl_seconds"] == 5.0
        assert s["current_entries"] == 0
        assert "eviction_count" in s


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


class TestObservability:
    def test_eviction_count_ttl(self) -> None:
        reg: BoundedSessionRegistry[str, int] = BoundedSessionRegistry(
            max_entries=10, ttl_seconds=0.1
        )
        reg["a"] = 1
        time.sleep(0.15)
        reg["b"] = 2  # triggers evict
        assert reg.eviction_count >= 1

    def test_eviction_count_cap(self) -> None:
        reg: BoundedSessionRegistry[str, int] = BoundedSessionRegistry(max_entries=3)
        for i in range(10):
            reg[f"k{i}"] = i
        assert reg.eviction_count == 7


# ---------------------------------------------------------------------------
# Concurrent safety
# ---------------------------------------------------------------------------


class TestConcurrent:
    @pytest.mark.asyncio
    async def test_concurrent_50_ops(self) -> None:
        reg: BoundedSessionRegistry[str, int] = BoundedSessionRegistry(
            max_entries=20, ttl_seconds=0
        )

        async def worker(start: int) -> None:
            for i in range(start, start + 10):
                reg[f"k{i}"] = i
                _ = reg.get(f"k{i}")
                if i % 3 == 0:
                    reg.discard(f"k{i}")

        tasks = [worker(i * 10) for i in range(5)]
        await asyncio.gather(*tasks)
        assert len(reg) <= 20
        assert reg.eviction_count >= 0
