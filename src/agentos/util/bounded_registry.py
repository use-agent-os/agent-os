"""A bounded, LRU-evicting, optionally TTL-backed registry.

Replaces the twenty unbounded dicts catalogued in
https://github.com/use-agent-os/agent-os/issues/1131 with a single primitive
that can be configured for either lifetime shape:

- **Session-scoped state (Shape A):** entries are dropped deterministically
  via ``discard()`` on session terminal events, with a max-size backstop for
  sessions that never emit an event.
- **Time-scoped caches (Shape B):** entries expire after a configurable TTL,
  with a max-size backstop as overflow protection.

Both shapes share the same LRU eviction policy — when the cap is exceeded the
oldest entries are evicted first.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from threading import Lock
from typing import TypeVar, cast

KT = TypeVar("KT")
VT = TypeVar("VT")

_DEFAULT_MAX_ENTRIES = 500


class BoundedRegistry[KT, VT]:
    """A dict-like bounded registry with LRU eviction and optional TTL.
    """

    def __init__(
        self, *, max_entries: int = _DEFAULT_MAX_ENTRIES, ttl_seconds: float = 0.0
    ) -> None:
        if max_entries < 0:
            raise ValueError(f"max_entries must be >= 0, got {max_entries}")
        if ttl_seconds < 0:
            raise ValueError(f"ttl_seconds must be >= 0, got {ttl_seconds}")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._data: OrderedDict[KT, VT] = OrderedDict()
        self._timestamps: dict[KT, float] = {}
        self._lock = Lock()
        self._eviction_count = 0

    def get(self, key: KT, default: VT | None = None) -> VT | None:
        with self._lock:
            self._evict_stale()
            if key not in self._data:
                return default
            self._data.move_to_end(key)
            return self._data[key]

    def set(self, key: KT, value: VT) -> None:
        with self._lock:
            self._evict_stale()
            self._data[key] = value
            self._data.move_to_end(key)
            self._timestamps[key] = time.monotonic()
            self._trim_to_fit()

    def discard(self, key: KT) -> bool:
        with self._lock:
            was_present = key in self._data
            self._data.pop(key, None)
            self._timestamps.pop(key, None)
            return was_present

    def get_or_create(self, key: KT, factory: type[VT] | None = None) -> VT:
        with self._lock:
            self._evict_stale()
            try:
                self._data.move_to_end(key)
                return self._data[key]
            except KeyError:
                pass
            if factory is not None:
                self._data[key] = factory()
            else:
                self._data[key] = cast(VT, None)
            self._data.move_to_end(key)
            self._timestamps[key] = time.monotonic()
            self._trim_to_fit()
            return self._data[key]

    def clear(self) -> int:
        with self._lock:
            count = len(self._data)
            self._data.clear()
            self._timestamps.clear()
            self._eviction_count += count
            return count

    def __getitem__(self, key: KT) -> VT:
        with self._lock:
            self._evict_stale()
            self._data.move_to_end(key)
            return self._data[key]

    def __setitem__(self, key: KT, value: VT) -> None:
        self.set(key, value)

    def __delitem__(self, key: KT) -> None:
        with self._lock:
            del self._data[key]
            self._timestamps.pop(key, None)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            self._evict_stale()
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __iter__(self):
        with self._lock:
            return iter(list(self._data.keys()))

    def items(self) -> list[tuple[KT, VT]]:
        with self._lock:
            return list(self._data.items())

    def pop(self, key: KT, default: VT | None = None) -> VT | None:
        with self._lock:
            self._timestamps.pop(key, None)
            if key not in self._data:
                return default
            return self._data.pop(key)

    def values(self) -> list[VT]:
        with self._lock:
            return list(self._data.values())

    @property
    def eviction_count(self) -> int:
        return self._eviction_count

    @property
    def max_entries(self) -> int:
        return self._max_entries

    @property
    def ttl_seconds(self) -> float:
        return self._ttl_seconds

    def _evict_stale(self) -> None:
        if self._ttl_seconds <= 0 or not self._timestamps:
            return
        now = time.monotonic()
        deadline = now - self._ttl_seconds
        stale = [k for k, ts in self._timestamps.items() if ts < deadline]
        for k in stale:
            del self._data[k]
            del self._timestamps[k]
            self._eviction_count += 1

    def _trim_to_fit(self) -> None:
        if self._max_entries <= 0:
            return
        while len(self._data) > self._max_entries:
            oldest, _ = self._data.popitem(last=False)
            self._timestamps.pop(oldest, None)
            self._eviction_count += 1
