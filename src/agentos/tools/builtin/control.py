"""Cron scheduler and gateway-control tools."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Protocol

import structlog

from agentos.scheduler.delivery_targets import validate_channel_target
from agentos.scheduler.payloads import (
    AGENT_TURN_KIND,
    REMINDER_KIND,
    SCRIPT_KIND,
    SYSTEM_EVENT_KIND,
    make_agent_turn_payload,
    make_reminder_payload,
    make_script_payload,
    make_system_event_payload,
    payload_agent_id,
    payload_args,
    payload_kind,
    payload_script,
    payload_text,
    payload_workdir,
)
from agentos.scheduler.prompt_safety import scan_cron_prompt as _scan_cron_prompt
from agentos.scheduler.schedule_normalizer import coerce_schedule_from_params
from agentos.scheduler.scripts import (
    ScriptPathError,
    normalize_script_value,
    resolve_script_path,
    validate_script_path,
)
from agentos.scheduler.types import (
    DeliveryConfig,
    DeliveryMode,
    ReplyTargetSnapshot,
    ScheduleKind,
    SessionTarget,
)
from agentos.tools.registry import tool
from agentos.tools.types import SafeToolError, ToolError

log = structlog.get_logger(__name__)

_VALID_CRON_ACTIONS = ("list", "add", "get", "update", "remove", "run", "runs")
_VALID_JOB_KINDS = (REMINDER_KIND, SYSTEM_EVENT_KIND, AGENT_TURN_KIND, SCRIPT_KIND)
_VALID_SESSION_TARGETS = ("main", "isolated", "current", "session")

# Run history is the only record of what a script job printed, and a watcher's
# stdout can be arbitrarily long. These bounds keep "what did it do last night?"
# from spending the context window on one answer.
_CRON_RUNS_DEFAULT_LIMIT = 5
_CRON_RUNS_MAX_LIMIT = 20
_CRON_RUN_OUTPUT_MAX_CHARS = 2000


_VALID_GATEWAY_ACTIONS = ("restart", "config_get", "config_set")


class _SchedulerProtocol(Protocol):
    async def list_jobs(self) -> list[Any]: ...

    async def add_job(
        self,
        name: str,
        *,
        schedule_kind: Any,
        schedule_value: str,
        schedule_tz: str = "",
        handler_key: str = "agent_run",
        payload: dict[Any, Any] | None = None,
        session_target: SessionTarget = SessionTarget.ISOLATED,
        session_key: str = "",
        timeout_seconds: float = 600.0,
        wake_mode: Any = "now",
        max_retries: int = 3,
        origin_session_key: str = "",
        delivery: DeliveryConfig | None = None,
        tool_policy: dict[str, Any] | None = None,
        tz: str = "",
        jitter_seconds: float | None = None,
        creator_session_key: str = "",
        creator_sender_id: str = "",
    ) -> Any: ...

    async def update_job(self, job_id: str, **patch: Any) -> Any: ...

    async def get_job(self, job_id: str) -> Any | None: ...

    async def pause_job(self, job_id: str) -> Any | None: ...

    async def resume_job(self, job_id: str) -> Any | None: ...

    async def remove_job(self, job_id: str) -> bool: ...

    async def run_job_now(self, job_id: str) -> Any: ...

    async def get_runs(self, job_id: str, limit: int = 20) -> list[Any]: ...


# Setter-injected dependencies (gateway boot calls these)
_scheduler: _SchedulerProtocol | None = None
_gateway_config = None
#: Lazy accessor for the ChannelManager. A callable rather than the manager
#: itself because channels are constructed after the tools are wired, the same
#: reason the cron delivery engine takes a ref.
_channel_manager_ref: Any = None


def set_scheduler(engine: _SchedulerProtocol) -> None:
    """Inject the SchedulerEngine (called from gateway boot)."""
    global _scheduler
    _scheduler = engine


def set_channel_manager_ref(ref: Any) -> None:
    """Inject a ``() -> ChannelManager | None`` accessor (from gateway boot)."""
    global _channel_manager_ref
    _channel_manager_ref = ref


def set_gateway_config(config: object) -> None:
    """Inject the GatewayConfig (called from gateway boot)."""
    global _gateway_config
    _gateway_config = config


def scheduler_available() -> bool:
    return _scheduler is not None


def gateway_config_available() -> bool:
    return _gateway_config is not None


# ---------------------------------------------------------------------------
# cron
# ---------------------------------------------------------------------------


def _coerce_tool_schedule(
    schedule: Any,
    *,
    tz: str = "",
) -> tuple[ScheduleKind, str, str]:
    """Validate the structured `schedule` param from the LLM tool call.

    Returns ``(ScheduleKind, schedule_value, schedule_tz)`` ready for
    ``add_job(schedule_kind=..., schedule_value=..., schedule_tz=...)``.

    Raises ``SafeToolError`` whose message names the offending field and shows
    the accepted shape so the model can self-correct on the next turn. Plain
    ``ToolError`` would be sanitised to a generic "internal error" line, which
    leaves the model nothing to correct against.
    """
    if not isinstance(schedule, dict):
        raise SafeToolError(
            "schedule must be an object with shape "
            "{kind: 'cron'|'every'|'at', ...}; "
            f"got {type(schedule).__name__}"
        )
    try:
        return coerce_schedule_from_params({"schedule": schedule, "tz": tz})
    except ValueError as exc:
        raise SafeToolError(str(exc)) from exc


def _cron_job_agent_id(job: Any) -> str:
    """Return the profile that owns a scheduled job."""
    payload = getattr(job, "payload", None)
    return payload_agent_id(payload if isinstance(payload, dict) else None, "main")


def _operator_caller(ctx: Any) -> bool:
    """True when the call comes from an interactive CLI or Web session.

    Scripts and elevated tool policies hand an unattended job a real shell, so
    every path that creates, inherits, or edits one is gated on the same answer.
    """
    from agentos.tools.types import CallerKind

    caller_kind = getattr(ctx, "caller_kind", None) if ctx is not None else None
    return caller_kind in (CallerKind.CLI, CallerKind.WEB)


def _enum_value(value: Any, default: str = "") -> str:
    return str(getattr(value, "value", value) or default)


def _webhook_origin(url: str) -> str:
    """Scheme + host of a webhook URL, without the secret-bearing path.

    A Slack/Discord/Teams webhook URL *is* the credential — the path is the
    secret. The model only needs to know where a job reports, so it gets the
    host and nothing that would let it re-post there.
    """
    if "://" not in url:
        return url.split("/", 1)[0]
    scheme, rest = url.split("://", 1)
    return f"{scheme}://{rest.split('/', 1)[0]}"


def _cron_delivery_view(delivery: Any) -> dict[str, Any]:
    """Shape a job's delivery routing for the model.

    A webhook's URL and token are credentials: their presence is reported and
    the host is named so a destination is recognisable, but nothing that could
    be replayed is disclosed.
    """
    if delivery is None:
        return {"mode": "none"}
    view: dict[str, Any] = {"mode": _enum_value(getattr(delivery, "mode", ""), "none")}
    for field in ("channel_name", "channel_id", "account_id", "thread_id"):
        value = str(getattr(delivery, field, "") or "")
        if value:
            view[field] = value
    webhook_url = str(getattr(delivery, "webhook_url", "") or "")
    if webhook_url:
        view["webhook_host"] = _webhook_origin(webhook_url)
        view["webhook_url_set"] = True
    if getattr(delivery, "webhook_token", ""):
        view["webhook_token_set"] = True
    if getattr(delivery, "best_effort", False):
        view["best_effort"] = True
    failure = getattr(delivery, "failure_destination", None)
    if failure is not None:
        view["failure_destination"] = _cron_delivery_view(failure)
    return view


def _delivery_targets_caller(delivery: Any, ctx: Any) -> bool:
    """True when a job's destination is one the caller can already write to.

    Inheriting or keeping a destination is a routing grant, not just a setting:
    without this, a channel user could clone an announcement job and have their
    own text delivered to the channel — or through the webhook credential — the
    original reported to. Operators are exempt; everyone else may only author
    content for the chat they are speaking in.
    """
    if delivery is None:
        return True
    if _enum_value(getattr(delivery, "mode", ""), "none") == DeliveryMode.WEBHOOK.value:
        return False
    if getattr(delivery, "webhook_url", "") or getattr(delivery, "webhook_token", ""):
        return False
    failure = getattr(delivery, "failure_destination", None)
    if failure is not None and not _delivery_targets_caller(failure, ctx):
        return False

    caller_channel = str(getattr(ctx, "channel_kind", "") or "") if ctx is not None else ""
    caller_id = str(getattr(ctx, "channel_id", "") or "") if ctx is not None else ""
    targets = [
        (
            str(getattr(delivery, "channel_name", "") or ""),
            str(getattr(delivery, "channel_id", "") or ""),
        )
    ]
    snapshot = getattr(delivery, "originating_reply_target", None)
    if snapshot is not None:
        targets.append(
            (
                str(getattr(snapshot, "channel_name", "") or ""),
                str(getattr(snapshot, "to", "") or ""),
            )
        )
    for channel_name, channel_id in targets:
        if not channel_name and not channel_id:
            continue
        if channel_name != caller_channel or channel_id != caller_id:
            return False
    return True


def _cron_job_view(job: Any) -> dict[str, Any]:
    """Every setting the model needs to describe a job or derive one from it.

    ``action=list`` deliberately stays thin; this is the full record, so a
    "clone it but change the prompt" request can be answered by reading the
    source rather than guessing at defaults.
    """
    from agentos.permissions import configured_cron_default_elevated, cron_tool_policy_elevated

    payload = job.payload if isinstance(getattr(job, "payload", None), dict) else {}
    session_target = getattr(job, "session_target", "")
    kind = payload_kind(payload, session_target)
    next_run_at = getattr(job, "next_run_at", None)
    last_run_at = getattr(job, "last_run_at", None)
    return {
        "job_id": job.id,
        "name": job.name,
        "status": _enum_value(getattr(job, "status", "")),
        "enabled": bool(getattr(job, "enabled", True)),
        "schedule": {
            "kind": _enum_value(getattr(job, "schedule_kind", "")),
            "value": str(getattr(job, "cron_expr", "") or ""),
        },
        "tz": str(getattr(job, "tz", "") or ""),
        "job_kind": kind,
        "task": "" if kind == SCRIPT_KIND else payload_text(payload, session_target),
        "script": payload_script(payload),
        "script_args": payload_args(payload),
        "workdir": payload_workdir(payload),
        "agent_id": _cron_job_agent_id(job),
        "session_target": _enum_value(session_target),
        "session_key": str(getattr(job, "session_key", "") or ""),
        "delivery": _cron_delivery_view(getattr(job, "delivery", None)),
        "tool_policy": dict(getattr(job, "tool_policy", None) or {}),
        "wake_mode": _enum_value(getattr(job, "wake_mode", ""), "now"),
        "timeout_seconds": float(getattr(job, "timeout_seconds", 600.0) or 600.0),
        "created_from": str(getattr(job, "creator_session_key", "") or ""),
        "next_run_at": next_run_at.isoformat() if next_run_at is not None else "",
        "last_run_at": last_run_at.isoformat() if last_run_at is not None else "",
        "elevated": (
            cron_tool_policy_elevated(job.tool_policy)
            if isinstance(getattr(job, "tool_policy", None), dict) and "elevated" in job.tool_policy
            else (
                configured_cron_default_elevated(_gateway_config)
                if getattr(job, "handler_key", None) == "agent_run"
                else None
            )
        )
        or "",
    }


def _validate_kind_and_target(job_kind: str, session_target: str) -> None:
    """Reject the job_kind / session_target combinations the scheduler forbids."""
    if job_kind not in _VALID_JOB_KINDS:
        raise SafeToolError("job_kind must be reminder, system_event, agent_turn, or script")
    if session_target not in _VALID_SESSION_TARGETS:
        raise SafeToolError("session_target must be main, isolated, current, or session")
    if job_kind == SCRIPT_KIND and session_target == "main":
        raise SafeToolError("script jobs cannot use session_target=main")
    if job_kind == SYSTEM_EVENT_KIND and session_target != "main":
        raise SafeToolError("system_event jobs must use session_target=main")
    if job_kind == REMINDER_KIND and session_target == "main":
        raise SafeToolError("reminder jobs cannot use session_target=main")
    if job_kind == AGENT_TURN_KIND and session_target == "main":
        raise SafeToolError("agent_turn jobs cannot use session_target=main")


def _cron_run_item(run: Any) -> dict[str, Any]:
    """Shape one execution record for the model.

    ``summary`` is renamed to ``output`` because for a script job that field
    holds the script's literal stdout, not a description of it — a name that
    invites the model to quote it rather than paraphrase. ``delivery`` is
    included so the model can tell "this ran and told you" from "this ran and
    the output went nowhere", which reads identically from the job alone.
    """
    output = str(getattr(run, "summary", "") or "")
    truncated = len(output) > _CRON_RUN_OUTPUT_MAX_CHARS
    if truncated:
        output = output[:_CRON_RUN_OUTPUT_MAX_CHARS]
    started_at = getattr(run, "started_at", None)
    item: dict[str, Any] = {
        "started_at": started_at.isoformat() if started_at is not None else "",
        "success": bool(getattr(run, "success", False)),
        "output": output,
        "delivery": str(getattr(run, "delivery_status", "") or ""),
    }
    if truncated:
        item["output_truncated"] = True
    error = getattr(run, "error", None)
    if error:
        item["error"] = str(error)
    return item


#: Delivery fields the tool accepts, in the snake_case the schema advertises and
#: the camelCase the RPC wire uses — a model that has seen `channelName` in a
#: cron listing should not have its call silently ignored.
_CRON_DELIVERY_ALIASES = {
    "channel_name": ("channel_name", "channelName", "channel"),
    "channel_id": ("channel_id", "channelId", "to"),
    "account_id": ("account_id", "accountId"),
    "thread_id": ("thread_id", "threadId"),
}

_CRON_DELIVERY_MODES = ("origin", "channel", "none")

#: Delivery features the CLI, Web UI, and RPC support that this tool does not.
#: Named explicitly so a model that copies a webhook block out of a cron listing
#: is told the field is unavailable here, rather than having it silently
#: dropped and its job announce somewhere else entirely.
_CRON_DELIVERY_UNSUPPORTED = {
    "webhook_url": "webhook delivery",
    "webhookUrl": "webhook delivery",
    "webhook_token": "webhook delivery",
    "webhookToken": "webhook delivery",
    "failure_destination": "a failure destination",
    "failureDestination": "a failure destination",
}


def _parse_cron_delivery(raw: Any) -> dict[str, Any] | None:
    """Normalize the tool's ``delivery`` argument, or ``None`` when omitted.

    Returns ``{"mode": ..., "channel_name": ..., ..., "best_effort": bool}``.
    Shape errors raise ``SafeToolError`` so the model is told what to fix rather
    than having its stated destination quietly dropped — the whole point of the
    parameter is that saying "post it to the ops group" has an effect.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SafeToolError("'delivery' must be an object")
    if not raw:
        return None

    for key, feature in _CRON_DELIVERY_UNSUPPORTED.items():
        if raw.get(key):
            raise SafeToolError(
                f"delivery.{key} is not available from the cron tool: {feature} "
                "is configured from the CLI, the Web UI, or the cron RPC"
            )

    parsed: dict[str, Any] = {}
    for field, aliases in _CRON_DELIVERY_ALIASES.items():
        value = ""
        for alias in aliases:
            candidate = raw.get(alias)
            if candidate:
                value = str(candidate).strip()
                break
        parsed[field] = value

    mode = str(raw.get("mode") or "").strip().lower()
    if not mode:
        # A model that fills in a recipient without naming a mode means
        # "send it there"; an otherwise empty object means nothing at all.
        mode = "channel" if parsed["channel_name"] else "origin"
    if mode not in _CRON_DELIVERY_MODES:
        raise SafeToolError(
            f"delivery.mode must be {', '.join(_CRON_DELIVERY_MODES)} (got '{mode}')"
        )
    if mode != "channel":
        # A destination that mode does not route to goes nowhere. Left to fall
        # through it would read as "deliver to the caller", quietly discarding
        # the recipient the user actually named — issue #310's own symptom,
        # reachable through the parameter meant to fix it. A bare channel_name
        # was already promoted to mode='channel' above, so the only way to land
        # here is an explicitly contradictory mode: nothing legitimate is lost
        # by refusing it.
        stray = [f for f in ("channel_name", "channel_id", "account_id", "thread_id") if parsed[f]]
        if stray:
            raise SafeToolError(
                f"delivery.{stray[0]} conflicts with delivery.mode='{mode}' — "
                "use mode='channel' to deliver to a named channel, or drop the "
                f"{stray[0]}"
            )
    elif not parsed["channel_name"]:
        raise SafeToolError("delivery.mode='channel' requires delivery.channel_name")

    parsed["mode"] = mode
    parsed["best_effort"] = bool(raw.get("best_effort") or raw.get("bestEffort") or False)
    return parsed


def _validate_cron_delivery_channel(channel_name: str) -> None:
    """Reject a channel that no adapter is registered for.

    ``validate_channel_target`` checks the *recipient*'s shape; nothing checked
    the channel itself, so a plausible-looking typo (``slak``) saved cleanly and
    then failed every single fire with "no adapter is registered". The model is
    the most likely source of that name, so the list of real ones is worth
    spending a few tokens on.

    The name checked is the *configured channel name*, which is usually the
    adapter type (``telegram``) but is whatever the operator called the entry.
    Silent when no manager is reachable — in a CLI process without channels
    this is unknowable, not invalid.
    """
    if _channel_manager_ref is None:
        return
    try:
        manager = _channel_manager_ref()
        if manager is None:
            return
        known = [str(name) for name, _ in manager.items()]
    except Exception:  # noqa: BLE001 - channel manager absent or mid-boot
        return
    if channel_name in known:
        return
    if not known:
        # A live manager with nothing in it is a real answer, not a missing one:
        # no channel delivery can succeed at all.
        raise SafeToolError("no channels are configured, so a cron job cannot deliver to one")
    raise SafeToolError(
        f"no channel named '{channel_name}' is configured; available: {', '.join(sorted(known))}"
    )


def _cron_delivery_summary(config: Any) -> dict[str, Any]:
    """The destination of a saved job, in the same words the tool accepts."""
    if config is None:
        return {"mode": "none"}
    mode = getattr(config, "mode", None)
    summary: dict[str, Any] = {
        "mode": getattr(mode, "value", None) or str(mode or "none"),
    }
    for field in ("channel_name", "channel_id", "account_id", "thread_id"):
        value = str(getattr(config, field, "") or "")
        if value:
            summary[field] = value
    if getattr(config, "best_effort", False):
        summary["best_effort"] = True
    return summary


async def _cron_update_delivery(
    override: dict[str, Any],
    target_job: Any,
    ctx: Any,
    caller_session_key: str,
    session_target: str,
) -> DeliveryConfig:
    """The delivery config an ``action=update`` repoint should persist.

    ``ws_topic`` is per-job, not per-destination, so it is carried across the
    move: dropping it would orphan every websocket subscriber already watching
    the job. A failure destination cannot be set from the tool, so whatever the
    job carries survives the edit rather than being silently cleared.
    """
    existing = getattr(target_job, "delivery", None)
    ws_topic = str(getattr(existing, "ws_topic", "") or "") or f"cron:{target_job.id}"
    mode = override["mode"]

    config: DeliveryConfig
    if mode == "none":
        config = DeliveryConfig(mode=DeliveryMode.NONE, ws_topic=ws_topic)
    elif mode == "channel":
        config = DeliveryConfig(
            mode=DeliveryMode.CHANNEL,
            channel_name=override["channel_name"],
            channel_id=override["channel_id"],
            account_id=override["account_id"],
            thread_id=override["thread_id"],
            ws_topic=ws_topic,
        )
    else:
        # mode='origin' means "report back to the conversation asking for this",
        # which is the calling session — the same inference a bare add makes.
        from agentos.scheduler.delivery import infer_delivery

        config = await infer_delivery(
            session_storage=_session_storage_or_none(),
            session_key=caller_session_key,
            user_overrides=None,
        )
        config.ws_topic = ws_topic
        if (
            config.mode == DeliveryMode.NONE
            and ctx is not None
            and getattr(ctx, "channel_kind", None)
        ):
            # Session storage has no routing target yet (a fresh session before
            # last_channel was written); the live context still knows one.
            config.mode = DeliveryMode.ORIGIN
            config.channel_name = ctx.channel_kind or ""
            config.channel_id = ctx.channel_id or ""
        if (
            config.mode == DeliveryMode.ORIGIN
            and config.channel_name
            and config.originating_reply_target is None
        ):
            config.originating_reply_target = ReplyTargetSnapshot(
                channel_name=config.channel_name,
                channel_type=config.channel_name,
                to=config.channel_id,
                account_id=config.account_id,
                thread_id=config.thread_id,
            )
        if session_target == "main":
            # Persistence forces NONE for main; the snapshot is what pins the
            # reply target, so it is the only part worth keeping.
            config = DeliveryConfig(
                mode=DeliveryMode.NONE,
                ws_topic=ws_topic,
                originating_reply_target=config.originating_reply_target,
            )

    if override["best_effort"]:
        config.best_effort = True
    if existing is not None and getattr(existing, "failure_destination", None) is not None:
        config.failure_destination = existing.failure_destination
    return config


def _session_storage_or_none() -> Any:
    """The session store ``infer_delivery`` reads, or ``None`` when unavailable."""
    try:
        from agentos.tools.builtin.sessions import _get_session_manager

        mgr = _get_session_manager()
    except Exception:  # noqa: BLE001 - no session manager wired: inference is optional
        return None
    return getattr(mgr, "_storage", mgr)


@tool(
    name="cron",
    description=(
        "Create, list, inspect, edit, remove, or trigger scheduled cron jobs. "
        "To change an existing job, use action=update with its job_id — never "
        "remove it and add a replacement, which loses its kind, timezone, tool "
        "policy, and delivery target. To make a second job like an existing one, "
        "use action=add with clone_from=<job_id>: the clone inherits every "
        "setting of the source and overrides only the fields you pass, and the "
        "source keeps running. Read a job's full settings first with action=get; "
        "action=list is a summary only. "
        "Use action=runs to answer any question about what a job actually did — "
        "its recent runs with the output each one produced, whether it succeeded, "
        "and where that output was delivered. For a script job the run output is "
        "the script's stdout, and run history is the only place it is recorded, "
        "so answer from action=runs rather than guessing what a schedule produced. "
        "Use this tool (NOT exec_command or background_process) for any recurring/timed "
        "task scheduling or reminders. Translate any natural language into the "
        "structured schedule shape yourself; the tool will not parse free-form text. "
        "For proactive reminders, including reminders phrased as 'this/current "
        "session', use job_kind=reminder and session_target=isolated so the "
        "scheduled run delivers static text without invoking the agent/model "
        "chain or adding a fake user turn to the visible conversation. Use "
        "job_kind=system_event and session_target=main only for internal "
        "main-session events. "
        "For recurring background agent tasks such as 'every morning summarize "
        "yesterday's emails', use job_kind=agent_turn with session_target=isolated. "
        "Channel users can create reminders and tasks bound to the calling channel. "
        "List, remove, and run operate on all jobs in the current profile, regardless "
        "of which connected session created them."
    ),
    params={
        "action": {
            "type": "string",
            "description": "Action: list, add, get, update, remove, run, runs",
        },
        "schedule": {
            "type": "object",
            "description": (
                "Structured schedule. Choose one shape. "
                "Do not pass human language in schedule; translate it before the tool call. "
                "Examples: "
                "for '每5分钟提醒我喝水' call schedule={kind:'cron', expr:'*/5 * * * *'}, "
                "job_kind='reminder', session_target='isolated'; "
                "for '45分钟后提醒我' call "
                "schedule={kind:'at', at:'<now+45min as ISO-8601 with timezone>'}; "
                "for '每30秒打印一次' call schedule={kind:'every', every_seconds:30}; "
                "for 'every weekday at 9 AM Shanghai time' call "
                "schedule={kind:'cron', expr:'0 9 * * 1-5', tz:'Asia/Shanghai'}."
            ),
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["cron", "every", "at"],
                },
                "expr": {
                    "type": "string",
                    "description": "5-field POSIX cron (kind=cron)",
                },
                "tz": {
                    "type": "string",
                    "description": "Optional IANA timezone (kind=cron)",
                },
                "every_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Interval in seconds (kind=every)",
                },
                "at": {
                    "type": "string",
                    "description": "ISO-8601 with timezone (kind=at)",
                },
            },
            "required": ["kind"],
        },
        "task": {
            "type": "string",
            "description": (
                "Message to execute on trigger (required for add; on update it "
                "replaces the job's prompt and leaves every other setting alone)"
            ),
        },
        "name": {
            "type": "string",
            "description": (
                "Display name for the job. Defaults to the task text on add; pass "
                "it to keep a readable name independent of the prompt, or on "
                "update to rename a job without touching what it does."
            ),
        },
        "clone_from": {
            "type": "string",
            "description": (
                "Job ID to copy on add. The new job inherits the source's tz, "
                "job_kind, session_target, delivery target, tool_policy, "
                "wake_mode, script, and schedule; anything you pass alongside "
                "overrides that field. The source job is left untouched."
            ),
        },
        "enabled": {
            "type": "boolean",
            "description": (
                "Enable or disable a job. update only — a new job always starts "
                "enabled, including a clone of a disabled one."
            ),
        },
        "job_kind": {
            "type": "string",
            "description": (
                "Use reminder for static user-facing reminders; it does not call "
                "the model. Use agent_turn only for scheduled background tasks "
                "that need the agent/model to work. Use system_event only for "
                "internal main-session events. Use script to run an existing "
                "script on schedule and deliver its stdout — no LLM, no tokens; "
                "it requires the script parameter and an interactive CLI or Web "
                "caller."
            ),
            "enum": ["reminder", "system_event", "agent_turn", "script"],
        },
        "script": {
            "type": "string",
            "description": (
                "File under ~/.agentos/scripts/ to run. Relative path only; "
                ".sh/.bash run under bash, anything else under python. With "
                "job_kind='script' it IS the job: stdout is delivered verbatim, "
                "empty stdout stays silent, a non-zero exit is a failure. With "
                "job_kind='agent_turn' it is a pre-run collector: its stdout is "
                "given to the agent as context, and no output means the turn is "
                "skipped entirely. Either way it needs an interactive CLI or Web "
                "caller. Subdirectories are allowed, and '{job_id}' anywhere in "
                "the path is replaced with the created job's own id — use it to "
                "give a job its own directory in one call, then write the file "
                "to the 'script_path' the result reports."
            ),
        },
        "script_args": {
            "type": "array",
            "items": {"type": "string"},
            "description": ("Arguments passed to 'script' as argv. Never shell-interpreted."),
        },
        "workdir": {
            "type": "string",
            "description": (
                "Optional working directory for 'script' (defaults to the script's own directory)."
            ),
        },
        "session_target": {
            "type": "string",
            "description": (
                "Target session mode for add and update. Use main for internal system "
                "events, "
                "isolated for proactive reminders that should deliver back to the "
                "caller, current only when the user explicitly wants the scheduled "
                "run to continue the current transcript as a conversation, or session "
                "with target_session_key for a named session."
            ),
            "enum": ["main", "isolated", "current", "session"],
        },
        "target_session_key": {
            "type": "string",
            "description": "Named session key when session_target=session.",
        },
        "job_id": {
            "type": "string",
            "description": "Job ID (required for get, update, remove, run, and runs)",
        },
        "limit": {
            "type": "integer",
            "description": (
                f"How many recent runs to return for action=runs "
                f"(default {_CRON_RUNS_DEFAULT_LIMIT}, max {_CRON_RUNS_MAX_LIMIT})."
            ),
            "minimum": 1,
            "maximum": _CRON_RUNS_MAX_LIMIT,
            "default": _CRON_RUNS_DEFAULT_LIMIT,
        },
        "agent_id": {
            "type": "string",
            "description": "Agent to run the task as (for add)",
            "default": "main",
        },
        "wake_mode": {
            "type": "string",
            "description": (
                "Main-session heartbeat mode: now runs one "
                "heartbeat immediately; next-heartbeat only queues a wake. "
                "Defaults to now on add; omit it on update to leave the job's "
                "mode alone."
            ),
            "enum": ["now", "next-heartbeat"],
        },
        "tool_policy": {
            "type": "object",
            "description": (
                "Optional per-job cron tool policy with profile, allow, also_allow, and "
                "deny. profile must be one of: coding, full, memory_only, messaging, "
                "minimal — omit it to inherit the caller's tools rather than guessing a "
                "name. May also carry elevated: 'bypass' to let the job run shell-based "
                "skills unattended, which only an interactive CLI or Web caller may set."
            ),
        },
        "tz": {
            "type": "string",
            "description": (
                "Optional IANA timezone (e.g. 'America/Los_Angeles', 'Asia/Shanghai'). "
                "Applies to cron expressions; '0 9 * * *' with tz='America/Los_Angeles' "
                "fires at 09:00 LA wall time. Omitted on add it means UTC; omitted on "
                "update or with clone_from it leaves the existing zone in place, so pass "
                "tz='UTC' to move a job back to UTC."
            ),
        },
        "delivery": {
            "type": "object",
            "description": (
                "Where the job announces its result. Omit it and delivery is "
                "inferred from the calling conversation, which is what a plain "
                "'remind me' wants. Pass it only when the user names a different "
                "destination: mode='channel' with channel_name and channel_id "
                "posts to that chat instead, mode='none' keeps the run silent. "
                "Works on update too, so moving a job's announcement is an edit "
                "— never remove the job and add a replacement to change it. "
                "channel_id is the id the provider uses (a Telegram numeric chat "
                "id, negative for groups, or @username), never an AgentOS session "
                "key; leave it empty to use the channel's configured default chat. "
                "A recipient field without channel_name is an error, not a "
                "fallback, and webhook delivery and failure destinations are not "
                "available here. Choosing a channel requires an interactive CLI "
                "or Web caller and a session_target other than main."
            ),
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["origin", "channel", "none"],
                    "description": (
                        "origin keeps the calling conversation (the default), "
                        "channel posts to channel_name/channel_id, none disables "
                        "delivery."
                    ),
                },
                "channel_name": {
                    "type": "string",
                    "description": (
                        "The configured channel's name when mode=channel — usually "
                        "the adapter type (telegram, slack, discord), but whatever "
                        "the operator named the entry. An unconfigured name is "
                        "rejected and the error lists the real ones."
                    ),
                },
                "channel_id": {
                    "type": "string",
                    "description": (
                        "Provider-side recipient when mode=channel. Empty means the "
                        "channel's configured default chat."
                    ),
                },
                "account_id": {
                    "type": "string",
                    "description": ("Optional account binding for multi-account channels."),
                },
                "thread_id": {
                    "type": "string",
                    "description": (
                        "Optional thread id inside the recipient chat (Slack only today)."
                    ),
                },
                "best_effort": {
                    "type": "boolean",
                    "description": (
                        "When true a delivery failure does not fail the run "
                        "(default false). Applies to any mode."
                    ),
                },
            },
        },
    },
    required=["action"],
)
async def cron(
    action: str,
    schedule: dict[str, Any] | None = None,
    task: str | None = None,
    job_kind: str | None = None,
    session_target: str | None = None,
    target_session_key: str | None = None,
    job_id: str | None = None,
    clone_from: str | None = None,
    name: str | None = None,
    enabled: bool | None = None,
    agent_id: str = "main",
    wake_mode: str | None = None,
    tool_policy: dict[str, Any] | None = None,
    script: str | None = None,
    script_args: list[str] | None = None,
    workdir: str = "",
    tz: str = "",
    delivery: dict[str, Any] | None = None,
    limit: int = _CRON_RUNS_DEFAULT_LIMIT,
) -> str:
    if action not in _VALID_CRON_ACTIONS:
        raise SafeToolError(
            f"Invalid action: {action}. Must be list|add|get|update|remove|run|runs"
        )

    # `job_kind` / `session_target` / `wake_mode` arrive as None when the caller
    # did not name them, so `add` can tell "inherit from clone_from" from "the
    # caller asked for a reminder" and `update` can leave them untouched.
    if action == "add" and schedule is None and not clone_from:
        raise SafeToolError("'schedule' required for add")
    if (
        action == "add"
        and not clone_from
        and (job_kind or REMINDER_KIND) != SCRIPT_KIND
        and not task
    ):
        raise SafeToolError("'task' required for add")
    if action in ("get", "update", "remove", "run", "runs") and not job_id:
        raise SafeToolError(f"'job_id' required for {action}")
    if action != "update" and enabled is not None:
        raise SafeToolError("'enabled' is only accepted by update; a new job always starts enabled")

    # Dispatch to injected scheduler
    if _scheduler is None:
        raise SafeToolError("Scheduler not available")

    sched = _scheduler

    # Scheduled jobs belong to a profile, not to the session that created them.
    # The creator session remains delivery/display metadata only.
    from agentos.tools.types import CallerKind, current_tool_context

    ctx = current_tool_context.get()
    channel_caller = ctx is not None and ctx.caller_kind is CallerKind.CHANNEL
    current_agent_id = (
        str(ctx.agent_id).strip()
        if ctx is not None and getattr(ctx, "agent_id", None)
        else str(agent_id or "main").strip() or "main"
    )
    caller_session_key = ctx.session_key if ctx is not None and ctx.session_key else ""
    caller_sender_id = str(getattr(ctx, "sender_id", "") or "") if ctx is not None else ""

    if channel_caller:
        if not caller_session_key:
            raise SafeToolError("cron requires a session context for channel callers")
        if action in ("add", "update"):
            if target_session_key:
                raise SafeToolError(
                    "target_session_key is unavailable from a channel; "
                    "channel reminders stay in the current session"
                )
            if tool_policy:
                raise SafeToolError("tool_policy is unavailable from a channel")

    # Elevation hands an unattended job a real shell, so it stays an operator
    # decision. Subagents and agent-kind callers already cannot reach `cron` at
    # all — this makes the rule explicit rather than emergent from two denylists.
    # Note: This gate only blocks explicit per-job tool_policy.elevated requests.
    # Unattended default elevation from cron_default_mode is handled at routing time.
    if tool_policy and isinstance(tool_policy, dict) and tool_policy.get("elevated"):
        if not _operator_caller(ctx):
            raise SafeToolError("tool_policy.elevated requires an interactive CLI or Web caller")

    # Any job that runs a script executes a file on this host every tick with
    # nothing in the loop to review it — the same unattended shell that
    # elevation grants, minus the model. Both the script job and the pre-run
    # collector get the same operator gate. `add` re-checks it after clone_from
    # is resolved, because an inherited script is still a script.
    if action == "add" and (job_kind == SCRIPT_KIND or script) and not _operator_caller(ctx):
        raise SafeToolError("scheduling a script requires an interactive CLI or Web caller")

    if action == "list":
        from agentos.permissions import configured_cron_default_elevated, cron_tool_policy_elevated

        jobs = [
            job for job in await sched.list_jobs() if _cron_job_agent_id(job) == current_agent_id
        ]
        items = [
            {
                "job_id": j.id,
                "name": j.name,
                "cron_expr": j.cron_expr,
                "tz": str(getattr(j, "tz", "") or ""),
                "job_kind": payload_kind(
                    j.payload if isinstance(getattr(j, "payload", None), dict) else {},
                    getattr(j, "session_target", ""),
                ),
                "status": j.status.value if hasattr(j.status, "value") else str(j.status),
                "agent_id": _cron_job_agent_id(j),
                "created_from": getattr(j, "creator_session_key", "") or "",
                "elevated": (
                    cron_tool_policy_elevated(j.tool_policy)
                    if isinstance(getattr(j, "tool_policy", None), dict)
                    and "elevated" in j.tool_policy
                    else (
                        configured_cron_default_elevated(_gateway_config)
                        if getattr(j, "handler_key", None) == "agent_run"
                        else None
                    )
                )
                or "",
            }
            for j in jobs
        ]
        return json.dumps({"action": "list", "jobs": items})

    if action == "get":
        assert job_id is not None
        target_job = await sched.get_job(job_id)
        if target_job is None:
            raise SafeToolError(f"Job not found: {job_id}")
        if _cron_job_agent_id(target_job) != current_agent_id:
            raise SafeToolError("cron job belongs to a different profile")
        return json.dumps({"action": "get", "job": _cron_job_view(target_job)})

    if action == "runs":
        assert job_id is not None
        target_job = await sched.get_job(job_id)
        if target_job is None:
            raise SafeToolError(f"Job not found: {job_id}")
        if _cron_job_agent_id(target_job) != current_agent_id:
            raise SafeToolError("cron job belongs to a different profile")
        try:
            requested = int(limit)
        except (TypeError, ValueError):
            requested = _CRON_RUNS_DEFAULT_LIMIT
        requested = max(1, min(requested, _CRON_RUNS_MAX_LIMIT))
        runs = await sched.get_runs(job_id, limit=requested)
        return json.dumps(
            {
                "action": "runs",
                "job_id": job_id,
                "name": target_job.name,
                "runs": [_cron_run_item(run) for run in runs],
            }
        )

    if action == "add":
        source_job: Any | None = None
        task_provided = task is not None
        if clone_from:
            source_job = await sched.get_job(clone_from)
            if source_job is None:
                raise SafeToolError(f"Job not found: {clone_from}")
            if _cron_job_agent_id(source_job) != current_agent_id:
                raise SafeToolError("cron job belongs to a different profile")

        # Every field the source defines becomes this job's default; anything the
        # caller passed wins. This is the whole point of clone_from — a job
        # derived from another must not silently fall back to reminder/UTC/no
        # policy/current-chat delivery the way a bare re-create does.
        source_payload: dict[str, Any] = {}
        source_kind = ""
        if source_job is not None:
            if isinstance(getattr(source_job, "payload", None), dict):
                source_payload = source_job.payload
            source_kind = payload_kind(source_payload, source_job.session_target)
            if task is None and source_kind != SCRIPT_KIND:
                task = payload_text(source_payload, source_job.session_target)
            if script is None:
                script = payload_script(source_payload) or None
            if not workdir:
                workdir = payload_workdir(source_payload)
            if script_args is None:
                inherited_args = payload_args(source_payload)
                script_args = inherited_args or None
            if tool_policy is None:
                tool_policy = dict(getattr(source_job, "tool_policy", None) or {}) or None
            if not tz:
                tz = str(getattr(source_job, "tz", "") or "")

        job_kind = str(job_kind or source_kind or REMINDER_KIND)
        session_target = str(
            session_target
            or (_enum_value(source_job.session_target) if source_job is not None else "")
            or "isolated"
        )
        wake_mode = (
            str(
                wake_mode
                or (_enum_value(getattr(source_job, "wake_mode", ""), "now") if source_job else "")
                or "now"
            )
            .strip()
            .lower()
        )

        # A clone inherits privilege as well as settings, so the operator gates
        # are re-checked against what the new job will actually carry.
        if source_job is not None:
            if session_target == "session" and not target_session_key and not channel_caller:
                target_session_key = str(getattr(source_job, "session_key", "") or "") or None
            if channel_caller and (tool_policy or script):
                raise SafeToolError(
                    "clone_from is unavailable from a channel for jobs that carry "
                    "a tool policy or a script"
                )
            if channel_caller and not _delivery_targets_caller(
                getattr(source_job, "delivery", None), ctx
            ):
                raise SafeToolError(
                    "that job reports to a destination this chat cannot address, "
                    "so it can only be cloned by an interactive CLI or Web caller"
                )
            if isinstance(tool_policy, dict) and tool_policy.get("elevated"):
                if not _operator_caller(ctx):
                    raise SafeToolError(
                        "tool_policy.elevated requires an interactive CLI or Web caller"
                    )
            if (job_kind == SCRIPT_KIND or script) and not _operator_caller(ctx):
                raise SafeToolError("scheduling a script requires an interactive CLI or Web caller")

        if job_kind != SCRIPT_KIND and not (task or "").strip():
            raise SafeToolError("'task' required for add")

        if schedule is not None:
            schedule_kind, schedule_value, schedule_tz = _coerce_tool_schedule(
                schedule,
                tz=tz,
            )
        else:
            assert source_job is not None
            if source_job.schedule_kind == ScheduleKind.AT:
                raise SafeToolError(
                    "clone_from of a one-shot 'at' job needs an explicit schedule: "
                    "the source's fire time is already spent"
                )
            schedule_kind = source_job.schedule_kind
            schedule_value = str(source_job.cron_expr or "")
            schedule_tz = tz

        # Scan prompt for injection/exfiltration before scheduling
        if task:
            blocked, reason = _scan_cron_prompt(task)
            if blocked:
                raise SafeToolError(reason)

        if job_kind not in _VALID_JOB_KINDS:
            raise SafeToolError("job_kind must be reminder, system_event, agent_turn, or script")
        if job_kind == SCRIPT_KIND:
            if session_target == "main":
                raise SafeToolError("script jobs cannot use session_target=main")
            if not script or not script.strip():
                raise SafeToolError("job_kind='script' requires 'script'")
            # A script job runs the file directly and never starts an agent
            # turn, so there is no tool policy for elevation to apply to. The
            # scheduler refuses this too, but only once the message has been
            # flattened into a bare ValueError several layers down.
            if isinstance(tool_policy, dict) and tool_policy.get("elevated"):
                raise SafeToolError(
                    "job_kind='script' cannot carry tool_policy.elevated: a script "
                    "job runs the file itself and never starts an agent turn for a "
                    "tool policy to apply to. Drop tool_policy, or use "
                    "job_kind='agent_turn' if the schedule needs a model in the loop."
                )
        elif script and job_kind != AGENT_TURN_KIND:
            raise SafeToolError("'script' is only used by job_kind='script' or 'agent_turn'")
        if script:
            script_error = validate_script_path(script)
            if script_error:
                raise SafeToolError(script_error)
        if session_target not in _VALID_SESSION_TARGETS:
            raise SafeToolError("session_target must be main, isolated, current, or session")
        if job_kind == SYSTEM_EVENT_KIND and session_target == "current":
            job_kind = REMINDER_KIND
            session_target = "isolated"
        if job_kind == SYSTEM_EVENT_KIND and session_target != "main":
            raise SafeToolError("system_event jobs must use session_target=main")
        if job_kind == REMINDER_KIND and session_target == "main":
            raise SafeToolError("reminder jobs cannot use session_target=main")
        if job_kind == AGENT_TURN_KIND and session_target == "main":
            raise SafeToolError("agent_turn jobs cannot use session_target=main")
        if session_target == "current" and not caller_session_key:
            raise SafeToolError("session_target=current requires a caller session context")
        if session_target == "session" and not target_session_key:
            raise SafeToolError("target_session_key is required when session_target=session")
        if wake_mode not in ("now", "next-heartbeat"):
            raise SafeToolError("wake_mode must be now or next-heartbeat")

        # An explicit destination the user named, as opposed to the calling
        # conversation the tool otherwise infers.
        override = _parse_cron_delivery(delivery)
        # mode='origin' is the inferred destination spelled out, so it must take
        # the same path as omitting the argument — including the snapshot
        # fallback below. Only these two modes redirect a job.
        redirected = override is not None and override["mode"] in ("channel", "none")
        if override is not None and override["mode"] == "channel":
            # Redirecting a job away from the conversation it was requested in
            # is an operator decision for the same reason tool_policy is: a chat
            # participant must not be able to aim scheduled output at a room
            # they were never in.
            caller_kind = getattr(ctx, "caller_kind", None) if ctx is not None else None
            if caller_kind not in (CallerKind.CLI, CallerKind.WEB):
                raise SafeToolError(
                    "delivery.mode='channel' requires an interactive CLI or Web caller; "
                    "from a chat the job delivers back to the calling conversation"
                )
            if session_target == "main":
                raise SafeToolError(
                    "delivery.mode='channel' is unavailable for session_target=main; "
                    "use session_target=isolated"
                )
            _validate_cron_delivery_channel(override["channel_name"])
            try:
                validate_channel_target(override["channel_name"], override["channel_id"])
            except ValueError as exc:
                # Caught at save time on purpose: an unusable recipient is
                # otherwise only discovered when the job fires.
                raise SafeToolError(str(exc)) from exc

        # A clone keeps the source's destination. Re-inferring it from the
        # calling session is what made a cloned announcement land in the current
        # chat instead of the channel the original reported to. `ws_topic` is
        # per-job and is re-derived below. An explicit override still wins: the
        # caller naming a destination outranks the one the source happened to
        # carry.
        clone_delivery: DeliveryConfig | None = None
        if source_job is not None and getattr(source_job, "delivery", None) is not None:
            clone_delivery = deepcopy(source_job.delivery)
            clone_delivery.ws_topic = ""

        delivery_config: DeliveryConfig | None = None

        if override is not None and override["mode"] == "none":
            delivery_config = DeliveryConfig(mode=DeliveryMode.NONE)
        elif override is not None and override["mode"] == "channel":
            from agentos.scheduler.delivery import infer_delivery

            delivery_config = await infer_delivery(
                session_storage=_session_storage_or_none(),
                session_key=caller_session_key,
                user_overrides={
                    "channel_name": override["channel_name"],
                    "channel_id": override["channel_id"],
                    "account_id": override["account_id"],
                    "thread_id": override["thread_id"],
                },
            )
        elif source_job is not None:
            delivery_config = clone_delivery
        elif caller_session_key:
            # Auto-detect delivery target from session storage.
            try:
                from agentos.scheduler.delivery import infer_delivery
                from agentos.tools.builtin.sessions import _get_session_manager

                mgr = _get_session_manager()
                storage = getattr(mgr, "_storage", mgr)
                inferred = await infer_delivery(
                    session_storage=storage,
                    session_key=caller_session_key,
                    user_overrides=None,
                )
                if (
                    inferred.mode == DeliveryMode.ORIGIN
                    and inferred.channel_name
                    and inferred.originating_reply_target is None
                ):
                    inferred.originating_reply_target = ReplyTargetSnapshot(
                        channel_name=inferred.channel_name,
                        channel_type=inferred.channel_name,
                        to=inferred.channel_id,
                        account_id=inferred.account_id,
                        thread_id=inferred.thread_id,
                    )
                if session_target == "main":
                    # Main heartbeat ignores the channel mode (persistence forces
                    # NONE for main) but uses the snapshot to pin the reply target.
                    if inferred.originating_reply_target is not None:
                        delivery_config = DeliveryConfig(
                            mode=DeliveryMode.NONE,
                            originating_reply_target=inferred.originating_reply_target,
                        )
                else:
                    delivery_config = inferred
            except Exception:
                pass

        # Snapshot fallback: when session storage did not yield a channel-
        # routable target (fresh session before last_channel was written), build
        # one from the live ToolContext so the first cron call still binds.
        # Skipped when the caller redirected the job — the point of a channel
        # or none override is that the calling chat is not where this lands.
        # Skipped for a clone too: it already carries the source's destination.
        if (
            not redirected
            and source_job is None
            and ctx is not None
            and getattr(ctx, "channel_kind", None)
            and getattr(delivery_config, "originating_reply_target", None) is None
        ):
            snapshot = ReplyTargetSnapshot(
                channel_name=ctx.channel_kind or "",
                channel_type=ctx.channel_kind or "",
                to=ctx.channel_id or "",
            )
            if delivery_config is None:
                if session_target == "main":
                    delivery_mode = DeliveryMode.NONE
                    channel_name = ""
                    channel_id = ""
                else:
                    delivery_mode = DeliveryMode.ORIGIN
                    channel_name = ctx.channel_kind or ""
                    channel_id = ctx.channel_id or ""
                delivery_config = DeliveryConfig(
                    mode=delivery_mode,
                    channel_name=channel_name,
                    channel_id=channel_id,
                    originating_reply_target=snapshot,
                )
            else:
                delivery_config.originating_reply_target = snapshot
                if session_target != "main" and delivery_config.mode == DeliveryMode.NONE:
                    delivery_config.mode = DeliveryMode.ORIGIN
                    delivery_config.channel_name = ctx.channel_kind or ""
                    delivery_config.channel_id = ctx.channel_id or ""

        # best_effort is a property of the delivery attempt, not of the
        # destination, so it applies to whichever config the branches above
        # settled on — including the inferred one.
        if override is not None and override["best_effort"] and delivery_config is not None:
            delivery_config.best_effort = True

        normalized_script = normalize_script_value(script)
        normalized_workdir = (workdir or "").strip()
        normalized_args = [str(arg) for arg in (script_args or [])]
        if job_kind == SCRIPT_KIND:
            payload = make_script_payload(
                normalized_script,
                current_agent_id,
                normalized_workdir,
                normalized_args,
            )
            handler_key = "script_run"
        elif job_kind == SYSTEM_EVENT_KIND:
            assert task is not None
            payload = make_system_event_payload(task, current_agent_id)
            handler_key = "system_event"
        elif job_kind == REMINDER_KIND:
            assert task is not None
            payload = make_reminder_payload(task, current_agent_id)
            handler_key = "static_message"
        else:
            assert task is not None
            payload = make_agent_turn_payload(
                task,
                current_agent_id,
                normalized_script,
                normalized_workdir,
                normalized_args,
            )
            handler_key = "agent_run"
        effective_tz = (schedule_tz or tz or "").strip()
        # A clone that was given a new prompt is a different job and gets a name
        # from it; one that was not keeps the source's name so the pair reads as
        # what it is. An explicit `name` always wins.
        job_name = (name or "").strip()
        if not job_name and source_job is not None and not task_provided:
            job_name = str(source_job.name or "")
        if not job_name:
            job_name = task or script or "cron-tool-job"
        extra: dict[str, Any] = {}
        if source_job is not None:
            extra["timeout_seconds"] = float(getattr(source_job, "timeout_seconds", 600.0))
            extra["max_retries"] = int(getattr(source_job, "max_retries", 3))
        try:
            job = await sched.add_job(
                name=job_name,
                handler_key=handler_key,
                payload=payload,
                session_target=SessionTarget(session_target),
                session_key=(
                    caller_session_key
                    if session_target == "current"
                    else (target_session_key or "")
                ),
                wake_mode=wake_mode,
                delivery=delivery_config,
                origin_session_key=caller_session_key,
                tool_policy=tool_policy,
                tz=effective_tz,
                creator_session_key=caller_session_key,
                creator_sender_id=caller_sender_id,
                schedule_kind=schedule_kind,
                schedule_value=schedule_value,
                schedule_tz=effective_tz,
                **extra,
            )
        except ValueError as exc:
            # The scheduler's own validation rejects combinations this tool does
            # not re-check. Its messages are authored literals naming the field,
            # so they are worth the model seeing; the bare ValueError would
            # otherwise be sanitised to "received an invalid argument".
            raise SafeToolError(str(exc)) from exc
        # Populate ws_topic
        if job.delivery and not job.delivery.ws_topic:
            job.delivery.ws_topic = f"cron:{job.id}"
            try:
                await sched.update_job(job.id, delivery=job.delivery)
            except Exception:
                pass  # best-effort
        added: dict[str, Any] = {
            "action": "add",
            "job_id": job.id,
            "name": job_name,
            "schedule_kind": _enum_value(schedule_kind),
            "schedule_value": schedule_value,
            "task": task,
            # Read back off the created job rather than echoed from the
            # argument: a `{job_id}` in the path was resolved by the scheduler
            # against the id this same result reports, and the caller needs the
            # resolved form to know where to write the file.
            "script": payload_script(job.payload),
            "payload_kind": job_kind,
            "session_target": session_target,
            "wake_mode": wake_mode,
            "tz": effective_tz,
            # Where this will actually announce. Reported for every add, not
            # just overridden ones, so "post it to the ops group" can be
            # confirmed rather than assumed.
            "delivery": _cron_delivery_summary(job.delivery),
            "status": "scheduled",
        }
        created_script = payload_script(job.payload)
        if created_script:
            # Spelled out in full on purpose: `script` is relative to a
            # directory the caller is not told the location of, and a job whose
            # script does not exist yet is one the caller is about to write.
            # Forward slashes on every platform, for the same reason skill_view
            # reports its linked files that way: the model quotes this straight
            # back as a `write_file` path, where a backslash is a JSON escape.
            # Windows accepts a forward-slash path everywhere this one is used.
            try:
                added["script_path"] = resolve_script_path(created_script).as_posix()
            except ScriptPathError:
                pass
        if source_job is not None:
            added["cloned_from"] = clone_from
        return json.dumps(added)

    if action == "update":
        assert job_id is not None
        # Repointing a live job used to be refused here, which left the model no
        # way to move an announcement except remove + re-create — losing the job
        # id the user named and its whole run history. The CLI, the Web UI and
        # the cron RPC have always been able to do it; the checks below are the
        # same grants ``add`` applies, not a weaker path to the same write.
        delivery_override = _parse_cron_delivery(delivery)
        target_job = await sched.get_job(job_id)
        if target_job is None:
            raise SafeToolError(f"Job not found: {job_id}")
        if _cron_job_agent_id(target_job) != current_agent_id:
            raise SafeToolError("cron job belongs to a different profile")

        current_payload: dict[str, Any] = (
            target_job.payload if isinstance(getattr(target_job, "payload", None), dict) else {}
        )
        current_kind = payload_kind(current_payload, target_job.session_target)
        new_kind = str(job_kind or current_kind)
        new_target = str(session_target or _enum_value(target_job.session_target, "isolated"))
        _validate_kind_and_target(new_kind, new_target)

        # A stored script or elevated policy survives the edit, so editing the
        # job that carries one is the same operator decision as creating it —
        # otherwise a chat message could repoint an unattended shell.
        carries_script = bool(payload_script(current_payload)) or current_kind == SCRIPT_KIND
        if (carries_script or script or new_kind == SCRIPT_KIND) and not _operator_caller(ctx):
            raise SafeToolError(
                "updating a scheduled script requires an interactive CLI or Web caller"
            )
        # Note: This gate only blocks updates to jobs with explicit per-job elevation requests.
        if dict(getattr(target_job, "tool_policy", None) or {}).get("elevated"):
            if not _operator_caller(ctx):
                raise SafeToolError(
                    "updating an elevated cron job requires an interactive CLI or Web caller"
                )
        # Rewriting what a job says is a routing grant when the job announces
        # somewhere the caller cannot post: the edit is refused rather than
        # quietly authoring content for another channel or a webhook.
        if (task is not None or job_kind is not None) and channel_caller:
            if not _delivery_targets_caller(getattr(target_job, "delivery", None), ctx):
                raise SafeToolError(
                    "that job reports to a destination this chat cannot address, "
                    "so its content can only be edited by an interactive CLI or Web caller"
                )

        # Moving where a job announces is the same grant as rewriting what it
        # says, and is checked before anything is written: `enabled` below
        # pauses/resumes as a side effect, so a rejected destination must not
        # leave a half-applied edit behind.
        new_delivery: DeliveryConfig | None = None
        if delivery_override is not None:
            if delivery_override["mode"] == "channel":
                caller_kind = getattr(ctx, "caller_kind", None) if ctx is not None else None
                if caller_kind not in (CallerKind.CLI, CallerKind.WEB):
                    raise SafeToolError(
                        "delivery.mode='channel' requires an interactive CLI or Web caller; "
                        "from a chat a job delivers back to the calling conversation"
                    )
                if new_target == "main":
                    raise SafeToolError(
                        "delivery.mode='channel' is unavailable for session_target=main; "
                        "use session_target=isolated"
                    )
                _validate_cron_delivery_channel(delivery_override["channel_name"])
                try:
                    validate_channel_target(
                        delivery_override["channel_name"],
                        delivery_override["channel_id"],
                    )
                except ValueError as exc:
                    # Caught at save time on purpose: an unusable recipient is
                    # otherwise only discovered when the job fires.
                    raise SafeToolError(str(exc)) from exc
            if channel_caller and not _delivery_targets_caller(
                getattr(target_job, "delivery", None), ctx
            ):
                raise SafeToolError(
                    "that job reports to a destination this chat cannot address, "
                    "so its delivery can only be changed by an interactive CLI or Web caller"
                )
            new_delivery = await _cron_update_delivery(
                delivery_override,
                target_job,
                ctx,
                caller_session_key,
                new_target,
            )

        if task is not None:
            blocked, reason = _scan_cron_prompt(task)
            if blocked:
                raise SafeToolError(reason)
        if script:
            script_error = validate_script_path(script)
            if script_error:
                raise SafeToolError(script_error)

        patch: dict[str, Any] = {}
        if new_delivery is not None:
            patch["delivery"] = new_delivery
        if name is not None and name.strip():
            patch["name"] = name.strip()
        if enabled is not None:
            # `enabled` and `status` are two different gates on firing, so the
            # flag alone would report a paused job as back on while it stayed
            # parked. Pause/resume are separate ops because resuming recomputes
            # next_run_at; the RPC layer pairs them the same way.
            patch["enabled"] = bool(enabled)
            if enabled:
                if _enum_value(getattr(target_job, "status", "")) == "paused":
                    resumed = await sched.resume_job(job_id)
                    if resumed is not None:
                        target_job = resumed
            else:
                paused = await sched.pause_job(job_id)
                if paused is not None:
                    target_job = paused
        if wake_mode is not None:
            normalized_wake = str(wake_mode).strip().lower()
            if normalized_wake not in ("now", "next-heartbeat"):
                raise SafeToolError("wake_mode must be now or next-heartbeat")
            patch["wake_mode"] = normalized_wake
        if tool_policy is not None:
            patch["tool_policy"] = tool_policy

        if schedule is not None:
            patch_kind, patch_value, patch_tz = _coerce_tool_schedule(schedule, tz=tz)
            patch["schedule_kind"] = patch_kind
            patch["schedule_value"] = patch_value
            # Only touch the timezone when one was actually supplied: an empty
            # schedule_tz would clear the job's tz, which is the silent
            # UTC-rewrite this action exists to avoid.
            if patch_tz or tz:
                patch["schedule_tz"] = patch_tz or tz
        elif tz:
            patch["tz"] = tz
            if target_job.schedule_kind == ScheduleKind.CRON:
                # next_run_at is computed in the job's timezone, so a bare tz
                # change has to re-run the schedule to take effect.
                patch["schedule_kind"] = ScheduleKind.CRON
                patch["schedule_value"] = str(target_job.cron_expr or "")
                patch["schedule_tz"] = tz

        payload_touched = (
            task is not None
            or job_kind is not None
            or script is not None
            or script_args is not None
            or bool(workdir)
            or session_target is not None
            or target_session_key is not None
        )
        if payload_touched:
            payload_agent = payload_agent_id(current_payload, current_agent_id)
            # A script payload's "text" is its path, not a prompt — it must not
            # become the prompt when a job is converted away from script.
            inherited_text = (
                ""
                if current_kind == SCRIPT_KIND
                else payload_text(current_payload, target_job.session_target)
            )
            new_text = task if task is not None else inherited_text
            new_script = normalize_script_value(
                script if script is not None else payload_script(current_payload)
            )
            new_workdir = (workdir or payload_workdir(current_payload)).strip()
            new_args = [
                str(arg)
                for arg in (
                    script_args if script_args is not None else payload_args(current_payload)
                )
            ]
            if new_kind == SCRIPT_KIND:
                if not new_script:
                    raise SafeToolError("job_kind='script' requires 'script'")
                patch["payload"] = make_script_payload(
                    new_script, payload_agent, new_workdir, new_args
                )
            else:
                if not new_text.strip():
                    raise SafeToolError("'task' is required for a non-script job")
                if new_script and new_kind != AGENT_TURN_KIND:
                    raise SafeToolError(
                        "'script' is only used by job_kind='script' or 'agent_turn'"
                    )
                if new_kind == SYSTEM_EVENT_KIND:
                    patch["payload"] = make_system_event_payload(new_text, payload_agent)
                elif new_kind == REMINDER_KIND:
                    patch["payload"] = make_reminder_payload(new_text, payload_agent)
                else:
                    patch["payload"] = make_agent_turn_payload(
                        new_text, payload_agent, new_script, new_workdir, new_args
                    )
            patch["session_target"] = SessionTarget(new_target)
            if new_target == "current":
                bound_key = str(getattr(target_job, "session_key", "") or "") or caller_session_key
                if not bound_key:
                    raise SafeToolError("session_target=current requires a caller session context")
                patch["session_key"] = bound_key
            elif new_target == "session":
                bound_key = target_session_key or str(getattr(target_job, "session_key", "") or "")
                if not bound_key:
                    raise SafeToolError(
                        "target_session_key is required when session_target=session"
                    )
                patch["session_key"] = bound_key
            else:
                patch["session_key"] = ""

        if not patch:
            raise SafeToolError(
                "update needs at least one field to change (schedule, task, name, "
                "job_kind, session_target, tool_policy, wake_mode, tz, delivery, "
                "or enabled)"
            )
        try:
            updated = await sched.update_job(job_id, **patch)
        except ValueError as exc:
            raise SafeToolError(str(exc)) from exc
        if updated is None:
            raise SafeToolError(f"Job not found: {job_id}")
        return json.dumps({"action": "update", "job": _cron_job_view(updated)})

    if action == "remove":
        assert job_id is not None
        target_job = await sched.get_job(job_id)
        if target_job is None:
            raise SafeToolError(f"Job not found: {job_id}")
        if _cron_job_agent_id(target_job) != current_agent_id:
            raise SafeToolError("cron job belongs to a different profile")
        removed = await sched.remove_job(job_id)
        if not removed:
            raise SafeToolError(f"Job not found: {job_id}")
        return json.dumps({"action": "remove", "job_id": job_id, "status": "removed"})

    # run
    assert job_id is not None
    target_job = await sched.get_job(job_id)
    if target_job is None:
        raise SafeToolError(f"Job not found: {job_id}")
    if _cron_job_agent_id(target_job) != current_agent_id:
        raise SafeToolError("cron job belongs to a different profile")
    result = await sched.run_job_now(job_id)
    status = getattr(result, "status", "")
    status_str = status.value if hasattr(status, "value") else str(status)
    execution = getattr(result, "execution", None)
    run_payload: dict[str, Any] = {
        "action": "run",
        "job_id": job_id,
        "status": status_str,
    }
    if execution is not None:
        run_payload["success"] = execution.success
        run_payload["summary"] = execution.summary
        run_payload["error"] = execution.error
    else:
        run_payload["success"] = False
        run_payload["reason"] = getattr(result, "reason", "") or status_str
        run_payload["error"] = getattr(result, "error", None)
        current_status = getattr(result, "current_status", "")
        if current_status:
            run_payload["current_status"] = current_status
        backoff_until = getattr(result, "backoff_until", None)
        if backoff_until is not None:
            run_payload["backoff_until"] = backoff_until.isoformat()
    return json.dumps(run_payload)


# ---------------------------------------------------------------------------
# gateway
# ---------------------------------------------------------------------------


@tool(
    name="gateway",
    description="Gateway control: restart and configuration management.",
    params={
        "action": {
            "type": "string",
            "description": "Action: restart, config_get, config_set",
        },
        "key": {
            "type": "string",
            "description": "Config key path (required for config_get and config_set)",
        },
        "value": {
            "type": "string",
            "description": "Config value as JSON string (required for config_set)",
        },
    },
    required=["action"],
    exposed_by_default=False,
)
async def gateway(
    action: str,
    key: str | None = None,
    value: str | None = None,
) -> str:
    if action not in _VALID_GATEWAY_ACTIONS:
        raise ToolError(f"Invalid action: {action}. Must be restart|config_get|config_set")

    if action in ("config_get", "config_set") and not key:
        raise ToolError(f"'key' required for {action}")
    if action == "config_set" and value is None:
        raise ToolError("'value' required for config_set")

    # Parse JSON value for config_set
    parsed_value = None
    if action == "config_set":
        assert value is not None
        try:
            parsed_value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            raise ToolError("'value' must be valid JSON")

    if _gateway_config is None:
        raise ToolError("Gateway config not available")

    config = _gateway_config

    if action == "restart":
        raise ToolError("Gateway restart not supported via tool")

    if action == "config_get":
        assert key is not None
        cfg_dict = config.to_toml_dict() if hasattr(config, "to_toml_dict") else {}
        # Navigate dot-path key
        parts = key.split(".")
        val = cfg_dict
        for p in parts:
            if isinstance(val, dict):
                val = val.get(p)
            else:
                val = None
                break
        if val is None:
            raise ToolError(f"Config key not found: {key}")
        return json.dumps({"action": "config_get", "key": key, "value": val})

    # config_set
    if hasattr(config, "patch"):
        await config.patch({key: parsed_value})
        return json.dumps(
            {
                "action": "config_set",
                "key": key,
                "value": parsed_value,
            }
        )
    raise ToolError("Config modification not supported")
