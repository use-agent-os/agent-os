"""CRUD operations for the scheduler — delegation layer between engine and store."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from agentos.permissions import CRON_ELEVATED_MODES, normalize_cron_elevated
from agentos.session.keys import normalize_agent_id
from agentos.tools.policy_config import normalize_tool_profile

from .delivery import validate_webhook_url
from .jobs import _next_run
from .parser import parse_cron, parse_iso_at, validate_tz
from .payloads import (
    normalize_contract,
    normalize_origin_session_key,
    payload_agent_id,
    payload_script,
)
from .persistence import JobStore
from .scripts import JOB_ID_PLACEHOLDER, substitute_job_id
from .stagger import compute_jitter
from .types import (
    CronJob,
    CronWakeMode,
    DeliveryConfig,
    DeliveryMode,
    JobExecution,
    JobStatus,
    ScheduleKind,
    SessionTarget,
)

# Hard cap on job timeout_seconds. A single cron create/update with a huge
# value would hold a model turn open indefinitely (scheduler DoS); negative
# values make asyncio.wait_for run the handler with no wait at all.
_MAX_JOB_TIMEOUT_SECONDS = 24 * 60 * 60  # 24h


def _resolve_script_placeholder(job: CronJob) -> None:
    """Replace ``{job_id}`` in the job's script path with the job's own id.

    A monitor that keeps its files in a directory named after its job cannot
    write that name when it creates the job, because the id is minted by the
    create. Substituting here — the last thing before the job is persisted, on
    the path every surface takes — means no store ever holds the placeholder,
    so there is no window in which a live job points at a path it will not keep.
    """
    script = payload_script(job.payload)
    if JOB_ID_PLACEHOLDER not in script:
        return
    job.payload = {**job.payload, "script": substitute_job_id(script, job.id)}


def _normalized_tool_policy(
    tool_policy: dict[str, Any] | None,
    *,
    handler_key: str,
) -> dict[str, Any]:
    """Canonicalise ``tool_policy["profile"]`` and ``["elevated"]``, and gate
    elevation to agent turns.

    This runs here, not only at the RPC boundary, because the ``cron`` builtin
    tool hands its ``tool_policy`` argument straight to ``add`` — validating
    only on the wire would leave that path open.

    ``profile`` is checked for the same reason it is checked at all: the name is
    otherwise resolved for the first time inside the run, so an unknown one
    creates a job that stores cleanly and then fails on every firing.

    Only ``agent_run`` jobs are allowed to carry elevation. A ``system_event``
    job may be serviced by HeartbeatLoop, which builds its own read-only
    ToolContext and never sees ``job.tool_policy``, so elevation would be
    honoured on one path and silently dropped on the other. A ``script_run`` job
    has no agent turn at all — the file is the job — so there is nothing for a
    tool policy to govern.
    """

    policy = dict(tool_policy or {})
    if "profile" in policy:
        policy["profile"] = normalize_tool_profile(policy["profile"])
    if "elevated" not in policy:
        return policy
    mode = normalize_cron_elevated(policy["elevated"])
    if mode is None:
        policy.pop("elevated")
        return policy
    if mode in CRON_ELEVATED_MODES and handler_key != "agent_run":
        raise ValueError(
            "cron elevation is only supported for agent_turn jobs; reminder, "
            "system_event and script jobs never run an agent turn with the job's "
            "tool policy"
        )
    policy["elevated"] = mode
    return policy


def _validate_structured_schedule(
    kind: ScheduleKind | str,
    value: str,
) -> tuple[ScheduleKind, str]:
    """Validate (kind, value) per-kind and return canonical (kind, value).

    Raises ``ValueError`` (or subclasses) on invalid input. ``value`` is always
    returned as a string to match how the EVERY interval is stored elsewhere.
    """
    if isinstance(kind, str):
        kind = ScheduleKind(kind)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("schedule_value must be a non-empty string")
    value = value.strip()
    if kind == ScheduleKind.CRON:
        parse_cron(value)
        return kind, value
    if kind == ScheduleKind.AT:
        parse_iso_at(value)
        return kind, value
    if kind == ScheduleKind.EVERY:
        try:
            seconds = int(value)
        except ValueError as exc:
            raise ValueError(
                f"schedule_value for kind=every must be integer seconds; got {value!r}"
            ) from exc
        if seconds < 1:
            raise ValueError("schedule_value for kind=every must be >= 1 second")
        return kind, str(seconds)
    raise ValueError(f"Unsupported schedule_kind: {kind!r}")


def _coerce_wake_mode(value: CronWakeMode | str) -> CronWakeMode:
    if isinstance(value, CronWakeMode):
        return value
    return CronWakeMode(str(value or CronWakeMode.NOW.value).strip().lower())


def _delivery_requested(delivery: DeliveryConfig | None) -> bool:
    return delivery is not None and delivery.mode != DeliveryMode.NONE


def _validate_main_agent(payload: dict | None, session_target: SessionTarget) -> None:
    if session_target != SessionTarget.MAIN:
        return
    agent_id = payload_agent_id(payload)
    if normalize_agent_id(agent_id) != "main":
        raise ValueError(
            'cron: sessionTarget "main" is only valid for the default agent. '
            'Use sessionTarget "isolated" with an agent_turn payload for non-default agents '
            f"(agent_id: {agent_id})"
        )


def _normalize_delivery_for_target(
    *,
    session_target: SessionTarget,
    delivery: DeliveryConfig,
    explicit_delivery: bool,
) -> DeliveryConfig:
    if delivery is not None and delivery.mode == DeliveryMode.WEBHOOK:
        validate_webhook_url(delivery.webhook_url)
    fd = delivery.failure_destination if delivery is not None else None
    if fd is not None and fd.mode == DeliveryMode.WEBHOOK:
        validate_webhook_url(fd.webhook_url)
    if session_target != SessionTarget.MAIN:
        return delivery
    # Webhook delivery is allowed for any sessionTarget — the heartbeat
    # pipeline ignores it and the webhook POST is independent of session.
    if delivery is not None and delivery.mode == DeliveryMode.WEBHOOK:
        return delivery
    if _delivery_requested(delivery):
        if explicit_delivery:
            raise ValueError(
                'cron channel delivery config is only supported for sessionTarget="isolated"'
            )
        return DeliveryConfig()
    return delivery


class SchedulerOps:
    """CRUD + validation layer over the JobStore."""

    def __init__(
        self,
        store: JobStore,
        max_jitter: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._max_jitter = max_jitter
        self._clock = clock

    def _now(self) -> datetime:
        now = self._clock() if self._clock is not None else datetime.now().astimezone()
        if now.tzinfo is None:
            return now.astimezone()
        return now

    async def add(
        self,
        name: str,
        *,
        schedule_kind: ScheduleKind | str,
        schedule_value: str,
        schedule_tz: str = "",
        handler_key: str = "",
        payload: dict | None = None,
        session_target: SessionTarget = SessionTarget.ISOLATED,
        session_key: str = "",
        timeout_seconds: float = 600.0,
        wake_mode: CronWakeMode | str = CronWakeMode.NOW,
        max_retries: int = 3,
        delivery: DeliveryConfig | None = None,
        origin_session_key: str = "",
        tool_policy: dict[str, Any] | None = None,
        tz: str = "",
        jitter_seconds: float | None = None,
        creator_session_key: str = "",
        creator_sender_id: str = "",
    ) -> CronJob:
        """Validate the structured schedule, compute jitter, persist a new CronJob.

        ``schedule_kind`` + ``schedule_value`` are required; the value is
        validated per kind via ``parse_cron`` / ``parse_iso_at`` / integer
        check. No natural-language detection.

        ``jitter_seconds`` controls stagger:
          * ``None`` (default) → auto-computed via compute_jitter (legacy behaviour).
          * ``0`` → exact timing, no stagger.
          * ``>0`` → explicit fixed offset.
        """
        now_local = self._now()
        kind, cron_expr = _validate_structured_schedule(schedule_kind, schedule_value)
        schedule_raw = cron_expr
        tz = (schedule_tz or tz or "").strip()
        validate_tz(tz)
        if jitter_seconds is None:
            jitter = compute_jitter(handler_key + name, self._max_jitter)
        else:
            jitter = max(0.0, float(jitter_seconds))
        now = now_local.astimezone(UTC)

        # Coerce string to enum if needed
        if isinstance(session_target, str):
            session_target = SessionTarget(session_target)
        wake_mode = _coerce_wake_mode(wake_mode)

        # If sessionTarget=current is requested but no binding is available,
        # fall back to ISOLATED instead of failing creation. Headless cron
        # callers (no session context) get an isolated run rather than a hard
        # error.
        if session_target == SessionTarget.CURRENT and not session_key and not origin_session_key:
            session_target = SessionTarget.ISOLATED

        origin_session_key = normalize_origin_session_key(session_target, origin_session_key)
        handler_key, normalized_payload, session_target, session_key = normalize_contract(
            handler_key=handler_key,
            payload=payload,
            session_target=session_target,
            session_key=session_key,
            origin_session_key=origin_session_key,
            strict=True,
        )
        _validate_main_agent(normalized_payload, session_target)
        # After normalize_contract, so the elevation gate sees the resolved
        # handler_key rather than whatever the caller passed in.
        normalized_tool_policy = _normalized_tool_policy(tool_policy, handler_key=handler_key)
        delivery = _normalize_delivery_for_target(
            session_target=session_target,
            delivery=delivery or DeliveryConfig(),
            explicit_delivery=delivery is not None,
        )

        # Bound timeout_seconds: a negative or absurdly large value would make
        # asyncio.wait_for run the handler with no wait at all (<=0) or hold a
        # model turn open for years (huge), a reliable scheduler DoS via a
        # single cron create/update call.
        if timeout_seconds is None or timeout_seconds < 1:
            raise ValueError(
                f"timeout_seconds must be >= 1, got {timeout_seconds!r}"
            )
        if timeout_seconds > _MAX_JOB_TIMEOUT_SECONDS:
            raise ValueError(
                f"timeout_seconds must be <= {_MAX_JOB_TIMEOUT_SECONDS}, "
                f"got {timeout_seconds!r}"
            )

        job = CronJob(
            name=name,
            schedule_raw=schedule_raw,
            schedule_kind=kind,
            cron_expr=cron_expr,
            tz=tz,
            handler_key=handler_key,
            payload=normalized_payload,
            session_target=session_target,
            session_key=session_key,
            timeout_seconds=timeout_seconds,
            wake_mode=wake_mode,
            max_retries=max_retries,
            jitter_seconds=jitter,
            delivery=delivery,
            origin_session_key=origin_session_key,
            tool_policy=normalized_tool_policy,
            creator_session_key=creator_session_key or "",
            creator_sender_id=creator_sender_id or "",
        )

        if kind == ScheduleKind.AT:
            job.delete_after_run = True
            at_dt = datetime.fromisoformat(cron_expr)
            # An AT one-shot whose timestamp is in the past is due on the very
            # next tick, so it would fire immediately with whatever prompt the
            # operator typed — a stale payload running right after create is
            # never what a one-shot is for (use EVERY/cron for that). Refuse
            # with a small skew tolerance instead of silently scheduling a
            # job that is already due.
            if at_dt < now - timedelta(seconds=5):
                raise ValueError(
                    f"AT schedule is in the past ({cron_expr}); a one-shot job "
                    "cannot fire `now`. Use a cron/`every` schedule for repeating "
                    "work, or an AT timestamp in the future."
                )
            job.next_run_at = at_dt
        elif kind == ScheduleKind.EVERY and cron_expr.isdigit():
            # Anchor-based interval: record the anchor so subsequent fires
            # align to it rather than drifting with each run.
            job.anchor_at = now
            job.next_run_at = now + timedelta(seconds=int(cron_expr))
        else:
            # CRON or EVERY with cron expression: scan forward.
            # apply_jitter=True so the stagger offset is baked into the very
            # first next_run_at only; post-execution reschedules use the
            # default apply_jitter=False to avoid cumulative drift.
            job.next_run_at = _next_run(job, now, apply_jitter=True)

        _resolve_script_placeholder(job)
        await self._store.save(job)
        return job

    async def update(self, job_id: str, **patch) -> CronJob | None:
        """Apply a partial update to an existing job. Returns None if not found."""
        job = await self._store.get(job_id)
        if job is None:
            return None

        now_local = self._now()
        now = now_local.astimezone(UTC)
        payload_patch = patch.pop("payload", None)
        delivery_was_patched = "delivery" in patch

        if "tz" in patch:
            raw_tz = (patch.pop("tz") or "").strip()
            validate_tz(raw_tz)
            job.tz = raw_tz

        structured_kind = patch.pop("schedule_kind", None)
        structured_value = patch.pop("schedule_value", None)
        structured_tz = patch.pop("schedule_tz", None)
        if structured_kind is not None and structured_value is not None:
            kind, cron_expr = _validate_structured_schedule(structured_kind, structured_value)
            if structured_tz is not None:
                raw_tz = (structured_tz or "").strip()
                validate_tz(raw_tz)
                job.tz = raw_tz
            job.schedule_raw = cron_expr
            job.schedule_kind = kind
            job.cron_expr = cron_expr
            # `add` sets delete_after_run for one-shot jobs only. Rescheduling a
            # one-shot onto a recurring expression has to clear it too, or the
            # edited job deletes itself after its first fire.
            job.delete_after_run = kind == ScheduleKind.AT
            if kind == ScheduleKind.AT:
                job.anchor_at = None
                at_dt = datetime.fromisoformat(cron_expr)
                # Same past-timestamp guard as `add`: a one-shot being edited
                # onto a past time is due on the next tick and would fire
                # immediately with a stale payload.
                if at_dt < now - timedelta(seconds=5):
                    raise ValueError(
                        f"AT schedule is in the past ({cron_expr}); a one-shot "
                        "job cannot fire `now`."
                    )
                job.next_run_at = at_dt
            elif kind == ScheduleKind.EVERY:
                job.anchor_at = now
                job.next_run_at = now + timedelta(seconds=int(cron_expr))
            else:
                job.anchor_at = None
                job.next_run_at = _next_run(job, now)
        elif "schedule_raw" in patch:
            raise ValueError(
                "ops.update no longer accepts schedule_raw; "
                "pass schedule_kind + schedule_value instead"
            )

        for field in ("name", "timeout_seconds", "enabled", "origin_session_key"):
            if field in patch:
                if field == "timeout_seconds":
                    value = patch[field]
                    if value is None or value < 1 or value > _MAX_JOB_TIMEOUT_SECONDS:
                        raise ValueError(
                            f"timeout_seconds must be 1..{_MAX_JOB_TIMEOUT_SECONDS}, "
                            f"got {value!r}"
                        )
                setattr(job, field, patch.pop(field))
        # Validated after normalize_contract below: a patch that converts the
        # job's kind also moves its handler_key, and the elevation rule is
        # handler-specific. Checking it here would judge the new policy against
        # the outgoing handler.
        tool_policy_patched = "tool_policy" in patch
        tool_policy_value = patch.pop("tool_policy", None)
        if "wake_mode" in patch:
            raw_wake_mode = patch.pop("wake_mode")
            job.wake_mode = _coerce_wake_mode(raw_wake_mode)

        if "session_target" in patch:
            raw_target = patch.pop("session_target")
            job.session_target = (
                raw_target if isinstance(raw_target, SessionTarget) else SessionTarget(raw_target)
            )
        if "session_key" in patch:
            job.session_key = patch.pop("session_key") or ""

        if payload_patch:
            # A patch that names its `kind` is a complete, already-normalized
            # payload from the RPC layer — take it whole. Merging it would make
            # optional keys unremovable: dropping a job's pre-run script sends a
            # payload without `script`, and a merge would resurrect the old one.
            # Partial patches (legacy callers touching one field) still merge.
            if payload_patch.get("kind"):
                job.payload = dict(payload_patch)
            else:
                job.payload = {**job.payload, **payload_patch}
        if "delivery" in patch:
            job.delivery = patch.pop("delivery")

        (
            job.handler_key,
            job.payload,
            job.session_target,
            job.session_key,
        ) = normalize_contract(
            handler_key=job.handler_key,
            payload=job.payload,
            session_target=job.session_target,
            session_key=job.session_key,
            origin_session_key=job.origin_session_key,
            strict=True,
        )
        _validate_main_agent(job.payload, job.session_target)
        if tool_policy_patched:
            job.tool_policy = _normalized_tool_policy(
                tool_policy_value,
                handler_key=job.handler_key,
            )
        elif job.tool_policy.get("elevated") and job.handler_key != "agent_run":
            # A kind conversion can strand elevation on a handler that never
            # runs an agent turn — a shape `add` refuses to create. Dropping it
            # is a privilege reduction, so it needs no ceremony; keeping it
            # would leave a job elevated on paper and read-only in practice.
            job.tool_policy = {k: v for k, v in job.tool_policy.items() if k != "elevated"}
        job.delivery = _normalize_delivery_for_target(
            session_target=job.session_target,
            delivery=job.delivery,
            explicit_delivery=delivery_was_patched,
        )
        job.origin_session_key = normalize_origin_session_key(
            job.session_target,
            job.origin_session_key,
        )

        job.updated_at = now
        _resolve_script_placeholder(job)
        await self._store.save(job)
        return job

    async def remove(self, job_id: str) -> bool:
        """Delete a job. Returns True if it existed."""
        job = await self._store.get(job_id)
        if job is None:
            return False
        await self._store.delete(job_id)
        return True

    async def pause(self, job_id: str) -> CronJob | None:
        """Set job status to PAUSED. Returns None if not found."""
        job = await self._store.get(job_id)
        if job is None:
            return None
        job.status = JobStatus.PAUSED
        job.updated_at = datetime.now(UTC)
        await self._store.save(job)
        return job

    async def resume(self, job_id: str) -> CronJob | None:
        """Set job status to PENDING and recompute next_run_at. Returns None if not found."""
        job = await self._store.get(job_id)
        if job is None:
            return None

        now = datetime.now(UTC)
        job.status = JobStatus.PENDING
        job.updated_at = now

        if job.schedule_kind == ScheduleKind.AT:
            # Keep existing next_run_at for one-shot jobs
            pass
        elif job.schedule_kind == ScheduleKind.EVERY and job.cron_expr.isdigit():
            # Use anchor-aligned next_run when an anchor exists; otherwise
            # match the historical "now + interval" behaviour.
            if job.anchor_at is not None:
                job.next_run_at = _next_run(job, now)
            else:
                job.next_run_at = now + timedelta(seconds=int(job.cron_expr))
        else:
            job.next_run_at = _next_run(job, now)

        await self._store.save(job)
        return job

    async def get(self, job_id: str) -> CronJob | None:
        """Retrieve a job by ID."""
        return await self._store.get(job_id)

    async def list_all(self) -> list[CronJob]:
        """Return all non-deleted jobs."""
        return await self._store.list_active()

    async def get_runs(self, job_id: str, limit: int = 20) -> list[JobExecution]:
        """Return recent execution records for a job."""
        return await self._store.list_executions(job_id, limit)

    async def get_run(self, job_id: str, run_id: str | None = None) -> JobExecution | None:
        """Return one execution record — the latest when *run_id* is omitted."""
        return await self._store.get_execution(job_id, run_id)
