"""Channel-side slash-command dispatcher — adapter over the unified registry.

``DEFAULT_COMMAND_REGISTRY`` is derived from
:data:`agentos.engine.commands.DEFAULT_REGISTRY` rather than holding its
own hard-coded command table. The ``CommandRegistry.match`` and ``dispatch``
API is preserved for existing callers (``gateway/boot.py``,
``gateway/channel_dispatch.py``).

The channel-side slash-intercept-pre-persist invariant
(``channel_dispatch.py``) stays where it lives; this module only
provides the dispatch lookup table.
"""

from __future__ import annotations

from typing import Any

from agentos.channels.types import OutgoingMessage
from agentos.engine.commands import DEFAULT_REGISTRY, ExecutionKind, ParamsFactory, Surface
from agentos.gateway.auth import Principal
from agentos.gateway.routing import RouteEnvelope, SourceKind
from agentos.gateway.rpc import RpcContext
from agentos.gateway.scopes import READ_SCOPE, WRITE_SCOPE

_CHANNEL_REPLY_LIMIT = 1900
_CHANNEL_ITEM_LIMIT = 320


class CommandRegistry:
    """Channel-mode dispatcher.

    Matches inbound channel messages against a registered slash-command set
    and forwards the resulting RPC call to the gateway dispatcher. Lookup
    keys are bare command names (without leading slash, lowercased).
    """

    def __init__(self, commands: dict[str, tuple[str, ParamsFactory]]) -> None:
        self._commands = commands

    @property
    def command_names(self) -> set[str]:
        return set(self._commands)

    def match(self, envelope: RouteEnvelope, content: str) -> tuple[str, str, ParamsFactory] | None:
        head = content.strip().split(maxsplit=1)[0] if content.strip() else ""
        if (
            envelope.source_kind is not SourceKind.CHANNEL
            or not head.startswith("/")
            or head == "/"
        ):
            return None
        bare = head[1:].lower()
        command = self._commands.get(bare)
        return (bare, *command) if command else None

    async def dispatch(
        self,
        *,
        envelope: RouteEnvelope,
        message_content: str,
        rpc_dispatcher: Any,
        context_factory: Any,
    ) -> OutgoingMessage | None:
        match = self.match(envelope, message_content)
        if match is None:
            return None
        name, method, params_factory = match
        params = params_factory(envelope)
        if method == "chat.history":
            params = {**params, "limit": 10}
        res = await rpc_dispatcher.dispatch(
            f"channel-command:{name}",
            method,
            params,
            context_factory(envelope),
        )
        compact_reply = _format_channel_compact_reply(
            name=name,
            method=method,
            res=res,
            reply_to=envelope.thread_id or envelope.channel_id,
        )
        if compact_reply is not None:
            return compact_reply
        denied = bool(not res.ok and getattr(res.error, "code", "") == "UNAUTHORIZED")
        reason = "" if res.ok else f": {getattr(res.error, 'message', 'command failed')}"
        state = "completed" if res.ok else ("denied" if denied else "failed")
        content = f"/{name} {state}{reason}"
        if res.ok:
            rendered = _format_channel_success_reply(
                name=name,
                method=method,
                payload=res.payload,
            )
            if rendered is not None:
                content = _bound_channel_text(rendered)
        return OutgoingMessage(
            content=content,
            reply_to=envelope.thread_id or envelope.channel_id,
            metadata={"command": name, "method": method, "denied": denied},
        )


def _format_channel_compact_reply(
    *,
    name: str,
    method: str,
    res: Any,
    reply_to: str | None,
) -> OutgoingMessage | None:
    if name != "compact" or method != "sessions.contextCompact":
        return None
    denied = bool(not res.ok and getattr(res.error, "code", "") == "UNAUTHORIZED")
    metadata = {"command": name, "method": method, "denied": denied}
    if not res.ok:
        error_message = getattr(res.error, "message", "command failed")
        state = "denied" if denied else "failed"
        return OutgoingMessage(
            content=f"Compact {state}: {error_message}",
            reply_to=reply_to,
            metadata=metadata,
        )
    payload = res.payload if isinstance(res.payload, dict) else {}
    status = str(payload.get("status") or "").lower()
    compacted = bool(payload.get("compacted"))
    if compacted or status == "completed":
        return OutgoingMessage(
            content="Context compacted.",
            reply_to=reply_to,
            metadata=metadata,
        )
    if status == "skipped" or payload.get("compacted") is False:
        return OutgoingMessage(
            content="Already within context budget; no compact was applied.",
            reply_to=reply_to,
            metadata=metadata,
        )
    return OutgoingMessage(
        content="/compact completed",
        reply_to=reply_to,
        metadata=metadata,
    )


def _bound_channel_text(text: str, limit: int = _CHANNEL_REPLY_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _plain(value: Any, limit: int = _CHANNEL_ITEM_LIMIT) -> str:
    text = " ".join(str(value or "").split())
    return _bound_channel_text(text, limit)


def _format_channel_success_reply(*, name: str, method: str, payload: Any) -> str | None:
    if method == "sessions.abort" and isinstance(payload, dict) and "aborted" in payload:
        return "Turn aborted." if payload["aborted"] else "No turn is currently running."
    if method == "router.hold.clear" and isinstance(payload, dict) and "cleared" in payload:
        if payload["cleared"]:
            return "Automatic model routing restored."
        return "Automatic model routing is already active."
    if method == "router.hold.set":
        return _format_router_hold(payload)
    if method == "commands.list_for_surface":
        return _format_help(payload)
    if method == "chat.history":
        return _format_history(payload)
    if method == "doctor.memory.status":
        return _format_memory(payload)
    if method == "models.list":
        return _format_models(payload)
    if method == "sessions.reset" and isinstance(payload, dict) and payload.get("reset"):
        return "Started a new chat session." if name == "new" else "Conversation context reset."
    if method == "skills.list":
        return _format_skills(payload)
    if method == "status":
        return _format_status(payload)
    if method == "usage.status":
        return _format_usage(payload)
    return None


def _format_router_hold(payload: Any) -> str | None:
    if not isinstance(payload, dict) or not payload.get("tier"):
        return None
    tier = _plain(payload["tier"], 20)
    provider = _plain(payload.get("provider"), 80)
    model = _plain(payload.get("model"), 160)
    target = "/".join(part for part in (provider, model) if part)
    detail = f": {target}" if target else ""
    ttl = payload.get("ttlSeconds")
    expiry = f" (expires in {ttl}s)" if isinstance(ttl, (int, float)) else ""
    return f"Router pinned to {tier}{detail}{expiry}."


def _format_help(payload: Any) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("commands"), list):
        return None
    lines = ["Available commands:"]
    for item in payload["commands"]:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        command = _plain(item["name"], 40)
        description = _plain(item.get("description"), 120)
        lines.append(f"{command} — {description}" if description else command)
    return "\n".join(lines)


def _format_history(payload: Any) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        return None
    lines: list[str] = []
    labels = {"user": "You", "assistant": "Agent"}
    for item in payload["messages"]:
        if not isinstance(item, dict):
            continue
        label = labels.get(str(item.get("role") or "").lower())
        text = _plain(item.get("text"))
        if label and text:
            lines.append(f"{label}: {text}")
    if not lines:
        return "No chat history yet."
    output = "Recent history:\n" + "\n".join(lines)
    if payload.get("has_more"):
        output += "\n… older messages are available."
    return output


def _format_memory(payload: Any) -> str | None:
    if not isinstance(payload, dict) or not (payload.get("status") or payload.get("backend")):
        return None
    status = _plain(payload.get("status") or "unknown", 40)
    backend = _plain(payload.get("backend") or "unknown", 80)
    lines = [f"Memory: {status} · {backend}"]
    entries = payload.get("entryCount")
    size = payload.get("sizeBytes")
    facts = []
    if isinstance(entries, int):
        facts.append(f"Entries: {entries:,}")
    if isinstance(size, (int, float)):
        facts.append(f"Size: {_human_bytes(float(size))}")
    if facts:
        lines.append(" · ".join(facts))
    lines.append(
        "Search: "
        f"vector {'ready' if payload.get('vecAvailable') else 'unavailable'}, "
        f"FTS {'ready' if payload.get('ftsAvailable') else 'unavailable'}"
    )
    return "\n".join(lines)


def _format_models(payload: Any) -> str | None:
    if not isinstance(payload, list):
        return None
    if not payload:
        return "No models reported by the active provider."
    lines = [f"Available models ({len(payload)}):"]
    visible = payload[:12]
    for item in visible:
        if not isinstance(item, dict):
            continue
        provider = _plain(item.get("provider"), 60)
        model = _plain(item.get("id") or item.get("name"), 100)
        label = "/".join(part for part in (provider, model) if part)
        if label:
            lines.append(f"• {label}")
    omitted = len(payload) - len(visible)
    if omitted:
        lines.append(f"… {omitted} more model{'s' if omitted != 1 else ''}.")
    return "\n".join(lines)


def _format_skills(payload: Any) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("skills"), list):
        return None
    skills = payload["skills"]
    if not skills:
        return "No user-invocable skills are loaded."
    lines = [f"Loaded skills ({len(skills)}):"]
    visible = skills[:15]
    for item in visible:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        skill_name = _plain(item["name"], 90)
        status = _plain(item.get("status"), 40)
        lines.append(f"• {skill_name} — {status}" if status else f"• {skill_name}")
    omitted = len(skills) - len(visible)
    if omitted:
        lines.append(f"… {omitted} more skill{'s' if omitted != 1 else ''}.")
    return "\n".join(lines)


def _format_status(payload: Any) -> str | None:
    if not isinstance(payload, dict) or not payload.get("status"):
        return None
    version = _plain(payload.get("version"), 80)
    status = _plain(payload["status"], 40)
    heading = f"AgentOS {version} · {status}" if version else f"AgentOS · {status}"
    lines = [heading]
    provider = _plain(payload.get("provider") or "not configured", 100)
    lines.append(f"Provider: {provider}")
    uptime = payload.get("uptime_ms")
    sessions = payload.get("active_sessions")
    facts = []
    if isinstance(uptime, (int, float)):
        facts.append(f"Uptime: {_human_duration(float(uptime) / 1000)}")
    if isinstance(sessions, int):
        facts.append(f"Active sessions: {sessions}")
    if facts:
        lines.append(" · ".join(facts))
    return "\n".join(lines)


def _format_usage(payload: Any) -> str | None:
    if not isinstance(payload, dict) or "totalTokens" not in payload:
        return None
    try:
        total = int(payload.get("totalTokens") or 0)
        input_tokens = int(payload.get("totalInputTokens") or 0)
        output_tokens = int(payload.get("totalOutputTokens") or 0)
        cost = float(payload.get("totalCostUsd") or 0.0)
        sessions = int(payload.get("totalSessions") or 0)
        active = int(payload.get("activeSessions") or 0)
        cache_read = int(payload.get("totalCacheReadTokens") or 0)
        cache_write = int(payload.get("totalCacheWriteTokens") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    return (
        "Gateway usage:\n"
        f"Tokens: {total:,} ({input_tokens:,} in / {output_tokens:,} out)\n"
        f"Cost: ${cost:.6f}\n"
        f"Sessions: {sessions} total / {active} active\n"
        f"Cache: {cache_read:,} read / {cache_write:,} write"
    )


def _human_bytes(size: float) -> str:
    units = ("B", "KiB", "MiB", "GiB")
    value = max(0.0, size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _human_duration(seconds: float) -> str:
    try:
        total = max(0, int(seconds))
    except (ValueError, OverflowError):
        return "unknown"
    days, remainder = divmod(total, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def build_channel_rpc_context(
    envelope: RouteEnvelope,
    *,
    gateway_config: Any,
    **handles: Any,
) -> RpcContext:
    admin_senders = getattr(gateway_config, "channel_admin_senders", {})
    sender_id = envelope.sender_id
    is_operator = bool(sender_id and sender_id in admin_senders.get(envelope.source_name, []))
    principal = Principal(
        role="operator" if is_operator else "viewer",
        scopes=frozenset({READ_SCOPE, WRITE_SCOPE}) if is_operator else frozenset(),
        is_owner=False,
        authenticated=True,
    )
    return RpcContext(
        conn_id=f"channel:{envelope.source_name}:{sender_id or 'unknown'}",
        principal=principal,
        config=gateway_config,
        originating_envelope=envelope,
        **handles,
    )


def _build_default_command_table() -> dict[str, tuple[str, ParamsFactory]]:
    """Project the unified registry's CHANNEL surface into the dispatcher table.

    Inserts both the canonical command name and any declared aliases under
    their bare (slash-stripped, lowercase) form so an alias advertised via
    ``commands.list_for_surface`` actually dispatches when typed by a
    channel user. Skips ``CommandDef`` entries that lack RPC metadata —
    channels require a method + params factory to dispatch.
    """
    table: dict[str, tuple[str, ParamsFactory]] = {}
    for cmd in DEFAULT_REGISTRY.for_surface(Surface.CHANNEL):
        execution = cmd.execution_for(Surface.CHANNEL)
        if (
            execution is None
            or execution.kind is not ExecutionKind.RPC
            or execution.rpc_method is None
            or execution.rpc_params is None
        ):
            continue
        for word in cmd.words():
            table[word.lstrip("/").lower()] = (execution.rpc_method, execution.rpc_params)
    return table


DEFAULT_COMMAND_REGISTRY = CommandRegistry(_build_default_command_table())
