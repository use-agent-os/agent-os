"""Property-based and unit tests for BoundedSessionRegistry.

Shares one property test across all 20 call sites (gh-1131). Every
registry in the codebase reuses the same bounds contract.
"""

from __future__ import annotations

import time

import pytest

from agentos.util.bounded_registry import BoundedSessionRegistry

# ---------------------------------------------------------------------------
# Shared property: under a cap of *max_entries*, N inserts leave at most
# *max_entries* entries, and discard removes the entry immediately.
# ---------------------------------------------------------------------------

MAX_SAMPLES = [1, 2, 5, 10, 100, 1000]


class TestGenericProperties:
    """Property tests shared by every BoundedSessionRegistry.

    These tests are written generically so they can be reused by any test
    file that adopts a BoundedSessionRegistry — just import and parametrize.
    """

    @staticmethod
    def assert_registry_bound(reg: BoundedSessionRegistry, max_entries: int, n: int) -> None:
        """Insert *n* distinct keys; verify at most *max_entries* survive."""
        for i in range(n):
            reg[f"k{i}"] = i
        assert len(reg) <= max_entries, f"Expected ≤{max_entries}, got {len(reg)}"
        # Verify LRU: oldest keys were evicted first
        if n > max_entries:
            for i in range(n - max_entries):
                assert f"k{i}" not in reg, f"Oldest key k{i} should have been evicted"
            for i in range(n - max_entries, n):
                assert f"k{i}" in reg, f"Recent key k{i} should survive"

    @staticmethod
    def assert_discard_removes(reg: BoundedSessionRegistry) -> None:
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
    reg = BoundedSessionRegistry(max_entries=max_entries)
    TestGenericProperties.assert_registry_bound(reg, max_entries, max_entries * 4)


@pytest.mark.parametrize("max_entries", [1, 5, 50])
def test_discard_after_insert(max_entries: int) -> None:
    """Explicit discard drops the entry immediately."""
    reg = BoundedSessionRegistry(max_entries=max_entries)
    TestGenericProperties.assert_discard_removes(reg)


# ---------------------------------------------------------------------------
# TTL tests
# ---------------------------------------------------------------------------


class TestTTL:
    def test_ttl_eviction(self) -> None:
        reg = BoundedSessionRegistry(max_entries=100, ttl_seconds=1)
        reg["sess:a"] = 1
        reg["sess:b"] = 2
        assert len(reg) == 2
        time.sleep(1.1)
        # Access triggers eviction
        _ = reg.get("sess:c", None)
        assert "sess:a" not in reg
        assert "sess:b" not in reg

    def test_ttl_no_eviction_within_window(self) -> None:
        reg = BoundedSessionRegistry(max_entries=100, ttl_seconds=60)
        reg["sess:a"] = 1
        assert reg["sess:a"] == 1

    def test_ttl_zero_is_noop(self) -> None:
        reg = BoundedSessionRegistry(max_entries=100, ttl_seconds=0)
        reg["sess:a"] = 1
        assert reg["sess:a"] == 1

    def test_ttl_with_access_eviction(self) -> None:
        reg = BoundedSessionRegistry(max_entries=10, ttl_seconds=1, evict_on_access=True)
        reg["k1"] = 1
        time.sleep(1.1)
        # contains triggers eviction
        assert "k1" not in reg

    def test_ttl_evict_on_write_only(self) -> None:
        reg = BoundedSessionRegistry(max_entries=10, ttl_seconds=1, evict_on_access=False)
        reg["k1"] = 1
        time.sleep(1.1)
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
        reg = BoundedSessionRegistry(max_entries=5)
        for i in range(5):
            reg[f"k{i}"] = i
        assert len(reg) == 5

    def test_one_over_triggers_eviction(self) -> None:
        reg = BoundedSessionRegistry(max_entries=5)
        for i in range(6):
            reg[f"k{i}"] = i
        assert len(reg) == 5
        assert "k0" not in reg

    def test_evicts_oldest_first(self) -> None:
        reg = BoundedSessionRegistry(max_entries=3)
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
        reg = BoundedSessionRegistry(max_entries=0)
        for i in range(100):
            reg[f"k{i}"] = i
        assert len(reg) == 100


# ---------------------------------------------------------------------------
# Explicit discard (session terminal hook)
# ---------------------------------------------------------------------------


class TestDiscard:
    def test_discard_existing(self) -> None:
        reg = BoundedSessionRegistry(max_entries=10)
        reg["sess:1"] = "a"
        reg["sess:2"] = "b"
        reg.discard("sess:1")
        assert "sess:1" not in reg
        assert reg.get("sess:2") == "b"

    def test_discard_nonexistent_is_noop(self) -> None:
        reg = BoundedSessionRegistry(max_entries=10)
        reg.discard("ghost")  # no crash

    def test_discard_then_reinsert(self) -> None:
        reg = BoundedSessionRegistry(max_entries=5)
        reg["s"] = 1
        reg.discard("s")
        reg["s"] = 2
        assert reg["s"] == 2


# ---------------------------------------------------------------------------
# Clear / full reset
# ---------------------------------------------------------------------------


class TestClear:
    def test_clear_removes_all(self) -> None:
        reg = BoundedSessionRegistry(max_entries=10)
        reg["a"] = 1
        reg["b"] = 2
        n = reg.clear()
        assert n == 2
        assert len(reg) == 0

    def test_clear_empty(self) -> None:
        reg = BoundedSessionRegistry(max_entries=10)
        n = reg.clear()
        assert n == 0


# ---------------------------------------------------------------------------
# Concurrent safety (50-op smoke)
# ---------------------------------------------------------------------------


class TestConcurrent:
    @pytest.mark.asyncio
    async def test_concurrent_50_ops(self) -> None:
        reg = BoundedSessionRegistry(max_entries=20, ttl_seconds=0)

        async def worker(start: int) -> None:
            for i in range(start, start + 10):
                reg[f"k{i}"] = i
                _ = reg.get(f"k{i}")
                if i % 3 == 0:
                    reg.discard(f"k{i}")

        import asyncio

        tasks = [worker(i * 10) for i in range(5)]
        await asyncio.gather(*tasks)
        assert len(reg) <= 20
        assert reg.eviction_count >= 0


# ---------------------------------------------------------------------------
# Observe eviction counter
# ---------------------------------------------------------------------------


class TestObservability:
    def test_eviction_count_ttl(self) -> None:
        reg = BoundedSessionRegistry(max_entries=10, ttl_seconds=1)
        reg["a"] = 1
        time.sleep(1.1)
        reg["b"] = 2  # triggers evict
        assert reg.eviction_count >= 1

    def test_eviction_count_cap(self) -> None:
        reg = BoundedSessionRegistry(max_entries=3)
        for i in range(10):
            reg[f"k{i}"] = i
        assert reg.eviction_count == 7

    def test_stats_dict(self) -> None:
        reg = BoundedSessionRegistry(max_entries=10, ttl_seconds=5)
        s = reg.stats()
        assert s["max_entries"] == 10
        assert s["ttl_seconds"] == 5
        assert s["current_entries"] == 0
        assert "eviction_count" in s


# ---------------------------------------------------------------------------
# setdefault
# ---------------------------------------------------------------------------


class TestSetDefault:
    def test_setdefault_inserts(self) -> None:
        reg = BoundedSessionRegistry(max_entries=10)
        val = reg.setdefault("k", 42)
        assert val == 42
        assert reg["k"] == 42

    def test_setdefault_returns_existing(self) -> None:
        reg = BoundedSessionRegistry(max_entries=10)
        reg["k"] = 99
        val = reg.setdefault("k", 42)
        assert val == 99


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_snapshot_plain_dict(self) -> None:
        reg = BoundedSessionRegistry(max_entries=10)
        reg["a"] = 1
        reg["b"] = 2
        snap = reg.snapshot()
        assert snap == {"a": 1, "b": 2}
        assert type(snap) is dict
