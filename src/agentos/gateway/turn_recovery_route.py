"""Who a turn answers, in a form that survives a restart.

A :class:`~agentos.gateway.routing.RouteEnvelope` lives in memory; the task
ledger keeps only part of it. Restart recovery needs the rest (the channel,
chat, thread and sender a reply goes to, and the channel pairing grant the
turn was admitted under) to resume an interrupted turn the way it started.
:func:`route_snapshot` writes those fields into the task record's details at
admission; :func:`envelope_from_task` rebuilds an envelope from them.

Kept free of heavy imports: the task runtime imports it.
"""

from __future__ import annotations

from typing import Any

from agentos.gateway.routing import ReplyTarget, RouteEnvelope, SourceKind
from agentos.tools.types import InteractionMode

_ROUTE_FIELDS = (
    "session_id",
    "sender_id",
    "account_id",
    "channel_type",
    "channel_name",
    "channel_id",
    "thread_id",
)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def route_snapshot(envelope: RouteEnvelope) -> dict[str, Any]:
    """The routing fields of *envelope* as plain JSON."""
    snapshot: dict[str, Any] = {
        name: getattr(envelope, name) for name in _ROUTE_FIELDS if getattr(envelope, name, None)
    }
    mode = getattr(envelope, "interaction_mode", None)
    snapshot["interaction_mode"] = getattr(mode, "value", mode) or "interactive"
    target = envelope.reply_target
    if target is not None:
        snapshot["reply_target"] = {
            "kind": target.kind,
            "channel_name": target.channel_name,
            "channel_type": target.channel_type,
            "to": target.to,
            "account_id": target.account_id,
            "thread_id": target.thread_id,
        }
    if envelope.delivery_context:
        snapshot["delivery_context"] = dict(envelope.delivery_context)
    admission = getattr(envelope.runtime_state, "channel_admission", None)
    if admission is not None:
        snapshot["channel_admission"] = {
            "channel_name": getattr(admission, "channel_name", None),
            "sender_id": getattr(admission, "sender_id", None),
            "grant_id": getattr(admission, "grant_id", None),
            "grant_version": getattr(admission, "grant_version", None),
        }
    return snapshot


def envelope_from_task(
    record: Any,
    *,
    input_provenance: dict[str, Any],
    metadata: dict[str, Any],
) -> RouteEnvelope:
    """Rebuild the envelope of the persisted task *record*.

    *input_provenance* and *metadata* replace the original ones (a resumed
    turn is not the user's message). The channel pairing grant is not
    restored here; see ``turn_recovery`` for its revalidation.
    """
    details = _as_dict(record.details)
    route = _as_dict(details.get("route"))
    try:
        source_kind = SourceKind(str(record.source_kind))
    except ValueError:
        source_kind = SourceKind.SYSTEM
    try:
        interaction_mode = InteractionMode(str(route.get("interaction_mode") or "interactive"))
    except ValueError:
        interaction_mode = InteractionMode.INTERACTIVE
    reply_target = None
    raw_target = route.get("reply_target")
    if isinstance(raw_target, dict) and _str_or_none(raw_target.get("kind")):
        reply_target = ReplyTarget(
            kind=str(raw_target["kind"]),
            channel_name=_str_or_none(raw_target.get("channel_name")),
            channel_type=_str_or_none(raw_target.get("channel_type")),
            to=_str_or_none(raw_target.get("to")),
            account_id=_str_or_none(raw_target.get("account_id")),
            thread_id=_str_or_none(raw_target.get("thread_id")),
        )
    delivery_context = route.get("delivery_context")
    return RouteEnvelope(
        source_kind=source_kind,
        source_name=str(details.get("source_name") or "restart_recovery"),
        agent_id=str(record.agent_id or "main"),
        session_key=str(record.session_key),
        session_id=_str_or_none(route.get("session_id")),
        sender_id=_str_or_none(route.get("sender_id")),
        account_id=_str_or_none(route.get("account_id")),
        channel_type=_str_or_none(route.get("channel_type")),
        channel_name=_str_or_none(route.get("channel_name")),
        channel_id=_str_or_none(route.get("channel_id")),
        thread_id=_str_or_none(route.get("thread_id")),
        reply_target=reply_target,
        input_provenance=input_provenance,
        delivery_context=dict(delivery_context) if isinstance(delivery_context, dict) else {},
        metadata=metadata,
        interaction_mode=interaction_mode,
    )


__all__ = ["envelope_from_task", "route_snapshot"]
