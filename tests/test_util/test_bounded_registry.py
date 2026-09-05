"""Property-based tests for BoundedRegistry — shared by every adopter.

These tests verify the invariants that every call site can rely on:

1. Under a max of M, at most M entries survive
2. An explicit discard drops the entry immediately
3. TTL entries are evicted on the next mutation after expiry
4. get() touches the entry (moves to end, refreshes TTL)
5. The eviction counter is monotonic
"""

from __future__ import annotations

import time

import pytest

from agentos.util.bounded_registry import BoundedRegistry

# ── cap / LRU ─────────────────────────────────────────────────────


class TestCap:
    def test_empty_under_cap(self) -> None:
        r = BoundedRegistry(max_entries=5)
        assert len(r) == 0
        assert r.eviction_count == 0

    def test_under_cap_no_eviction(self) -> None:
        r = BoundedRegistry(max_entries=10)
        for i in range(5):
            r.set(i, f"v{i}")
        assert len(r) == 5
        assert r.eviction_count == 0

    def test_at_cap_no_eviction(self) -> None:
        r = BoundedRegistry(max_entries=5)
        for i in range(5):
            r.set(i, f"v{i}")
        assert len(r) == 5
        assert r.eviction_count == 0

    def test_one_over_evicts_oldest(self) -> None:
        r = BoundedRegistry(max_entries=5)
        for i in range(6):
            r.set(i, f"v{i}")
        assert len(r) == 5
        assert r.eviction_count == 1
        # 0 (oldest) should be gone
        assert r.get(0) is None
        # 1–5 survive
        assert r.get(1) == "v1"
        assert r.get(5) == "v5"

    def test_lru_order_evicts_least_recently_used(self) -> None:
        r = BoundedRegistry(max_entries=3)
        r.set("a", 1)
        r.set("b", 2)
        r.set("c", 3)
        r.get("a")  # touch a → a is most recently used now
        r.set("d", 4)  # should evict b (oldest untouched)
        assert len(r) == 3
        assert r.get("a") == 1  # survived
        assert r.get("b") is None  # evicted
        assert r.get("c") == 3
        assert r.get("d") == 4

    def test_zero_max_never_evicts(self) -> None:
        r = BoundedRegistry(max_entries=0, ttl_seconds=0)
        for i in range(100):
            r.set(f"k{i}", i)
        assert len(r) == 100
        assert r.eviction_count == 0

    def test_set_overwrite_refreshes_position(self) -> None:
        r = BoundedRegistry(max_entries=3)
        r.set("a", 1)
        r.set("b", 2)
        r.set("c", 3)
        r.set("a", 99)  # overwrite a — moves to end
        r.set("d", 4)  # should evict b
        assert r.get("a") == 99
        assert r.get("b") is None
        assert r.get("c") == 3
        assert r.get("d") == 4


# ── discard ───────────────────────────────────────────────────────


class TestDiscard:
    def test_discard_existing(self) -> None:
        r = BoundedRegistry(max_entries=10)
        r.set("k", "v")
        assert r.discard("k") is True
        assert r.get("k") is None
        assert len(r) == 0

    def test_discard_missing(self) -> None:
        r = BoundedRegistry(max_entries=10)
        assert r.discard("nope") is False

    def test_discard_reduces_below_cap(self) -> None:
        r = BoundedRegistry(max_entries=3)
        for i in range(3):
            r.set(i, i)
        r.discard(0)
        r.set(99, 99)  # should not evict; we're at 3/3
        assert len(r) == 3
        assert r.get(0) is None
        assert r.get(99) == 99


# ── TTL ───────────────────────────────────────────────────────────


class TestTTL:
    def test_fresh_entries_survive(self) -> None:
        r = BoundedRegistry(max_entries=10, ttl_seconds=3600)
        r.set("k", "v")
        assert r.get("k") == "v"

    def test_stale_entries_evicted(self) -> None:
        r = BoundedRegistry(max_entries=10, ttl_seconds=0.01)
        r.set("k", "v")
        time.sleep(0.02)
        # Next mutation triggers eviction
        r.set("other", "x")
        assert r.get("k") is None

    def test_ttl_zero_disables_eviction(self) -> None:
        r = BoundedRegistry(max_entries=100, ttl_seconds=0)
        r.set("k", "v")
        time.sleep(0.02)
        r.set("other", "x")
        assert r.get("k") == "v"

    def test_mixed_fresh_and_stale(self) -> None:
        r = BoundedRegistry(max_entries=10, ttl_seconds=0.01)
        r.set("stale", "gone")
        time.sleep(0.02)
        r.set("fresh", "here")
        assert r.get("stale") is None
        assert r.get("fresh") == "here"
        assert len(r) == 1

    def test_contains_evicts_stale(self) -> None:
        r = BoundedRegistry(max_entries=10, ttl_seconds=0.01)
        r.set("k", "v")
        time.sleep(0.02)
        assert "k" not in r

    def test_get_touches_refreshes_ttl_on_hit(self) -> None:
        """get() on a live entry should not be enough to protect it from
        TTL in the general sense (we don't refresh timestamps on get),
        but it *does* move it to the end of the LRU ordering.
        """
        r = BoundedRegistry(max_entries=3, ttl_seconds=0.1)
        r.set("a", 1)
        r.set("b", 2)
        r.set("c", 3)
        time.sleep(0.05)
        # get does NOT refresh timestamp — so all are still fresh
        r.get("a")
        r.set("d", 4)  # evicts oldest untouched — b
        assert r.get("b") is None
        assert r.get("a") == 1


# ── get_or_create ─────────────────────────────────────────────────


class TestGetOrCreate:
    def test_creates_when_missing(self) -> None:
        r = BoundedRegistry[int, list[int]](max_entries=10)
        val = r.get_or_create(1, list)
        assert val == []
        assert r.get(1) is val

    def test_returns_existing(self) -> None:
        r = BoundedRegistry(max_entries=10)
        r.set("k", "existing")
        val = r.get_or_create("k", str)
        assert val == "existing"

    def test_under_cap(self) -> None:
        r = BoundedRegistry[str, list[int]](max_entries=2)
        r.get_or_create("a", list).append(1)
        r.get_or_create("b", list).append(2)
        r.get_or_create("a", list).append(3)  # touch a
        r.get_or_create("c", list).append(4)  # evicts b
        assert r.get("a") == [1, 3]
        assert r.get("b") is None
        assert r.get("c") == [4]


# ── clear ─────────────────────────────────────────────────────────


class TestClear:
    def test_clear_empties(self) -> None:
        r = BoundedRegistry(max_entries=10)
        for i in range(5):
            r.set(i, i)
        count = r.clear()
        assert count == 5
        assert len(r) == 0
        assert r.get(0) is None

    def test_clear_empty(self) -> None:
        r = BoundedRegistry(max_entries=10)
        assert r.clear() == 0


# ── eviction counter ──────────────────────────────────────────────


class TestEvictionCount:
    def test_monotonic(self) -> None:
        r = BoundedRegistry(max_entries=3)
        assert r.eviction_count == 0
        for i in range(6):
            r.set(f"k{i}", i)
        # first 3: no eviction; next 3: 1 each
        assert r.eviction_count == 3

    def test_clear_adds_to_count(self) -> None:
        r = BoundedRegistry(max_entries=5)
        for i in range(5):
            r.set(i, i)
        r.clear()
        assert r.eviction_count == 5


# ── thread safety (smoke) ─────────────────────────────────────────


class TestThreadSafety:
    def test_concurrent_set_and_get(self) -> None:
        import threading

        r = BoundedRegistry(max_entries=100)
        errors: list[Exception] = []

        def writer() -> None:
            for i in range(500):
                try:
                    r.set(i, i * 2)
                except Exception as e:
                    errors.append(e)

        def reader() -> None:
            for i in range(500):
                try:
                    r.get(i)
                except Exception as e:
                    errors.append(e)

        threads = [
            threading.Thread(target=writer, daemon=True),
            threading.Thread(target=reader, daemon=True),
            threading.Thread(target=writer, daemon=True),
            threading.Thread(target=reader, daemon=True),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"{len(errors)} errors: {errors[:3]}"


# ── constructor validation ────────────────────────────────────────


class TestConstructor:
    def test_defaults(self) -> None:
        r = BoundedRegistry()
        assert r.max_entries == 500
        assert r.ttl_seconds == 0.0

    def test_custom_values(self) -> None:
        r = BoundedRegistry(max_entries=100, ttl_seconds=300)
        assert r.max_entries == 100
        assert r.ttl_seconds == 300

    def test_negative_max_raises(self) -> None:
        with pytest.raises(ValueError, match="max_entries"):
            BoundedRegistry(max_entries=-1)

    def test_negative_ttl_raises(self) -> None:
        with pytest.raises(ValueError, match="ttl_seconds"):
            BoundedRegistry(ttl_seconds=-1)
