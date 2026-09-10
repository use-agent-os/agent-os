"""One bounded registry for every per-session dict in a long-lived process.

Fifteen separate leak reports had the same shape: a registry keyed by a session
id (or by a tuple containing one) that is inserted into and never evicted, so a
gateway serving many short sessions grows one entry per session for the life of
the process. Fixing them one at a time produced a different eviction policy per
site — some TTL, some max-size, some LRU, some ``pop()`` on a terminal event —
which is worse than the leak, because the next contributor has to learn fifteen
slightly different lifetime rules.

:class:`BoundedRegistry` is that one rule. The sites split into exactly two
lifetime shapes and the primitive takes one configuration for each:

``"session"``
    State that belongs to a session and should disappear when the session does.
    The correct trigger is the session's terminal event, which is what
    :func:`drop_session_state` delivers; the size ceiling is the backstop for a
    session that never emits one.

``"cache"``
    An entry that is only useful for a window after it is written. TTL plus a
    size ceiling.

Both ceilings come from :func:`registry_limits`, which the gateway configures
at boot, so there is one place to reason about how much a process retains.
"""

from __future__ import annotations

import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal, overload

RegistryShape = Literal["session", "cache"]

DEFAULT_SESSION_MAX_ENTRIES = 512
DEFAULT_CACHE_MAX_ENTRIES = 512
DEFAULT_CACHE_TTL_SECONDS = 900.0


@dataclass(frozen=True)
class RegistryLimits:
    """Process-wide ceilings for the two registry shapes."""

    session_max_entries: int = DEFAULT_SESSION_MAX_ENTRIES
    cache_max_entries: int = DEFAULT_CACHE_MAX_ENTRIES
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS


_limits = RegistryLimits()
_limits_lock = threading.Lock()


def registry_limits() -> RegistryLimits:
    """Return the ceilings in force right now."""

    return _limits


def configure_registry_limits(
    *,
    session_max_entries: int | None = None,
    cache_max_entries: int | None = None,
    cache_ttl_seconds: float | None = None,
) -> RegistryLimits:
    """Update the process-wide ceilings.

    Registries resolve their limit on every write, so a registry built before
    the gateway read its config still honours the configured value. A
    non-positive value is ignored rather than disabling the bound.
    """

    global _limits
    with _limits_lock:
        updates: dict[str, Any] = {}
        if session_max_entries is not None and session_max_entries > 0:
            updates["session_max_entries"] = int(session_max_entries)
        if cache_max_entries is not None and cache_max_entries > 0:
            updates["cache_max_entries"] = int(cache_max_entries)
        if cache_ttl_seconds is not None and cache_ttl_seconds > 0:
            updates["cache_ttl_seconds"] = float(cache_ttl_seconds)
        _limits = replace(_limits, **updates)
        return _limits


def reset_registry_limits() -> None:
    """Restore boot defaults. For tests and gateway restarts."""

    global _limits
    with _limits_lock:
        _limits = RegistryLimits()


_MISSING = object()


class BoundedRegistry[KT, VT]:
    """A dict with an LRU ceiling, an optional TTL, and an eviction counter.

    The read API mirrors the ``dict`` methods the call sites already used, so a
    migration is a constructor change rather than a rewrite. Every operation
    holds a re-entrant lock: several adopters are touched from both the event
    loop and a worker thread.
    """

    __slots__ = (
        "__weakref__",
        "_entries",
        "_evictable",
        "_lock",
        "_max_entries",
        "_name",
        "_now",
        "_session_of",
        "_shape",
        "_ttl_seconds",
        "evictions",
        "expirations",
    )

    def __init__(
        self,
        *,
        shape: RegistryShape = "session",
        name: str = "",
        max_entries: int | None = None,
        ttl_seconds: float | None = None,
        session_of: Callable[[KT, VT], str | None] | None = None,
        evictable: Callable[[VT], bool] | None = None,
        time_source: Callable[[], float] = time.monotonic,
        register: bool = True,
    ) -> None:
        """Build a registry.

        ``max_entries`` and ``ttl_seconds`` override the shape's configured
        ceiling for a site with its own natural bound; leaving them ``None``
        follows :func:`registry_limits`.

        ``session_of`` maps an entry to the session id it belongs to — it is
        given both the key and the value, because some registries carry the
        session id in the value rather than the key — which is what
        lets :func:`drop_session_state` evict this registry's share of a session
        deterministically. A registry whose keys are not session-scoped leaves
        it ``None`` and is simply skipped by that sweep.

        ``evictable`` lets a site veto the removal of a value that is still in
        use — a held ``asyncio.Lock`` is the motivating case, since dropping one
        would hand the next caller a fresh lock and silently break the mutual
        exclusion it was protecting. Both the ceiling sweep and
        :meth:`discard_session` honour it; an explicit :meth:`discard` of a
        single key does not, because that caller owns the entry.
        """

        self._entries: OrderedDict[KT, tuple[VT, float]] = OrderedDict()
        self._lock = threading.RLock()
        self._shape: RegistryShape = shape
        self._name = name
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._session_of = session_of
        self._evictable = evictable
        self._now = time_source
        self.evictions = 0
        self.expirations = 0
        if register:
            _register(self)

    # ── configuration ────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._name or type(self).__name__

    @property
    def max_entries(self) -> int:
        if self._max_entries is not None:
            return self._max_entries
        limits = registry_limits()
        return limits.session_max_entries if self._shape == "session" else limits.cache_max_entries

    @property
    def ttl_seconds(self) -> float | None:
        if self._ttl_seconds is not None:
            return self._ttl_seconds
        return registry_limits().cache_ttl_seconds if self._shape == "cache" else None

    # ── mapping surface ──────────────────────────────────────────────

    def __len__(self) -> int:
        with self._lock:
            self._expire()
            return len(self._entries)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            self._expire()
            return key in self._entries

    def __iter__(self) -> Iterator[KT]:
        return iter(self.keys())

    def keys(self) -> list[KT]:
        with self._lock:
            self._expire()
            return list(self._entries)

    def values(self) -> list[VT]:
        with self._lock:
            self._expire()
            return [value for value, _ in self._entries.values()]

    def items(self) -> list[tuple[KT, VT]]:
        with self._lock:
            self._expire()
            return [(key, value) for key, (value, _) in self._entries.items()]

    @overload
    def get(self, key: KT) -> VT | None: ...

    @overload
    def get[D](self, key: KT, default: D) -> VT | D: ...

    def get(self, key: KT, default: Any = None) -> Any:
        with self._lock:
            self._expire()
            found = self._entries.get(key)
            if found is None:
                return default
            self._entries.move_to_end(key)
            return found[0]

    def __getitem__(self, key: KT) -> VT:
        found = self.get(key, _MISSING)  # type: ignore[arg-type]
        if found is _MISSING:
            raise KeyError(key)
        return found  # type: ignore[return-value]

    def set(self, key: KT, value: VT) -> VT:
        with self._lock:
            self._expire()
            self._entries[key] = (value, self._now())
            self._entries.move_to_end(key)
            self._enforce_ceiling()
            return value

    def __setitem__(self, key: KT, value: VT) -> None:
        self.set(key, value)

    def setdefault(self, key: KT, default: VT) -> VT:
        """Insert ``default`` when ``key`` is absent and return the live value.

        Matches ``dict.setdefault``: ``default`` is always evaluated by the
        caller, so a site that passes a freshly built lock keeps working.
        """

        with self._lock:
            self._expire()
            found = self._entries.get(key)
            if found is not None:
                self._entries.move_to_end(key)
                return found[0]
            return self.set(key, default)

    @overload
    def pop(self, key: KT) -> VT: ...

    @overload
    def pop[D](self, key: KT, default: D) -> VT | D: ...

    def pop(self, key: KT, default: Any = _MISSING) -> Any:
        with self._lock:
            self._expire()
            found = self._entries.pop(key, None)
            if found is not None:
                return found[0]
            if default is _MISSING:
                raise KeyError(key)
            return default

    def __delitem__(self, key: KT) -> None:
        with self._lock:
            del self._entries[key]

    def discard(self, key: KT) -> bool:
        """Drop ``key`` if present. Returns whether anything was removed."""

        with self._lock:
            return self._entries.pop(key, None) is not None

    def discard_where(self, predicate: Callable[[KT, VT], bool]) -> int:
        """Drop every entry matching ``predicate``. Returns how many went.

        The predicate is given both key and value. Entries the site declared
        busy via ``evictable`` are left in place.
        """

        with self._lock:
            return self._discard(
                [
                    key
                    for key, (value, _) in self._entries.items()
                    if predicate(key, value) and self._is_evictable(value)
                ]
            )

    def _discard(self, keys: list[KT]) -> int:
        for key in keys:
            del self._entries[key]
        return len(keys)

    def discard_session(self, session_id: str) -> int:
        """Drop every entry belonging to ``session_id``.

        A registry built without ``session_of`` cannot answer the question and
        removes nothing.
        """

        session_of = self._session_of
        if session_of is None:
            return 0
        with self._lock:
            return self._discard(
                [
                    key
                    for key, (value, _) in self._entries.items()
                    if session_of(key, value) == session_id and self._is_evictable(value)
                ]
            )

    def update(self, other: Mapping[KT, VT] | Iterable[tuple[KT, VT]]) -> None:
        pairs = other.items() if isinstance(other, Mapping) else other
        with self._lock:
            for key, value in pairs:
                self.set(key, value)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    # ── observability ────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "shape": self._shape,
                "entries": len(self._entries),
                "maxEntries": self.max_entries,
                "ttlSeconds": self.ttl_seconds,
                "evictions": self.evictions,
                "expirations": self.expirations,
            }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<BoundedRegistry {self.name} entries={len(self._entries)} max={self.max_entries}>"

    # ── internals ────────────────────────────────────────────────────

    def _expire(self) -> None:
        ttl = self.ttl_seconds
        if ttl is None or not self._entries:
            return
        cutoff = self._now() - ttl
        # A site can veto eviction for a busy value (a running background
        # process, a held lock). Every other path honours that veto; TTL was
        # the one that did not, so a long-running process was dropped from
        # its registry mid-flight — unreachable but still running (see #1131
        # review). `set()` refreshes the timestamp, so an entry the site
        # keeps re-inserting stays alive; a vetoed entry that is never
        # touched again is skipped until the site releases it.
        doomed = [
            key
            for key, (value, written) in self._entries.items()
            if written <= cutoff and self._is_evictable(value)
        ]
        for key in doomed:
            del self._entries[key]
        self.expirations += len(doomed)

    def _is_evictable(self, value: VT) -> bool:
        if self._evictable is None:
            return True
        try:
            return bool(self._evictable(value))
        except Exception:  # noqa: BLE001 - a broken predicate must not pin memory
            return True

    def _enforce_ceiling(self) -> None:
        ceiling = self.max_entries
        if len(self._entries) <= ceiling:
            return
        # Least-recently-used first, skipping anything the site says is busy.
        # A registry that is entirely busy stays over its ceiling rather than
        # breaking the invariant the value protects; it drains as entries free.
        for key in list(self._entries):
            if len(self._entries) <= ceiling:
                return
            value, _ = self._entries[key]
            if not self._is_evictable(value):
                continue
            del self._entries[key]
            self.evictions += 1


# ── process-wide teardown ────────────────────────────────────────────

# Weak, so the table that exists to stop leaks cannot become one: a registry
# owned by a discarded object (a per-test SessionStreamRegistry, a torn-down
# ApprovalQueue) drops out of here when it is collected.
_registries: weakref.WeakSet[BoundedRegistry[Any, Any]] = weakref.WeakSet()
_registries_lock = threading.Lock()


def _register(registry: BoundedRegistry[Any, Any]) -> None:
    with _registries_lock:
        _registries.add(registry)


def _live_registries() -> list[BoundedRegistry[Any, Any]]:
    with _registries_lock:
        return list(_registries)


def drop_session_state(session_key: str) -> int:
    """Evict every registry's share of ``session_key``.

    This is the deterministic half of the contract: the gateway calls it once
    on a session's terminal event (delete / abort / completion) and every
    session-scoped registry in the process drops that session immediately,
    rather than ageing out behind a ceiling. Returns the number of entries
    removed, for logging.
    """

    key = (session_key or "").strip()
    if not key:
        return 0
    return sum(registry.discard_session(key) for registry in _live_registries())


def registry_stats() -> list[dict[str, Any]]:
    """Per-registry occupancy and eviction counts, for diagnostics."""

    return [registry.stats() for registry in _live_registries()]
