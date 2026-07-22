"""Bounded, plain-text renderers for channel slash-command replies."""

from __future__ import annotations

from typing import Any

from agentos.channels.types import OutgoingMessage

CHANNEL_REPLY_LIMIT = 1900
_CHANNEL_ITEM_LIMIT = 320


def bound_channel_text(text: str, limit: int = CHANNEL_REPLY_LIMIT) -> str:
    """Keep outbound command text within the strictest supported channel budget."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _plain(value: Any, limit: int = _CHANNEL_ITEM_LIMIT) -> str:
    text = " ".join(str(value or "").split())
    return bound_channel_text(text, limit)


def format_channel_compact_reply(
    *,
    name: str,
    method: str,
    res: Any,
    reply_to: str | None,
) -> OutgoingMessage | None:
    """Render the compact command's intentionally terse status messages."""
    if name != "compact" or method != "sessions.contextCompact":
        return None
    denied = bool(not res.ok and getattr(res.error, "code", "") == "UNAUTHORIZED")
    metadata = {"command": name, "method": method, "denied": denied}
    if not res.ok:
        error_message = _plain(getattr(res.error, "message", "command failed"))
        state = "denied" if denied else "failed"
        return OutgoingMessage(
            content=bound_channel_text(f"Compact {state}: {error_message}"),
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


def format_channel_success_reply(*, name: str, method: str, payload: Any) -> str | None:
    """Render a successful RPC payload when the command has a human-facing view."""
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
