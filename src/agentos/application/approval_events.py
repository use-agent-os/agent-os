"""Approval queue change events and the display shape shared by every surface.

The queue calls :func:`notify_approval_event` from its write paths (a row
queued, resolved, or expired); the gateway registers a listener that pushes
the frames to Control connections as ``approval.requested`` /
``approval.resolved``. Listeners are module-level so they keep working across
:func:`agentos.application.approval_queue.reset_approval_queue`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import structlog

log = structlog.get_logger(__name__)

APPROVAL_REQUESTED = "approval.requested"
APPROVAL_RESOLVED = "approval.resolved"

ApprovalListener = Callable[[str, dict[str, Any]], None]

_listeners: list[ApprovalListener] = []


def add_approval_listener(listener: ApprovalListener) -> Callable[[], None]:
    """Call ``listener(event_name, payload)`` on every queue change; returns a remover."""

    _listeners.append(listener)

    def _remove() -> None:
        try:
            _listeners.remove(listener)
        except ValueError:
            pass

    return _remove


def notify_approval_event(event_name: str, payload: dict[str, Any]) -> None:
    """Fan a queue change out to the listeners; a failing listener never fails the write."""

    for listener in list(_listeners):
        try:
            listener(event_name, dict(payload))
        except Exception:
            log.warning("approval_events.listener_failed", event_name=event_name, exc_info=True)


def approval_display_title(namespace: str, params: dict[str, Any] | None) -> str:
    """One readable line describing a pending approval, for any namespace.

    ACP rows carry a precomputed ``displayTitle`` (``"Codex wants to run:
    git push"``); exec and plugin rows are described from their own fields.
    """

    p = params if isinstance(params, dict) else {}
    title = p.get("displayTitle")
    if isinstance(title, str) and title.strip():
        return title.strip()
    if namespace == "plugin":
        plugin = str(p.get("pluginId") or "plugin")
        return f"Plugin {plugin} requests permissions"
    tool = str(p.get("toolName") or p.get("action_kind") or namespace or "tool")
    command = p.get("command")
    if not command and isinstance(p.get("argv"), list):
        command = " ".join(str(part) for part in p["argv"])
    return f"{tool}: {command}" if command else tool


def _command(params: dict[str, Any]) -> str:
    argv = params.get("argv")
    command = params.get("command")
    if not command and isinstance(argv, list):
        command = " ".join(str(part) for part in argv)
    return str(command or "")


def approval_display_item(pending: dict[str, Any], *, mode: str) -> dict[str, Any]:
    """A pending row as ``GET /api/approvals`` lists it (and the console renders it)."""

    params = pending.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    argv = params.get("argv")
    return {
        "id": pending["id"],
        "namespace": pending["namespace"],
        "created_at": pending.get("created_at"),
        "toolName": params.get(
            "toolName",
            params.get("pluginId", params.get("action_kind", "Unknown")),
        ),
        "sessionKey": params.get("sessionKey", ""),
        # The requesting session and its subagent / coding-agent ancestors:
        # a parent's view matches a child's prompt on this.
        "audienceSessionKeys": list(pending.get("audience_session_keys") or []),
        "agent": params.get("agent", ""),
        "args": params.get("args", params.get("permissions")),
        "command": _command(params),
        "warning": params.get("warning", params.get("reason", "")),
        "actionKind": params.get("action_kind", ""),
        "argv": argv if isinstance(argv, list) else [],
        "mode": params.get("mode", mode),
        "params": params,
        "title": approval_display_title(str(pending["namespace"]), params),
    }


def approval_requested_payload(
    *,
    approval_id: str,
    namespace: str,
    params: dict[str, Any] | None,
    created_at: float | None,
    expires_at: float | None,
    mode: str,
    audience_session_keys: Sequence[str] = (),
) -> dict[str, Any]:
    """``approval.requested``: summary fields (unknown ones omitted) plus the display item.

    ``audience_session_keys`` lists the sessions whose viewers should see the
    prompt (the requester first, then its ancestors); omitted when empty.
    """

    p = params if isinstance(params, dict) else {}
    payload: dict[str, Any] = {"id": approval_id, "namespace": namespace}
    session_key = p.get("sessionKey") or p.get("session_key")
    if isinstance(session_key, str) and session_key:
        payload["session_key"] = session_key
    audience = [key for key in audience_session_keys if key]
    if audience:
        payload["audience_session_keys"] = audience
    if created_at is not None:
        payload["created_at"] = created_at
    if expires_at is not None:
        payload["expires_at"] = expires_at
    payload["summary"] = approval_display_title(namespace, p)
    tool = p.get("toolName") or p.get("pluginId") or p.get("action_kind")
    if isinstance(tool, str) and tool:
        payload["tool"] = tool
    command = _command(p)
    if command:
        payload["command"] = command
    payload["approval"] = approval_display_item(
        {
            "id": approval_id,
            "namespace": namespace,
            "params": p,
            "created_at": created_at,
            "audience_session_keys": audience,
        },
        mode=mode,
    )
    return payload


def approval_resolved_payload(
    *,
    approval_id: str,
    namespace: str,
    decision: str,
    resolved_by: str | None,
    resolved_at: float | None,
) -> dict[str, Any]:
    """``approval.resolved``: ``decision`` is ``approved``, ``denied``, ``expired`` or
    ``cancelled`` (its run was stopped)."""

    payload: dict[str, Any] = {"id": approval_id, "namespace": namespace, "decision": decision}
    if resolved_by:
        payload["resolved_by"] = resolved_by
    if resolved_at is not None:
        payload["resolved_at"] = resolved_at
    return payload


__all__ = [
    "APPROVAL_REQUESTED",
    "APPROVAL_RESOLVED",
    "ApprovalListener",
    "add_approval_listener",
    "approval_display_item",
    "approval_display_title",
    "approval_requested_payload",
    "approval_resolved_payload",
    "notify_approval_event",
]
