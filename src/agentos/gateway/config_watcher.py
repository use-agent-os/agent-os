"""Apply edits of the active config file while the gateway runs.

Every two seconds the watcher compares the file's signature (mtime, size,
inode). A change waits until the file has been still for half a second (an
editor saving in several writes, a deploy copying a file in), then goes
through :func:`config_commit.reload_config_from_disk`: validated like an RPC
write, applied in place, hot adapters re-synced, restart-only keys recorded as
pending. A ``config.changed`` event tells connected consoles what happened.

A file that does not parse or validate never replaces the running config: the
last good snapshot stays, ``config.reload_skipped`` is logged, and the event
carries ``status: "invalid"``; fixing it back to the running settings sends
``status: "recovered"``. A write the gateway made itself (a config RPC)
is recognized by content and does nothing. ``[gateway] config_reload =
false`` turns the watcher off without a restart.

Changes to the operator-only settings (``config_commit.OPERATOR_ONLY_CONFIG_ROOTS``:
trading, hooks, tools, permissions, sandbox, auth, ...) are *held*: the file
is a same-user write away for an agent's shell, so the watcher never makes
them live. They are reported (``held``, ``status: "held"`` when nothing else
changed) and wait for ``agentos config reload`` from the operator or a
restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

POLL_INTERVAL_SECONDS = 2.0
DEBOUNCE_SECONDS = 0.5

Signature = tuple[int, int, int] | None
Emitter = Callable[[str, dict[str, Any]], Awaitable[Any]]


def file_signature(path: Path) -> Signature:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size, info.st_ino)


@dataclass
class ReloadStatus:
    """The watcher's last verdict, for ``doctor.status``."""

    status: str = "idle"  # idle | applied | held | unchanged | invalid
    at: float | None = None
    error: str | None = None
    changed: list[str] = field(default_factory=list)
    restart_paths: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)


_last_status = ReloadStatus()


def last_reload_status() -> ReloadStatus:
    return _last_status


class ConfigWatcher:
    def __init__(
        self,
        ctx_factory: Callable[[], Any],
        *,
        emit: Emitter | None = None,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        debounce: float = DEBOUNCE_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._ctx_factory = ctx_factory
        self._emit = emit
        self._poll = poll_interval
        self._debounce = debounce
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._signature: Signature = None
        self._path: Path | None = None

    def _config(self) -> Any:
        return getattr(self._ctx_factory(), "config", None)

    def _enabled(self) -> bool:
        gateway = getattr(self._config(), "gateway", None)
        return bool(getattr(gateway, "config_reload", True))

    def _current_path(self) -> Path | None:
        value = getattr(self._config(), "config_path", None)
        return Path(str(value)).expanduser() if value else None

    def start(self) -> None:
        if self._task is not None:
            return
        self._path = self._current_path()
        if self._path is not None:
            self._signature = file_signature(self._path)
        self._task = asyncio.create_task(self._run(), name="config-watcher")
        log.info("config.watch_started", path=str(self._path) if self._path else None)

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while True:
            await self._sleep(self._poll)
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the watcher must outlive one bad poll
                log.warning("config.watch_poll_failed", exc_info=True)

    async def poll_once(self) -> bool:
        """Check the file once; reload when it changed. True when a reload ran."""
        path = self._current_path()
        if path != self._path:
            self._path = path
            self._signature = file_signature(path) if path else None
            return False
        if path is None:
            return False
        signature = file_signature(path)
        if signature == self._signature or signature is None:
            return False
        # Wait for the writer to finish: the file must hold still.
        while True:
            await self._sleep(self._debounce)
            settled = file_signature(path)
            if settled == signature:
                break
            signature = settled
            if signature is None:
                return False
        self._signature = signature
        if not self._enabled():
            log.info("config.reload_disabled", path=str(path))
            return False
        await self.reload()
        return True

    async def reload(self) -> None:
        from agentos.gateway.config_commit import ConfigReloadError, reload_config_from_disk

        global _last_status
        ctx = self._ctx_factory()
        try:
            result = reload_config_from_disk(ctx)
        except ConfigReloadError as exc:
            _last_status = ReloadStatus(status="invalid", at=time.time(), error=str(exc))
            log.warning("config.reload_skipped", path=str(self._path), error=str(exc))
            await self._send(
                {
                    "source": "file",
                    "status": "invalid",
                    "path": str(self._path),
                    "error": str(exc),
                }
            )
            return
        if result.held:
            log.warning(
                "config.reload_held",
                path=str(result.path),
                held=list(result.held_paths),
                hint="operator-only settings; run `agentos config reload` or restart",
            )
        if not result.changed and result.held:
            _last_status = ReloadStatus(status="held", at=time.time(), held=list(result.held_paths))
            await self._send(
                {
                    "source": "file",
                    "status": "held",
                    "path": str(result.path),
                    "revision": result.revision,
                    "changed": [],
                    "hot": [],
                    "held": list(result.held_paths),
                    "restartRequired": [],
                    "restartReasons": list(result.restart_reasons),
                    "pendingRestart": bool(result.restart_reasons),
                    "appliedNow": False,
                }
            )
            return
        if not result.changed:
            previous = _last_status.status
            _last_status = ReloadStatus(status="unchanged", at=time.time())
            log.debug("config.reload_unchanged", path=str(result.path))
            if previous == "invalid":
                # The broken file was fixed back to what is running: say so,
                # or a console showing the error would keep showing it.
                await self._send(
                    {
                        "source": "file",
                        "status": "recovered",
                        "path": str(result.path),
                        "revision": result.revision,
                        "changed": [],
                        "hot": [],
                        "restartRequired": [],
                        "restartReasons": [],
                        "pendingRestart": False,
                        "appliedNow": True,
                    }
                )
            return
        _last_status = ReloadStatus(
            status="applied",
            at=time.time(),
            changed=list(result.changed_paths),
            restart_paths=list(result.restart_paths),
            held=list(result.held_paths),
        )
        log.info(
            "config.reloaded",
            path=str(result.path),
            changed=list(result.changed_paths),
            hot=list(result.hot_paths),
            restart_required=list(result.restart_paths),
            restart_reasons=list(result.restart_reasons),
        )
        await self._send(
            {
                "source": "file",
                "status": "applied",
                "path": str(result.path),
                "revision": result.revision,
                "changed": list(result.changed_paths),
                "hot": list(result.hot_paths),
                "held": list(result.held_paths),
                "restartRequired": list(result.restart_paths),
                "restartReasons": list(result.restart_reasons),
                "pendingRestart": bool(result.restart_reasons),
                "appliedNow": result.applied_now,
            }
        )

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._emit is None:
            return
        try:
            await self._emit("config.changed", payload)
        except Exception:  # noqa: BLE001 - a dead socket must not stop the watcher
            log.debug("config.changed_emit_failed", exc_info=True)


def note_operator_reload(result: Any) -> None:
    """Record an operator's ``config.reload`` as the watcher's last verdict."""
    global _last_status
    _last_status = ReloadStatus(
        status="applied",
        at=time.time(),
        changed=list(getattr(result, "changed_paths", ())),
        restart_paths=list(getattr(result, "restart_paths", ())),
    )


async def broadcast_to_control(event: str, payload: dict[str, Any]) -> None:
    """Send ``event`` to every Control connection (config paths are operator data)."""
    from agentos.gateway.access import ConnectionSurface
    from agentos.gateway.websocket import get_registry

    for conn in get_registry().all():
        if getattr(conn.access, "surface", None) is not ConnectionSurface.CONTROL:
            continue
        try:
            await conn.send_event(event, payload)
        except Exception:  # noqa: BLE001 - one closed socket must not stop the rest
            continue


__all__ = [
    "DEBOUNCE_SECONDS",
    "POLL_INTERVAL_SECONDS",
    "ConfigWatcher",
    "ReloadStatus",
    "broadcast_to_control",
    "file_signature",
    "last_reload_status",
    "note_operator_reload",
]
