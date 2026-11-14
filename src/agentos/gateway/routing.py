"""Canonical route envelopes for gateway, CLI, scheduler, and subagent turns."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog

from agentos.channel_pairing import ChannelAdmission
from agentos.channels.types import IncomingMessage
from agentos.permissions import CRON_ELEVATED_MODES, cron_tool_policy_elevated
from agentos.session.keys import normalize_agent_id, parse_agent_id
from agentos.tools.authority import (
    AUTHORITY_METADATA_KEY,
    apply_authority_ceiling,
    authority_from_metadata,
    authority_to_metadata,
)
from agentos.tools.policy import apply_tool_policy_layer
from agentos.tools.types import (
    CRON_AGENT_ALLOW,
    CRON_AGENT_DENY,
    CRON_ELEVATED_ALLOW,
    CRON_ELEVATED_DENY,
    SUBAGENT_TOOL_DENY,
    CallerKind,
    InteractionMode,
    ToolAuthority,
    ToolContext,
)

log = structlog.get_logger(__name__)


class SourceKind(StrEnum):
    """Top-level inbound runtime source."""

    WEB = "web"
    CLI = "cli"
    CHANNEL = "channel"
    CRON = "cron"
    SUBAGENT = "subagent"
    SYSTEM = "system"


@dataclass(frozen=True)
class ReplyTarget:
    """External or subscriber target that can receive a reply/announce."""

    kind: str
    channel_name: str | None = None
    channel_type: str | None = None
    to: str | None = None
    account_id: str | None = None
    thread_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RouteRuntimeState:
    """In-process route state that must never enter persisted metadata."""

    channel_admission: ChannelAdmission | None = None
    channel_admission_validator: Callable[[ChannelAdmission], bool] | None = None

    def clear_channel_admission(self) -> None:
        self.channel_admission = None
        self.channel_admission_validator = None


@dataclass(frozen=True)
class RouteEnvelope:
    """Canonical routing data for one inbound turn request."""

    source_kind: SourceKind
    source_name: str
    agent_id: str
    session_key: str
    session_id: str | None = None
    sender_id: str | None = None
    account_id: str | None = None
    channel_type: str | None = None
    channel_name: str | None = None
    channel_id: str | None = None
    thread_id: str | None = None
    reply_target: ReplyTarget | None = None
    input_provenance: dict[str, Any] = field(default_factory=dict)
    delivery_context: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    interaction_mode: InteractionMode = InteractionMode.INTERACTIVE
    runtime_state: RouteRuntimeState = field(
        default_factory=RouteRuntimeState,
        compare=False,
        repr=False,
    )

    def delivery_fields(self) -> dict[str, Any]:
        """Return session routing fields derived from the reply target."""
        return delivery_fields_from_envelope(self)

    def tool_context(
        self,
        *,
        workspace_dir: str | None = None,
        workspace_strict: bool = False,
        default_elevated: str | None = None,
    ) -> ToolContext:
        """Build the ToolContext for this route."""
        return tool_context_from_envelope(
            self,
            workspace_dir=workspace_dir,
            workspace_strict=workspace_strict,
            default_elevated=default_elevated,
        )


def _agent_id(agent_id: str | None, session_key: str) -> str:
    return normalize_agent_id(agent_id) if agent_id else parse_agent_id(session_key)


def _thread_id(metadata: dict[str, Any]) -> str | None:
    thread = metadata.get("thread_ts") or metadata.get("thread_id")
    return thread if isinstance(thread, str) and thread else None


def build_channel_route_envelope(
    msg: IncomingMessage,
    *,
    session_key: str,
    session_prefix: str,
    agent_id: str | None = None,
    channel_type: str | None = None,
) -> RouteEnvelope:
    """Build a route for a normalized inbound channel message."""
    metadata = dict(msg.metadata or {})
    resolved_agent_id = _agent_id(agent_id, session_key)
    resolved_channel_type = channel_type or session_prefix
    account_id = metadata.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        account_id = None
    thread_id = _thread_id(metadata)
    delivery_context = {
        "sender_id": msg.sender_id,
        "channel_id": msg.channel_id,
        **metadata,
    }
    return RouteEnvelope(
        source_kind=SourceKind.CHANNEL,
        source_name=session_prefix,
        agent_id=resolved_agent_id,
        session_key=session_key,
        sender_id=msg.sender_id,
        account_id=account_id,
        channel_type=resolved_channel_type,
        channel_name=session_prefix,
        channel_id=msg.channel_id,
        thread_id=thread_id,
        reply_target=ReplyTarget(
            kind="channel",
            channel_name=session_prefix,
            channel_type=resolved_channel_type,
            to=msg.channel_id,
            account_id=account_id,
            thread_id=thread_id,
            metadata=metadata,
        ),
        input_provenance={
            "kind": "channel_message",
            "source": session_prefix,
        },
        delivery_context=delivery_context,
        metadata=metadata,
        interaction_mode=InteractionMode.UNATTENDED,
    )


def build_cli_route_envelope(
    *,
    session_key: str,
    agent_id: str | None = None,
    source_name: str = "run",
    channel_id: str = "cli:agent",
    sender_id: str | None = None,
    session_id: str | None = None,
    interaction_mode: InteractionMode | str = InteractionMode.INTERACTIVE,
    elevated: str | None = None,
) -> RouteEnvelope:
    """Build a route for local CLI input."""
    resolved_interaction_mode = _interaction_mode(interaction_mode)
    metadata: dict[str, Any] = {}
    if elevated in ("on", "bypass", "full"):
        metadata["elevated"] = elevated
    return RouteEnvelope(
        source_kind=SourceKind.CLI,
        source_name=source_name,
        agent_id=_agent_id(agent_id, session_key),
        session_key=session_key,
        session_id=session_id,
        sender_id=sender_id,
        channel_type="cli",
        channel_name="cli",
        channel_id=channel_id,
        input_provenance={"kind": "cli_message", "source": source_name},
        metadata=metadata,
        interaction_mode=resolved_interaction_mode,
    )


def build_web_route_envelope(
    *,
    session_key: str,
    agent_id: str | None = None,
    source_name: str = "web",
    conn_id: str | None = None,
    sender_id: str | None = None,
    channel_id: str | None = None,
    session_id: str | None = None,
    tool_source_kind: str | None = None,
) -> RouteEnvelope:
    """Build a route for Web/RPC-originated input."""
    resolved_channel_id = channel_id or (f"web:{conn_id}" if conn_id else "web")
    channel_name = "webchat" if resolved_channel_id.startswith("webchat:") else "web"
    metadata: dict[str, Any] = {"conn_id": conn_id}
    if tool_source_kind:
        metadata["tool_source_kind"] = tool_source_kind
    return RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name=source_name,
        agent_id=_agent_id(agent_id, session_key),
        session_key=session_key,
        session_id=session_id,
        sender_id=sender_id,
        channel_type="web",
        channel_name=channel_name,
        channel_id=resolved_channel_id,
        reply_target=ReplyTarget(
            kind="web",
            channel_name=channel_name,
            channel_type="web",
            to=conn_id,
        ),
        input_provenance={"kind": "web_message", "source": source_name},
        delivery_context={"sender_id": sender_id, "channel_id": resolved_channel_id},
        metadata=metadata,
        interaction_mode=InteractionMode.INTERACTIVE,
    )


def build_cron_route_envelope(
    job: Any,
    *,
    session_key: str,
    agent_id: str | None = None,
    delivery: Any | None = None,
) -> RouteEnvelope:
    """Build a route for scheduler-originated agent work or delivery."""
    resolved_delivery = delivery if delivery is not None else getattr(job, "delivery", None)
    job_id = str(getattr(job, "id", "unknown"))
    job_name = str(getattr(job, "name", ""))
    sender_id = f"cron-job-{job_id}"
    metadata: dict[str, Any] = {"job_id": job_id, "job_name": job_name}
    handler_key = getattr(job, "handler_key", None)
    if handler_key:
        metadata["handler_key"] = str(handler_key)
    tool_policy = getattr(job, "tool_policy", None)
    if isinstance(tool_policy, dict) and tool_policy:
        metadata["tool_policy"] = dict(tool_policy)
    reply_target = None
    delivery_context = {
        "sender_id": sender_id,
        "channel_id": "",
        "job_id": job_id,
        "job_name": job_name,
    }
    if (
        resolved_delivery is not None
        and getattr(resolved_delivery, "mode", None) != "none"
        and getattr(resolved_delivery, "channel_name", "")
    ):
        channel_name = getattr(resolved_delivery, "channel_name", "")
        channel_id = getattr(resolved_delivery, "channel_id", "")
        account_id = getattr(resolved_delivery, "account_id", "")
        thread_id = getattr(resolved_delivery, "thread_id", "")
        reply_target = ReplyTarget(
            kind="channel",
            channel_name=channel_name,
            channel_type=channel_name,
            to=channel_id,
            account_id=account_id or None,
            thread_id=thread_id or None,
        )
        delivery_context["channel_id"] = channel_id
    return RouteEnvelope(
        source_kind=SourceKind.CRON,
        source_name="cron",
        agent_id=_agent_id(agent_id, session_key),
        session_key=session_key,
        sender_id=sender_id,
        channel_type="cron",
        channel_name="cron",
        channel_id=f"cron:{job_id}",
        reply_target=reply_target,
        input_provenance={"kind": "cron_job", "job_id": job_id},
        delivery_context=delivery_context,
        metadata=metadata,
        interaction_mode=InteractionMode.UNATTENDED,
    )


def build_subagent_route_envelope(
    *,
    session_key: str,
    parent_session_key: str,
    agent_id: str | None = None,
    run_id: str | None = None,
    parent_task_id: str | None = None,
    spawn_depth: int = 0,
    origin: str = "sessions_spawn",
    authority: ToolAuthority | None = None,
) -> RouteEnvelope:
    """Build a route for a child subagent run.

    *authority* is the parent's tool authority; the child's surface is
    clipped to it (see :mod:`agentos.tools.authority`).
    """
    metadata: dict[str, Any] = {
        "parent_session_key": parent_session_key,
        "run_id": run_id,
        "parent_task_id": parent_task_id,
        "spawn_depth": spawn_depth,
        "origin": origin,
    }
    if authority is not None:
        metadata[AUTHORITY_METADATA_KEY] = authority_to_metadata(authority)
    return RouteEnvelope(
        source_kind=SourceKind.SUBAGENT,
        source_name="subagent",
        agent_id=_agent_id(agent_id, session_key),
        session_key=session_key,
        channel_type="subagent",
        channel_name="subagent",
        channel_id=run_id,
        input_provenance={
            "kind": "subagent_task",
            "parent_session_key": parent_session_key,
            "run_id": run_id,
            "parent_task_id": parent_task_id,
        },
        metadata=metadata,
        interaction_mode=InteractionMode.UNATTENDED,
    )


def delivery_fields_from_envelope(envelope: RouteEnvelope) -> dict[str, Any]:
    """Translate a channel-capable route into SessionNode delivery fields."""
    target = envelope.reply_target
    if target is None or target.kind != "channel":
        return {}
    return {
        "last_channel": target.channel_name,
        "last_to": target.to,
        "last_account_id": target.account_id,
        "last_thread_id": target.thread_id,
        "delivery_context": dict(envelope.delivery_context),
    }


def tool_context_from_envelope(
    envelope: RouteEnvelope,
    *,
    workspace_dir: str | None = None,
    workspace_strict: bool = False,
    default_elevated: str | None = None,
    cron_default_elevated: str | None = None,
) -> ToolContext:
    """Build the runtime ToolContext from the canonical route envelope."""
    caller_kind = _caller_kind(envelope.source_kind)
    allowed_tools: set[str] | None = None
    denied_tools: set[str] = set()
    interaction_mode = _interaction_mode(envelope.interaction_mode)
    cron_elevated: str | None = None
    cron_baseline_allow = CRON_AGENT_ALLOW
    cron_baseline_deny = CRON_AGENT_DENY
    if caller_kind is CallerKind.CRON:
        tool_policy = envelope.metadata.get("tool_policy")
        handler_key = envelope.metadata.get("handler_key")
        has_explicit = isinstance(tool_policy, dict) and "elevated" in tool_policy
        if has_explicit:
            cron_elevated = cron_tool_policy_elevated(tool_policy)
            cron_source = "job"
        elif handler_key == "agent_run":
            cron_elevated = cron_default_elevated
            cron_source = "config"
        else:
            cron_elevated = None
            cron_source = None

        if cron_elevated in CRON_ELEVATED_MODES:
            cron_baseline_allow = CRON_ELEVATED_ALLOW
            cron_baseline_deny = CRON_ELEVATED_DENY
            # An unattended turn is about to get a real shell. Logged here
            # rather than in the scheduler because both the task-runtime and
            # the legacy turn-runner path funnel through this one function.
            log.warning(
                "cron_elevated_tool_surface",
                job_id=envelope.metadata.get("job_id"),
                job_name=envelope.metadata.get("job_name"),
                agent_id=envelope.agent_id,
                mode=cron_elevated,
                source=cron_source,
            )
        allowed_tools = set(cron_baseline_allow)
        denied_tools = set(cron_baseline_deny)
    elif caller_kind is CallerKind.SUBAGENT:
        denied_tools = set(SUBAGENT_TOOL_DENY)
    if caller_kind not in {CallerKind.CRON, CallerKind.SUBAGENT}:
        # Plan mode: the session is research-only until the plan is approved.
        # Applied as an ALLOWLIST intersection so a tool added later is never
        # granted to plan mode by omission, and enforced both at tool-list
        # time (visibility) and at dispatch time (policy chain allowlist
        # check). Cron and subagent turns keep their own surfaces — plan mode
        # is a property of the user-facing session, not of background work.
        # Channel turns count as user-facing even though they are marked
        # unattended: a human is on the other end to approve the plan.
        from agentos.plan_mode import PLAN_MODE_TOOL_ALLOW, get_plan_mode_store

        if get_plan_mode_store().is_enabled(envelope.session_key):
            allowed_tools = (
                set(PLAN_MODE_TOOL_ALLOW)
                if allowed_tools is None
                else allowed_tools & PLAN_MODE_TOOL_ALLOW
            )
    source_kind = envelope.metadata.get("tool_source_kind") or envelope.source_kind.value
    source_name = envelope.metadata.get("tool_source_name") or envelope.source_name
    channel_admission = envelope.runtime_state.channel_admission
    channel_admission_validator = envelope.runtime_state.channel_admission_validator
    if not isinstance(channel_admission, ChannelAdmission):
        channel_admission = None
        channel_admission_validator = None
    if not callable(channel_admission_validator):
        channel_admission_validator = None
    elevated = envelope.metadata.get("elevated") or default_elevated
    if caller_kind is CallerKind.CRON:
        elevated = cron_elevated if cron_elevated in CRON_ELEVATED_MODES else None
    elif (
        elevated not in ("on", "bypass", "full")
        or caller_kind not in {CallerKind.CLI, CallerKind.WEB}
        or interaction_mode is not InteractionMode.INTERACTIVE
    ):
        elevated = None
    ctx = ToolContext(
        caller_kind=caller_kind,
        interaction_mode=interaction_mode,
        subagent_depth=int(envelope.metadata.get("spawn_depth") or 0),
        agent_id=envelope.agent_id,
        workspace_dir=workspace_dir,
        workspace_strict=workspace_strict,
        session_key=envelope.session_key,
        channel_kind=envelope.channel_name or envelope.channel_type,
        channel_id=envelope.channel_id,
        sender_id=envelope.sender_id,
        source_kind=source_kind,
        source_name=source_name,
        channel_admission=channel_admission,
        channel_admission_validator=channel_admission_validator,
        allowed_tools=allowed_tools,
        denied_tools=denied_tools,
        elevated=elevated,
        tool_policy=envelope.metadata.get("tool_policy"),
        # Work another context started (subagent, sessions_send) never runs
        # with more than that context had. Applied here and again after the
        # config policy layers in resolve_runtime_tool_surface.
        authority_ceiling=authority_from_metadata(envelope.metadata),
    )
    ctx = apply_authority_ceiling(ctx)
    if caller_kind is CallerKind.CRON:
        ctx = apply_tool_policy_layer(
            ctx,
            envelope.metadata.get("tool_policy"),
            available_tools=cron_baseline_allow | cron_baseline_deny,
            hard_denied=cron_baseline_deny,
        )
    return ctx


def _interaction_mode(value: InteractionMode | str) -> InteractionMode:
    if isinstance(value, InteractionMode):
        return value
    return InteractionMode(str(value))


def _caller_kind(source_kind: SourceKind) -> CallerKind:
    match source_kind:
        case SourceKind.WEB:
            return CallerKind.WEB
        case SourceKind.CLI:
            return CallerKind.CLI
        case SourceKind.CHANNEL:
            return CallerKind.CHANNEL
        case SourceKind.CRON:
            return CallerKind.CRON
        case SourceKind.SUBAGENT:
            return CallerKind.SUBAGENT
        case SourceKind.SYSTEM:
            return CallerKind.AGENT
