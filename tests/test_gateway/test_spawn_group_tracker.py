"""SpawnGroupTracker isolates per-group state and supports eviction."""

from __future__ import annotations

from agentos.gateway.subagent_announce import SpawnGroupTracker


def test_mark_and_query_closed() -> None:
    t = SpawnGroupTracker()
    assert not t.is_closed("agent:main:main", "task-1")
    t.mark_closed("agent:main:main", "task-1")
    assert t.is_closed("agent:main:main", "task-1")
    assert not t.is_closed("agent:main:main", "task-2")


def test_woken_lifecycle() -> None:
    t = SpawnGroupTracker()
    key = ("agent:main:main", "task-1")
    assert not t.is_woken(key)
    t.mark_woken(key)
    assert t.is_woken(key)
    t.discard_woken(key)
    assert not t.is_woken(key)


def test_evict_drops_all_groups_for_a_parent_session() -> None:
    t = SpawnGroupTracker()
    t.mark_closed("agent:main:main", "task-1")
    t.mark_closed("agent:main:main", "task-2")
    t.mark_woken(("agent:main:main", "task-1"))
    t.mark_closed("agent:other:main", "task-1")

    removed = t.evict("agent:main:main")

    assert removed == 3
    assert not t.is_closed("agent:main:main", "task-1")
    assert not t.is_closed("agent:main:main", "task-2")
    assert not t.is_woken(("agent:main:main", "task-1"))
    # Other parent session unaffected
    assert t.is_closed("agent:other:main", "task-1")


def test_a_hold_lasts_until_the_recovery_settles() -> None:
    t = SpawnGroupTracker()
    t.begin_hold("agent:main:main", "task-1")
    assert t.end_hold("agent:main:main", "task-1") is None
    assert t.is_held("agent:main:main", "task-1")

    assert t.settle_held("agent:main:main", resumed=True) == ["task-1"]
    assert not t.is_held("agent:main:main", "task-1")
    assert t.settle_held("agent:main:main", resumed=False) == []


def test_a_hold_written_while_the_recovery_settles_learns_the_outcome() -> None:
    t = SpawnGroupTracker()
    for resumed in (True, False):
        t.begin_hold("agent:main:main", "task-1")
        assert t.settle_held("agent:main:main", resumed=resumed) == []
        assert t.end_hold("agent:main:main", "task-1") is resumed
        assert not t.is_held("agent:main:main", "task-1")


def test_evict_drops_holds_too() -> None:
    t = SpawnGroupTracker()
    t.begin_hold("agent:main:main", "task-1")
    t.end_hold("agent:main:main", "task-1")
    t.begin_hold("agent:main:main", "task-2")

    assert t.evict("agent:main:main") == 2
    assert not t.is_held("agent:main:main", "task-1")
    assert t.evict("agent:main:main") == 0
