"""BoundedSessionRegistry — shared bounded-dict primitive for per-session state.

Replaces 20+ bare dicts that grow unbounded with one configurable primitive:

- LRU eviction when the size cap is exceeded
- Optional per-entry TTL expiry
- Explicit ``discard()`` for deterministic cleanup on session terminal events
- ``eviction_count`` for observability

All 20 sites listed in gh-1131 migrate to this single class, ensuring one
eviction policy, one set of metrics, and one property test across the codebase.
"""

from __future__ import annotations

import time
import weakref
from collections import OrderedDict
from collections.abc import ItemsView, Iterator, KeysView, ValuesView
from typing import Any, TypeVar, overload

K = TypeVar("K")
V = TypeVar("V")

# WeakSet of session-scoped registries that _evict_session_runtime_state should
# discard. Using WeakSet prevents memory leaks when registries are collected.
_session_scoped_registries: weakref.WeakSet[BoundedSessionRegistry[Any, Any]] = weakref.WeakSet()


def _register_session_scoped(reg: BoundedSessionRegistry[Any, Any]) -> None:
    """Register a registry as session-scoped for eviction on terminal events."""
    _session_scoped_registries.add(reg)


def _discard_from_all(session_key: str) -> None:
    """Call discard on every registered session-scoped registry."""
    for reg in list(_session_scoped_registries):
        try:
            reg.discard(session_key)
        except Exception:
            pass


class BoundedSessionRegistry[K, V]:
    """A dict-like bounded registry with TTL and LRU eviction.

    Two lifetime shapes (from gh-1131):

    *Session-scoped state* — entries are dropped deterministically via
    ``discard(session_key)`` when the session terminates. The cap is a
    bounded backstop for sessions that never emit a terminal event.

    *Time-scoped caches* — entries expire via TTL and/or cap-based LRU
    eviction. Use ``ttl_seconds`` to set a per-entry TTL window.

    Parameters
    ----------
    max_entries:
        Hard cap on the number of entries. Once reached, the oldest entry
        (by insertion/update order) is evicted. ``0`` = no cap.
    ttl_seconds:
        Per-entry TTL in seconds. An entry is stale when ``time.time()``
        exceeds its birth time by this amount. ``0`` = no TTL.
    evict_on_access:
        When True (default), stale entries are evicted on ``__getitem__``
        / ``get()``. When False, only ``__setitem__`` triggers eviction.
    session_scoped:
        When True, the registry automatically registers for deterministic
        teardown on session terminal events via ``_discard_from_all()``.
    """

    __slots__ = (
        "_max_entries",
        "_ttl_s",
        "_evict_on_access",
        "_store",
        "_birth",
        "_eviction_count",
        "__weakref__",
    )

    def __init__(
        self,
        max_entries: int = 0,
        ttl_seconds: float = 0,
        evict_on_access: bool = True,
        session_scoped: bool = False,
    ) -> None:
        self._max_entries = max(0, int(max_entries))
        self._ttl_s = max(0.0, float(ttl_seconds))
        self._evict_on_access = bool(evict_on_access)
        self._store: OrderedDict[K, V] = OrderedDict()
        self._birth: dict[K, float] = {}
        self._eviction_count: int = 0
        if session_scoped:
            _register_session_scoped(self)

    # -- dict-like interface ------------------------------------------------

    def __iter__(self) -> Iterator[K]:
        return iter(self._store)

    def __getitem__(self, key: K) -> V:
        if self._evict_on_access:
            self._evict_stale()
        val = self._store[key]
        self._store.move_to_end(key)
        return val

    def __setitem__(self, key: K, value: V) -> None:
        self._store[key] = value
        self._store.move_to_end(key)
        self._birth[key] = time.time()
        self._evict()

    def __delitem__(self, key: K) -> None:
        del self._store[key]
        self._birth.pop(key, None)

    def __contains__(self, key: object) -> bool:
        if self._evict_on_access and key in self._store:
            self._evict_stale()
        return key in self._store

    def __len__(self) -> int:
        return len(self._store)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(max_entries={self._max_entries}, "
            f"ttl_seconds={self._ttl_s}, entries={len(self._store)}, "
            f"evictions={self._eviction_count})"
        )

    # -- public api ------------------------------------------------------

    @overload
    def get(self, key: K, default: None = None) -> V | None: ...

    @overload
    def get(self, key: K, default: V) -> V: ...

    def get(self, key: K, default: V | None = None) -> V | None:
        try:
            return self[key]  # triggers __getitem__, which evicts stale
        except KeyError:
            return default

    def setdefault(self, key: K, default: V) -> V:
        if key not in self._store:
            self[key] = default
        return self._store[key]

    def pop(self, key: K, default: V | None = None) -> V | None:
        self._birth.pop(key, None)
        return self._store.pop(key, default)

    def discard(self, key: Any) -> None:
        """Remove *key* if present. No-op if missing.

        Called by session teardown hooks so session-scoped state is
        deterministically released rather than aged out.
        Supports both direct key matching and composite tuple keys containing
        *key*.
        """
        if key in self._store:
            self._store.pop(key, None)
            self._birth.pop(key, None)

        if isinstance(key, str):
            matching = [
                k
                for k in list(self._store.keys())
                if isinstance(k, tuple) and any(elem == key for elem in k)
            ]
            for k in matching:
                self._store.pop(k, None)
                self._birth.pop(k, None)

    def clear(self) -> int:
        """Remove all entries. Returns the number of entries removed."""
        n = len(self._store)
        self._store.clear()
        self._birth.clear()
        return n

    def update(self, other: BoundedSessionRegistry[K, V] | dict[K, V]) -> None:
        self._store.update(other)
        for k in other:
            self._store.move_to_end(k)
        now = time.time()
        self._birth.update({k: now for k in other})
        self._evict()

    @property
    def eviction_count(self) -> int:
        return self._eviction_count

    def snapshot(self) -> dict[K, V]:
        return dict(self._store)

    def keys(self) -> KeysView[K]:
        return self._store.keys()

    def values(self) -> ValuesView[V]:
        return self._store.values()

    def items(self) -> ItemsView[K, V]:
        return self._store.items()

    # -- internal eviction -------------------------------------------------

    def _evict(self) -> None:
        """Evict stale (TTL) entries first, then LRU entries past the cap."""
        self._evict_stale()
        self._evict_lru()

    def _evict_stale(self) -> None:
        if not self._ttl_s or not self._store:
            return
        now = time.time()
        cutoff = now - self._ttl_s
        stale_keys = [k for k, b in self._birth.items() if b < cutoff]
        for k in stale_keys:
            del self._store[k]
            del self._birth[k]
        self._eviction_count += len(stale_keys)

    def _evict_lru(self) -> None:
        if not self._max_entries or len(self._store) <= self._max_entries:
            return
        overflow = len(self._store) - self._max_entries
        for _ in range(overflow):
            key, _ = self._store.popitem(last=False)  # FIFO = oldest first
            self._birth.pop(key, None)
        self._eviction_count += overflow

    # -- serialization helpers (for metrics/logging) -----------------------

    def stats(self) -> dict[str, Any]:
        return {
            "max_entries": self._max_entries,
            "ttl_seconds": self._ttl_s,
            "evict_on_access": self._evict_on_access,
            "current_entries": len(self._store),
            "eviction_count": self._eviction_count,
            "type": f"{type(self).__name__}",
        }
