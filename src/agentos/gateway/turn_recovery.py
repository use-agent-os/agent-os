"""Resume turns that a gateway restart or crash cut short.

A turn the gateway stops under is not lost any more: its tool rounds are in
the transcript (:mod:`agentos.engine.turn_progress`), and its task is marked
abandoned -- ``shutdown_interrupted`` by a graceful shutdown or restart,
``process_restart`` by the startup sweep when the process died. On boot,
:class:`TurnRecovery` looks at each session whose newest task is such a turn
and decides from the transcript tail:

* the turn finished (its reply or error row is the newest) or there is
  nothing to answer -- close it, nothing to resume;
* the user's message was never answered, or the last recorded tool round
  completed -- resume normally, with the session's usual tools;
* calls of the next round were issued with no recorded result (their outcome
  is unknown) -- resume with a restricted, replay-safe tool set (reads and
  status only: :data:`RESTART_SAFE_TOOL_ALLOW`, each also declared
  ``read_only``). The model checks what happened instead of acting again;
  the full set returns with the user's next message.

A resume is a synthetic continuation turn (internal-system provenance, not a
replay of the user's message), run through the task runtime in a lane of
:data:`RECOVERY_LANE_CONCURRENCY`, with the interrupted run's id as its
idempotency key. A recovery cycle makes at most :data:`MAX_ATTEMPTS` attempts
with backoff, then leaves a tombstone row with a hint; a turn older than
:data:`MAX_AGE_MS` is not resumed. A new user message on the session cancels
a recovery that has not started yet. Cron, heartbeat, subagent and ACP turns
are never resumed.

The recovery state lives on the interrupted task's ledger row
(``details["restart_recovery"]``), so attempts are counted across restarts:
a turn that crashes the gateway cannot be resumed forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import structlog

from agentos.engine.turn_progress import TurnTail, classify_transcript_tail
from agentos.gateway.task_runtime import (
    SHUTDOWN_INTERRUPTED_REASON,
    TaskRuntimeNotAdmittingError,
)
from agentos.gateway.turn_recovery_route import envelope_from_task
from agentos.session.keys import canonicalize_session_key
from agentos.session.models import AgentTaskStatus

log = structlog.get_logger(__name__)

#: Terminal reasons of a turn the gateway stopped under (not a user cancel).
RECOVERABLE_TERMINAL_REASONS: Final = (
    SHUTDOWN_INTERRUPTED_REASON,
    "process_restart",
    "shutdown_timeout",
)
MAX_ATTEMPTS: Final = 3
MAX_AGE_MS: Final = 24 * 60 * 60 * 1000
#: Wait before attempt 1, 2 and 3 of a cycle (seconds).
BACKOFF_S: Final = (2.0, 15.0, 60.0)
RECOVERY_LANE_CONCURRENCY: Final = 4
#: Key of the recovery state on the task ledger row and of the marker on a
#: resumed turn's envelope metadata.
RECOVERY_KEY: Final = "restart_recovery"
RECOVERY_SOURCE_TOOL: Final = "restart_recovery"
_TRANSCRIPT_TAIL_ROWS = 80

#: Sources whose turns a person is waiting on. Cron and system work is not
#: resumed; their own schedulers re-run what matters.
_USER_FACING_SOURCES: Final = frozenset({"web", "cli", "channel"})
_EXCLUDED_RUN_KINDS: Final = frozenset(
    {"acp", "subagent", "cron_turn", "heartbeat", "memory_nudge"}
)

#: Tools a resumed turn may call when the interrupted turn left calls with an
#: unknown outcome. An allowlist, so a tool added later is never granted by
#: omission; every entry must also be registered with ``read_only=True``
#: (:func:`restart_safe_tool_names`). Nothing here writes files, runs code,
#: trades, sends a message, schedules, spawns, approves, or calls an
#: arbitrary URL.
RESTART_SAFE_TOOL_ALLOW: Final = frozenset(
    {
        # Filesystem and git reads.
        "read_file",
        "read_spreadsheet",
        "list_dir",
        "glob_search",
        "grep_search",
        "git_status",
        "git_diff",
        "git_log",
        "pdf",
        "image",
        # Memory and session reads.
        "memory_search",
        "memory_get",
        "session_search",
        "session_status",
        "sessions_list",
        "sessions_history",
        # Environment reads.
        "skill_list",
        "agents_list",
        "projects_list",
        "web_search",
    }
)

RecoveryAction = Literal["close", "resume", "resume_restricted"]
RecoveryState = Literal[
    "pending",
    "running",
    "resumed",
    "closed",
    "tombstoned",
    "cancelled",
    "reported",
    "expired",
]
_FINAL_STATES: Final = frozenset(
    {"resumed", "closed", "tombstoned", "cancelled", "reported", "expired"}
)
_STATES: Final = ("pending", "running", *sorted(_FINAL_STATES))


# ---------------------------------------------------------------------------
# Tool surface of a resumed turn
# ---------------------------------------------------------------------------


def restart_safe_tool_names(registry: Any = None) -> frozenset[str]:
    """The allowlist, narrowed to tools registered as ``read_only``."""
    if registry is None:
        from agentos.tools.registry import get_default_registry

        registry = get_default_registry()
    names: set[str] = set()
    for name in RESTART_SAFE_TOOL_ALLOW:
        registered = registry.get(name)
        spec = getattr(registered, "spec", None)
        if spec is not None and getattr(spec, "read_only", False) is True:
            names.add(name)
    return frozenset(names)


def recovery_marker(metadata: Any) -> dict[str, Any] | None:
    """The restart-recovery marker of a turn's envelope metadata, if any."""
    if not isinstance(metadata, dict):
        return None
    marker = metadata.get(RECOVERY_KEY)
    return marker if isinstance(marker, dict) else None


def apply_restart_recovery_tool_policy(
    tool_context: Any, envelope: Any, *, registry: Any = None
) -> bool:
    """Restrict a resumed turn's tools to the replay-safe set.

    Applied to the turn's ``ToolContext`` twice over: an allowlist
    intersection (the other tools are hidden and refused at dispatch) and a
    deny of every other registered tool. Denies survive the configured policy
    layers the engine applies later (a profile or ``allow`` entry can widen an
    allowlist, never lift a deny). Only the resumed turn carries the marker:
    the next user turn builds its context from its own envelope and gets the
    full set back.
    """
    marker = recovery_marker(getattr(envelope, "metadata", None))
    if marker is None or not marker.get("restricted"):
        return False
    if registry is None:
        from agentos.tools.registry import get_default_registry

        registry = get_default_registry()
    safe = set(restart_safe_tool_names(registry))
    current = getattr(tool_context, "allowed_tools", None)
    tool_context.allowed_tools = safe if current is None else set(current) & safe
    others = {name for name in registry.list_names() if name not in safe}
    tool_context.denied_tools = set(getattr(tool_context, "denied_tools", None) or ()) | others
    return True


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def decide_recovery(tail: TurnTail) -> tuple[RecoveryAction, str]:
    """What to do with a session whose transcript ends in *tail*."""
    if tail.status == "empty":
        return "close", "empty_transcript"
    if tail.status == "finished":
        return "close", "turn_finished"
    if tail.status == "unanswered_input":
        return "resume", "unanswered_input"
    if tail.pending_tool_use_ids:
        return "resume_restricted", "unknown_tool_outcome"
    return "resume", "round_complete"


def continuation_message(*, restricted: bool, unanswered_input: bool) -> str:
    """The synthetic turn that resumes an interrupted one."""
    if unanswered_input:
        text = (
            "[Restart recovery] The gateway restarted before you answered the latest "
            "message above. Answer it now. Do not ask the user to send it again."
        )
    else:
        text = (
            "[Restart recovery] The gateway restarted while you were working on the "
            "request above, and that turn stopped before it finished. Carry on from "
            "where it stopped, using what the transcript shows was already done. Do "
            "not start over and do not ask the user to send the request again."
        )
    if restricted:
        text += (
            " Some tool calls of that turn have no recorded result: their outcome is "
            "unknown, and they may or may not have taken effect. For this turn only "
            "read and status tools are available. Use them to check the current state, "
            "then tell the user what is done and what is still open. Do not repeat an "
            "action whose outcome is unknown; say what the user should confirm instead. "
            "All tools are available again with the user's next message."
        )
    return text


def tombstone_text(*, attempts: int, reason: str) -> str:
    if reason == "expired":
        return (
            "A reply was interrupted by a gateway restart more than 24 hours ago and "
            "was not resumed. Send a message to continue."
        )
    if reason == "restart_requested":
        return (
            "The gateway restarted as this conversation asked. The turn that asked "
            "was not resumed automatically, so it cannot restart the gateway again; "
            "send a message to continue."
        )
    if reason == "reported":
        return (
            "A reply was interrupted by a gateway restart. Automatic resume is off; "
            "send a message to continue."
        )
    return (
        f"A reply was interrupted by a gateway restart and could not be resumed "
        f"automatically ({attempts} attempts). Send a message to continue; tool "
        "results marked unknown should be checked before anything is repeated."
    )


#: A shell command that restarts, stops or updates the gateway itself.
_SELF_RESTART_COMMAND = re.compile(
    r"\b(?:agentos|gateway)\b[^\n;|&]*\b(?:restart|stop|update|upgrade)\b"
    r"|\buv\s+tool\s+(?:upgrade|install)\b[^\n;|&]*\buse-agent-os\b",
    re.IGNORECASE,
)


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def turn_requested_restart(tail: TurnTail) -> bool:
    """Whether the interrupted turn itself called for the restart that stopped it.

    True for a ``gateway`` tool call with ``action: restart`` or any other
    tool call (a shell command) that restarts, stops or updates the gateway.
    Resuming such a turn could restart the gateway again.
    """
    record = getattr(tail.progress_entry, "provider_messages", None)
    if isinstance(record, str):
        import json

        try:
            record = json.loads(record)
        except ValueError:
            return False
    messages = record.get("messages") if isinstance(record, dict) else None
    for message in messages if isinstance(messages, list) else []:
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name") or "")
            arguments = block.get("input")
            if name == "gateway" and isinstance(arguments, dict):
                if str(arguments.get("action") or "").strip().lower() == "restart":
                    return True
                continue
            if name in RESTART_SAFE_TOOL_ALLOW:
                continue
            if any(_SELF_RESTART_COMMAND.search(text) for text in _strings(arguments)):
                return True
    return False


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _details(record: Any) -> dict[str, Any]:
    details = getattr(record, "details", None)
    return dict(details) if isinstance(details, dict) else {}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _status_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _is_cron_session(session_key: str) -> bool:
    return session_key.startswith("cron:") or ":cron:" in session_key


@dataclass
class _Recovery:
    """One interrupted run being recovered (keyed by the interrupted run id)."""

    origin_task_id: str
    session_key: str
    state: RecoveryState
    reason: str
    attempts: int = 0
    restricted: bool = False
    unanswered_input: bool = False
    resume_task_ids: list[str] = field(default_factory=list)
    current_task_id: str | None = None
    cancelled: bool = False
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    runner: asyncio.Task[None] | None = None
    envelope: Any = None
    origin: Any = None

    def persisted(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "attempts": self.attempts,
            "restricted": self.restricted,
            "resume_task_ids": list(self.resume_task_ids),
            "updated_at": _now_ms(),
        }

    def summary(self) -> dict[str, Any]:
        return {
            "session_key": self.session_key,
            "run_id": self.origin_task_id,
            "state": self.state,
            "reason": self.reason,
            "attempts": self.attempts,
            "restricted": self.restricted,
        }


SleepFn = Callable[[float], Awaitable[None]]


class TurnRecovery:
    """Finds and resumes turns a restart or crash interrupted."""

    def __init__(
        self,
        *,
        storage: Any,
        session_manager: Any,
        task_runtime: Any,
        auto_resume: bool = True,
        channel_manager_ref: Callable[[], Any] | None = None,
        safe_mode_active: Callable[[], bool] | None = None,
        restart_requested_by: str | None = None,
        clock_ms: Callable[[], int] = _now_ms,
        backoff_s: Iterable[float] = BACKOFF_S,
        max_attempts: int = MAX_ATTEMPTS,
        max_age_ms: int = MAX_AGE_MS,
        concurrency: int = RECOVERY_LANE_CONCURRENCY,
    ) -> None:
        self._storage = storage
        self._session_manager = session_manager
        self._task_runtime = task_runtime
        self._auto_resume = auto_resume
        self._channel_manager_ref = channel_manager_ref or (lambda: None)
        self._safe_mode_active = safe_mode_active or (lambda: False)
        # Session that asked for the restart this boot follows (if any).
        self._restart_requested_by = (
            canonicalize_session_key(restart_requested_by) if restart_requested_by else None
        )
        self._clock_ms = clock_ms
        self._backoff_s = tuple(backoff_s) or (0.0,)
        self._max_attempts = max(1, int(max_attempts))
        self._max_age_ms = max_age_ms
        self._lane = asyncio.Semaphore(max(1, int(concurrency)))
        self._recoveries: dict[str, _Recovery] = {}
        self._by_session: dict[str, _Recovery] = {}
        self._scan_task: asyncio.Task[Any] | None = None
        self._background: set[asyncio.Future[Any]] = set()
        self._scanned = False
        self._stopping = False
        add_listener = getattr(task_runtime, "add_enqueue_listener", None)
        if callable(add_listener):
            add_listener(self._on_enqueue)

    # ── public surface ──────────────────────────────────────────────

    @property
    def auto_resume(self) -> bool:
        return self._auto_resume

    def start(self) -> asyncio.Task[Any]:
        """Scan in the background; resumes run in their own tasks."""
        if self._scan_task is None:
            self._scan_task = asyncio.create_task(self.scan(), name="turn_recovery.scan")
        return self._scan_task

    async def scan(self) -> list[dict[str, Any]]:
        """Find interrupted turns and start recovering them. Never raises."""
        try:
            await self._scan()
        except Exception:  # noqa: BLE001 - recovery must never break boot
            log.warning("turn_recovery.scan_failed", exc_info=True)
        finally:
            self._scanned = True
        return [recovery.summary() for recovery in self._recoveries.values()]

    async def wait_idle(self, timeout: float | None = None) -> None:
        """Wait for the scan and every running recovery (tests, shutdown)."""
        if self._scan_task is not None:
            await asyncio.wait_for(asyncio.shield(self._scan_task), timeout=timeout)
        runners = [r.runner for r in self._recoveries.values() if r.runner is not None]
        if runners:
            await asyncio.wait_for(
                asyncio.gather(*runners, return_exceptions=True), timeout=timeout
            )

    async def stop(self) -> None:
        """Stop before the runtime drains; unfinished recoveries resume next boot."""
        self._stopping = True
        tasks: list[asyncio.Task[Any]] = []
        if self._scan_task is not None and not self._scan_task.done():
            tasks.append(self._scan_task)
        for recovery in self._recoveries.values():
            runner = recovery.runner
            if runner is None or runner.done():
                continue
            if recovery.current_task_id is None:
                # Still in backoff: nothing of this attempt runs yet.
                tasks.append(runner)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task

    def snapshot(self) -> dict[str, Any]:
        """Counts for ``/ready`` and ``doctor``."""
        counts = dict.fromkeys(_STATES, 0)
        for recovery in self._recoveries.values():
            counts[recovery.state] = counts.get(recovery.state, 0) + 1
        return {
            "auto_resume": self._auto_resume,
            "scanned": self._scanned,
            "found": len(self._recoveries),
            **counts,
        }

    def recoveries(self) -> list[dict[str, Any]]:
        return [recovery.summary() for recovery in self._recoveries.values()]

    # ── scan ────────────────────────────────────────────────────────

    async def _scan(self) -> None:
        now = self._clock_ms()
        list_interrupted = getattr(self._storage, "list_interrupted_agent_tasks", None)
        if not callable(list_interrupted):
            return
        # Twice the age limit: a turn just past it is reported as expired.
        candidates = await list_interrupted(
            list(RECOVERABLE_TERMINAL_REASONS),
            since_ms=now - 2 * self._max_age_ms,
        )
        newest_by_session: dict[str, Any] = {}
        for record in candidates:
            newest_by_session.setdefault(record.session_key, record)
        resume_enabled = self._auto_resume and not self._safe_mode_active()
        for session_key, record in newest_by_session.items():
            if self._stopping:
                return
            try:
                await self._consider(record, now=now, resume_enabled=resume_enabled)
            except Exception:  # noqa: BLE001 - one session never blocks the others
                log.warning(
                    "turn_recovery.consider_failed",
                    session_key=session_key,
                    task_id=record.task_id,
                    exc_info=True,
                )
        if self._recoveries:
            log.info("turn_recovery.scanned", **self.snapshot())

    async def _consider(self, record: Any, *, now: int, resume_enabled: bool) -> None:
        session_key = record.session_key
        if session_key in self._by_session:
            return
        if not await self._is_newest_task(record):
            return  # later work on the session superseded it
        origin = await self._origin_of(record)
        if origin is None:
            return
        prior = _details(origin).get(RECOVERY_KEY)
        prior = prior if isinstance(prior, dict) else {}
        if prior.get("state") in _FINAL_STATES:
            return
        if not await self._eligible(origin):
            return

        recovery = _Recovery(
            origin_task_id=origin.task_id,
            session_key=session_key,
            state="pending",
            reason="",
            attempts=int(prior.get("attempts") or 0),
            restricted=bool(prior.get("restricted")),
            resume_task_ids=[str(t) for t in prior.get("resume_task_ids") or []],
            origin=origin,
        )
        self._recoveries[origin.task_id] = recovery
        self._by_session[session_key] = recovery

        started = origin.started_at or origin.created_at or now
        if now - int(started) > self._max_age_ms:
            await self._finish(recovery, "expired", "max_age", hint=True)
            return

        tail = await self._tail(session_key)
        action, reason = decide_recovery(tail)
        recovery.reason = reason
        if action == "close":
            await self._finish(recovery, "closed", reason)
            return
        if session_key == self._restart_requested_by or turn_requested_restart(tail):
            # The turn asked for the restart that stopped it: resuming it could
            # restart again. Report only.
            await self._finish(recovery, "reported", "restart_requested", hint=True)
            return
        recovery.restricted = recovery.restricted or action == "resume_restricted"
        recovery.unanswered_input = tail.status == "unanswered_input" and any(
            getattr(entry, "provenance_source_tool", None) != RECOVERY_SOURCE_TOOL
            for entry in tail.trailing_user_entries
        )
        if not resume_enabled:
            await self._finish(recovery, "reported", reason, hint=True)
            return
        if recovery.attempts >= self._max_attempts:
            await self._finish(recovery, "tombstoned", "attempts_exhausted", hint=True)
            return
        envelope, refused = await self._envelope_for(origin, recovery)
        if envelope is None:
            if refused == "pairing_revoked":
                # Never answer a sender whose pairing was revoked meanwhile.
                await self._finish(recovery, "closed", refused)
            else:
                await self._finish(recovery, "reported", refused, hint=True)
            return
        recovery.envelope = envelope
        await self._persist(recovery)
        recovery.runner = asyncio.create_task(
            self._run(recovery), name=f"turn_recovery:{session_key}"
        )

    async def _is_newest_task(self, record: Any) -> bool:
        list_tasks = getattr(self._storage, "list_agent_tasks", None)
        if not callable(list_tasks):
            return True
        newest = await list_tasks(session_key=record.session_key, limit=1, newest_first=True)
        return not newest or newest[-1].task_id == record.task_id

    async def _origin_of(self, record: Any) -> Any:
        marker = recovery_marker(_details(record).get("metadata"))
        origin_id = marker.get("origin_task_id") if marker else None
        if not isinstance(origin_id, str) or not origin_id or origin_id == record.task_id:
            return record
        get_task = getattr(self._storage, "get_agent_task", None)
        origin = await get_task(origin_id) if callable(get_task) else None
        return origin if origin is not None else record

    async def _eligible(self, origin: Any) -> bool:
        if str(origin.source_kind) not in _USER_FACING_SOURCES:
            return False
        if str(origin.run_kind) in _EXCLUDED_RUN_KINDS:
            return False
        if _is_cron_session(origin.session_key):
            return False
        session = await self._get_session(origin.session_key)
        if session is None:
            return False
        origin_info = getattr(session, "origin", None)
        if isinstance(origin_info, dict) and origin_info.get("kind") == "acp":
            return False
        return True

    async def _get_session(self, session_key: str) -> Any:
        get_session = getattr(self._session_manager, "get_session", None)
        if not callable(get_session):
            return None
        try:
            return await get_session(session_key)
        except Exception:  # noqa: BLE001
            return None

    async def _tail(self, session_key: str) -> TurnTail:
        rows: list[Any] = []
        try:
            rows = list(
                await self._session_manager.get_recent_transcript(
                    session_key, _TRANSCRIPT_TAIL_ROWS
                )
            )
        except Exception:  # noqa: BLE001
            log.debug("turn_recovery.transcript_read_failed", session_key=session_key)
        return classify_transcript_tail(rows)

    async def _envelope_for(self, origin: Any, recovery: _Recovery) -> tuple[Any, str]:
        """The resumed turn's envelope, or ``None`` and why it may not run."""
        details = _details(origin)
        if str(origin.source_kind) == "channel" and not isinstance(details.get("route"), dict):
            # Written before routes were recorded: neither the chat to answer
            # nor the pairing grant to check is known.
            return None, "route_unknown"
        original_metadata = details.get("metadata")
        metadata = (
            {key: value for key, value in (original_metadata or {}).items() if key != RECOVERY_KEY}
            if isinstance(original_metadata, dict)
            else {}
        )
        metadata[RECOVERY_KEY] = {
            "origin_task_id": origin.task_id,
            "restricted": recovery.restricted,
        }
        envelope = envelope_from_task(
            origin,
            input_provenance={
                "kind": "internal_system",
                "source_tool": RECOVERY_SOURCE_TOOL,
                "origin_task_id": origin.task_id,
            },
            metadata=metadata,
        )
        route = _as_dict(details.get("route"))
        grant = route.get("channel_admission")
        if isinstance(grant, dict):
            # The turn was admitted under a channel pairing grant: only resume
            # while that grant still stands, and keep checking it in the turn.
            admission, validator = self._revalidate_admission(grant)
            if admission is None or validator is None:
                return None, "pairing_revoked"
            envelope.runtime_state.channel_admission = admission
            envelope.runtime_state.channel_admission_validator = validator
        return envelope, ""

    def _revalidate_admission(self, grant: dict[str, Any]) -> tuple[Any, Any]:
        from agentos.channel_pairing import ChannelAdmission

        try:
            admission = ChannelAdmission(
                channel_name=str(grant.get("channel_name") or ""),
                sender_id=str(grant.get("sender_id") or ""),
                grant_id=str(grant.get("grant_id") or ""),
                grant_version=int(grant.get("grant_version") or 1),
            )
        except (TypeError, ValueError):
            return None, None
        manager = self._channel_manager_ref()
        get_channel = getattr(manager, "get", None)
        adapter = get_channel(admission.channel_name) if callable(get_channel) else None
        store = getattr(adapter, "pairing_store", None)
        validator = getattr(store, "validate_admission", None)
        if not callable(validator):
            return None, None
        try:
            valid = bool(validator(admission))
        except Exception:  # noqa: BLE001
            valid = False
        return (admission, validator) if valid else (None, None)

    # ── resume ──────────────────────────────────────────────────────

    async def _run(self, recovery: _Recovery) -> None:
        try:
            async with self._lane:
                await self._attempts(recovery)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning(
                "turn_recovery.run_failed",
                session_key=recovery.session_key,
                run_id=recovery.origin_task_id,
                exc_info=True,
            )

    async def _attempts(self, recovery: _Recovery) -> None:
        while recovery.attempts < self._max_attempts:
            delay = self._backoff_s[min(recovery.attempts, len(self._backoff_s) - 1)]
            if delay > 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(recovery.wake.wait(), timeout=delay)
            if recovery.cancelled:
                await self._finish(recovery, "cancelled", "user_message")
                return
            recovery.attempts += 1
            recovery.state = "running"
            await self._persist(recovery)
            watermark = await self._transcript_length(recovery.session_key)
            try:
                handle = await self._task_runtime.enqueue(
                    recovery.envelope,
                    continuation_message(
                        restricted=recovery.restricted,
                        unanswered_input=recovery.unanswered_input,
                    ),
                    mode="followup",
                    run_kind=str(recovery.origin.run_kind or "default"),
                    update_envelope_cache=False,
                    idempotency_key=f"{RECOVERY_KEY}:{recovery.origin_task_id}",
                )
            except TaskRuntimeNotAdmittingError:
                return  # the gateway is stopping; the next boot carries on
            recovery.current_task_id = handle.task_id
            recovery.resume_task_ids.append(handle.task_id)
            await self._persist(recovery)
            log.info(
                "turn_recovery.resuming",
                session_key=recovery.session_key,
                run_id=recovery.origin_task_id,
                task_id=handle.task_id,
                attempt=recovery.attempts,
                restricted=recovery.restricted,
            )
            record = await self._task_runtime.wait(handle.task_id)
            recovery.current_task_id = None
            status = _status_value(getattr(record, "status", None))
            if status == AgentTaskStatus.SUCCEEDED.value:
                await self._finish(recovery, "resumed", recovery.reason)
                await self._deliver_reply(recovery, watermark)
                return
            if status == AgentTaskStatus.ABANDONED.value:
                return  # stopped by a shutdown; the next boot carries on
            if status == AgentTaskStatus.CANCELLED.value or recovery.cancelled:
                await self._finish(recovery, "cancelled", "cancelled")
                return
            log.warning(
                "turn_recovery.attempt_failed",
                session_key=recovery.session_key,
                run_id=recovery.origin_task_id,
                attempt=recovery.attempts,
                status=status,
                terminal_reason=getattr(record, "terminal_reason", None),
            )
        await self._finish(recovery, "tombstoned", "attempts_exhausted", hint=True)

    def _on_enqueue(self, record: Any) -> None:
        """A new task on a session with a recovery that has not started: cancel it."""
        recovery = self._by_session.get(getattr(record, "session_key", ""))
        if recovery is None or recovery.state not in ("pending", "running"):
            return
        details = _details(record)
        if recovery_marker(details.get("metadata")) is not None:
            return  # the recovery's own turn
        provenance = details.get("input_provenance")
        kind = provenance.get("kind") if isinstance(provenance, dict) else None
        if str(getattr(record, "source_kind", "")) not in _USER_FACING_SOURCES or kind in (
            "internal_system",
            "inter_session",
        ):
            return
        current = recovery.current_task_id
        if current is not None:
            is_started = getattr(self._task_runtime, "is_task_started", None)
            if callable(is_started) and is_started(current):
                return  # already answering; the user's turn queues behind it
        recovery.cancelled = True
        recovery.wake.set()
        if current is not None:
            cancel = asyncio.ensure_future(
                self._task_runtime.cancel(
                    task_id=current,
                    source="restart_recovery",
                    reason="user_message",
                )
            )
            self._background.add(cancel)
            cancel.add_done_callback(self._background.discard)
        log.info(
            "turn_recovery.cancelled_by_user_message",
            session_key=recovery.session_key,
            run_id=recovery.origin_task_id,
        )

    # ── bookkeeping ─────────────────────────────────────────────────

    async def _finish(
        self,
        recovery: _Recovery,
        state: RecoveryState,
        reason: str,
        *,
        hint: bool = False,
    ) -> None:
        recovery.state = state
        if reason:
            recovery.reason = reason
        await self._persist(recovery)
        if hint:
            if recovery.reason == "restart_requested":
                label = "restart_requested"
            elif state in ("expired", "reported"):
                label = state
            else:
                label = "attempts"
            await self._leave_hint(
                recovery, tombstone_text(attempts=recovery.attempts, reason=label)
            )
        log.info(
            "turn_recovery.finished",
            session_key=recovery.session_key,
            run_id=recovery.origin_task_id,
            state=state,
            reason=recovery.reason,
            attempts=recovery.attempts,
        )

    async def _persist(self, recovery: _Recovery) -> None:
        update = getattr(self._storage, "update_agent_task", None)
        if not callable(update):
            return
        details = _details(recovery.origin)
        details[RECOVERY_KEY] = recovery.persisted()
        try:
            recovery.origin = await update(recovery.origin_task_id, details=details)
        except Exception:  # noqa: BLE001 - bookkeeping is best-effort
            log.warning(
                "turn_recovery.persist_failed",
                run_id=recovery.origin_task_id,
                exc_info=True,
            )

    async def _transcript_length(self, session_key: str) -> int:
        try:
            rows = await self._session_manager.get_transcript(session_key)
        except Exception:  # noqa: BLE001
            return 0
        return len(rows)

    async def _leave_hint(self, recovery: _Recovery, text: str) -> None:
        append = getattr(self._session_manager, "append_message", None)
        if callable(append):
            try:
                await append(
                    recovery.session_key,
                    role="system",
                    content=text,
                    provenance={
                        "kind": "internal_system",
                        "source_tool": RECOVERY_SOURCE_TOOL,
                    },
                )
            except Exception:  # noqa: BLE001
                log.debug("turn_recovery.hint_write_failed", exc_info=True)
        await self._send_to_channel(recovery, text)

    async def _deliver_reply(self, recovery: _Recovery, watermark: int) -> None:
        """Send the resumed turn's reply to the chat the turn came from.

        Web and CLI clients get the reply as they get any turn: the run's
        stream events on the session and the transcript. A channel turn's
        reply is normally sent by the dispatch that admitted it, which died
        with the old process, so it is sent here.
        """
        if not self._channel_target(recovery):
            return
        try:
            rows = await self._session_manager.get_transcript(recovery.session_key)
        except Exception:  # noqa: BLE001
            return
        text = ""
        for row in reversed(list(rows)[watermark:]):
            content = getattr(row, "content", None)
            if getattr(row, "role", None) == "assistant" and isinstance(content, str) and content:
                text = content
                break
        if not text:
            return
        from agentos.gateway.channel_dispatch import (
            _artifact_fallback_lines,
            _split_assistant_artifact_content,
            _strip_artifact_markers_from_channel_text,
        )

        body, artifacts = _split_assistant_artifact_content(text)
        body = _strip_artifact_markers_from_channel_text(body)
        lines = _artifact_fallback_lines(artifacts)
        if lines:
            body = "\n\n".join(part for part in (body, "\n".join(lines)) if part)
        if body:
            await self._send_to_channel(recovery, body)

    def _channel_target(self, recovery: _Recovery) -> dict[str, str] | None:
        envelope = recovery.envelope
        if envelope is None:
            if not isinstance(_details(recovery.origin).get("route"), dict):
                return None
            envelope = envelope_from_task(recovery.origin, input_provenance={}, metadata={})
        if str(getattr(envelope.source_kind, "value", envelope.source_kind)) != "channel":
            return None
        target = envelope.reply_target
        if target is not None and target.kind == "channel":
            name, to = target.channel_name, target.to
            thread, account = target.thread_id, target.account_id
        else:
            name, to = envelope.channel_name, envelope.channel_id
            thread, account = envelope.thread_id, envelope.account_id
        if not name or not (to or thread):
            return None
        return {
            "target": str(name),
            "to": str(to or ""),
            "thread_id": str(thread or ""),
            "account_id": str(account or ""),
        }

    async def _send_to_channel(self, recovery: _Recovery, text: str) -> bool:
        target = self._channel_target(recovery)
        manager = self._channel_manager_ref()
        resolve = getattr(manager, "resolve_delivery_target", None)
        if target is None or not callable(resolve):
            return False
        from agentos.channels.addressing import build_outgoing_for_target

        try:
            resolved = resolve(**target)
            adapter = getattr(resolved, "adapter", None) if getattr(resolved, "ok", False) else None
            if adapter is None:
                return False
            message = build_outgoing_for_target(
                channel_type=resolved.channel_type,
                to=resolved.to,
                thread_id=resolved.thread_id,
                text=text,
            )
            await adapter.send(message)
            return True
        except Exception:  # noqa: BLE001 - delivery failure is logged, not raised
            log.warning(
                "turn_recovery.channel_delivery_failed",
                session_key=recovery.session_key,
                exc_info=True,
            )
            return False


# ---------------------------------------------------------------------------
# Process-wide handle (doctor, /ready)
# ---------------------------------------------------------------------------

_current: TurnRecovery | None = None


def set_turn_recovery(recovery: TurnRecovery | None) -> None:
    global _current
    _current = recovery


def get_turn_recovery() -> TurnRecovery | None:
    return _current


__all__ = [
    "BACKOFF_S",
    "MAX_AGE_MS",
    "MAX_ATTEMPTS",
    "RECOVERABLE_TERMINAL_REASONS",
    "RECOVERY_KEY",
    "RECOVERY_LANE_CONCURRENCY",
    "RESTART_SAFE_TOOL_ALLOW",
    "TurnRecovery",
    "apply_restart_recovery_tool_policy",
    "continuation_message",
    "decide_recovery",
    "get_turn_recovery",
    "recovery_marker",
    "restart_safe_tool_names",
    "set_turn_recovery",
    "tombstone_text",
    "turn_requested_restart",
]
