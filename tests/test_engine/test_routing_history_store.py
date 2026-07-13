"""RoutingHistoryStore exposes a bounded per-session history with eviction."""

from __future__ import annotations

from agentos.engine.steps.agentos_router import RoutingHistoryStore


def test_get_set_setdefault() -> None:
    store = RoutingHistoryStore()
    assert store.get("agent:main:main") is None
    store.set("agent:main:main", [{"turn_index": 0}])
    assert store.get("agent:main:main") == [{"turn_index": 0}]
    same = store.setdefault("agent:main:main", [])
    assert same == [{"turn_index": 0}]
    fresh = store.setdefault("agent:other:main", [])
    assert fresh == []


def test_length_reports_zero_for_unknown_keys() -> None:
    store = RoutingHistoryStore()
    assert store.length("never:set") == 0
    store.set("agent:main:main", [{"turn_index": 0}, {"turn_index": 1}])
    assert store.length("agent:main:main") == 2


def test_evict_removes_only_the_named_session() -> None:
    store = RoutingHistoryStore()
    store.set("agent:main:main", [{"turn_index": 0}])
    store.set("agent:other:main", [{"turn_index": 0}])
    assert store.evict("agent:main:main") is True
    assert store.get("agent:main:main") is None
    assert store.get("agent:other:main") == [{"turn_index": 0}]
    assert store.evict("agent:main:main") is False  # idempotent


def test_clear_drops_all_entries() -> None:
    store = RoutingHistoryStore()
    store.set("a", [])
    store.set("b", [])
    store.clear()
    assert store.get("a") is None
    assert store.get("b") is None
