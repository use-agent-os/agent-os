"""Per-session write locks to prevent concurrent modifications."""

from __future__ import annotations

import asyncio
from typing import Any


class SessionWriteLock:
    """Async lock per session_key to serialize writes.

    Entries are evicted from the internal dict as soon as the lock is fully
    idle (released with no pending waiters), so the dict does not grow
    unboundedly with the number of unique session keys.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _has_active_waiters(lock: asyncio.Lock) -> bool:
        return bool(lock._waiters and any(not w.cancelled() for w in lock._waiters))

    def _maybe_evict(self, session_key: str, lock: asyncio.Lock) -> None:
        if self._locks.get(session_key) is lock:
            if not lock.locked() and not self._has_active_waiters(lock):
                del self._locks[session_key]

    async def acquire(self, session_key: str) -> None:
        if session_key not in self._locks:
            self._locks[session_key] = asyncio.Lock()
        lock = self._locks[session_key]
        try:
            await lock.acquire()
        except asyncio.CancelledError:
            self._maybe_evict(session_key, lock)
            raise

    def release(self, session_key: str) -> None:
        if session_key in self._locks:
            lock = self._locks[session_key]
            # Evict if no active waiter is queued: the next acquire() will create a
            # fresh lock for this session_key.  If uncancelled waiters exist, keep the entry
            # so they can acquire the already-released lock.
            if not self._has_active_waiters(lock):
                del self._locks[session_key]
            lock.release()

    async def __aenter__(self) -> SessionWriteLock:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass

    def context(self, session_key: str) -> SessionLockContext:
        """Return a context manager for the session lock."""
        return SessionLockContext(self, session_key)


class SessionLockContext:
    """Context manager for session write lock."""

    def __init__(self, lock_manager: SessionWriteLock, session_key: str) -> None:
        self._lock_manager = lock_manager
        self._session_key = session_key

    async def __aenter__(self) -> None:
        await self._lock_manager.acquire(self._session_key)

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._lock_manager.release(self._session_key)
