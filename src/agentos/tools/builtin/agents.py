"""Agent listing and subagent management tools."""

from __future__ import annotations

import json
from typing import Any

import structlog

from agentos.tools.authority import authority_from_context
from agentos.tools.builtin.sessions import _get_session_manager, _get_task_runtime
from agentos.tools.registry import tool
from agentos.tools.types import ToolError, current_tool_context

_VALID_SUBAGENT_ACTIONS = ("list", "kill", "steer")
_TERMINAL_STATUSES = ("done", "failed", "killed", "timeout")
_LIST_PAGE_SIZE = 100

log = structlog.get_logger(__name__)

_agent_registry: object | None = None


def set_agent_registry(registry: object | None) -> None:
    """Inject the AgentRegistry instance (called from gateway boot)."""
    global _agent_registry
    _agent_registry = registry


def _get_agent_registry() -> object:
    if _agent_registry is None:
        raise ToolError("Agent registry not available")
    return _agent_registry


def _manager_unavailable() -> ToolError:
    return ToolError("Session manager not available")


async def _current_session_key(mgr: Any) -> str | None:
    ctx = current_tool_context.get()
    current_key = ctx.session_key if ctx is not None else None
    try:
        current = await mgr.get_current_session()
        if current_key is None and current is not None:
            current_key = getattr(current, "session_key", None)
    except (AttributeError, NotImplementedError):
        pass
    return current_key


async def _list_children(mgr: Any, parent_key: str) -> list[dict[str, Any]]:
    """Return every session spawned by ``parent_key``, filtered in storage.

    ``list_sessions`` caps each call at ``limit`` rows *before* any Python-side
    filtering could run, so the ``spawned_by`` filter has to go down to the
    query; pages through the result so a parent with more children than one
    page still sees all of them.
    """
    children: list[dict[str, Any]] = []
    seen: set[str] = set()
    page = 0
    while True:
        rows = await mgr.list_sessions(
            spawned_by=parent_key,
            limit=_LIST_PAGE_SIZE,
            offset=page * _LIST_PAGE_SIZE,
        )
        if not rows:
            return children
        for row in rows:
            if not isinstance(row, dict):
                continue
            # Rows are ordered by ``updated_at`` and children keep touching
            # it, so one can straddle a page boundary; list it once.
            key = row.get("session_key")
            if isinstance(key, str):
                if key in seen:
                    continue
                seen.add(key)
            children.append(row)
        if len(rows) < _LIST_PAGE_SIZE:
            return children
        page += 1


#: Fields a ``subagents(list)`` row keeps; the raw session row (with the
#: child's whole grounded task in ``origin``) ran to ~2 kB per child, and the
#: transcript cap then emptied the list.
_ACTIVE_STATUSES = frozenset({"running", "queued", "pending"})
_TASK_PREVIEW_CHARS = 80
_LABEL_CHARS = 60


def _short(value: Any, limit: int) -> Any:
    if not isinstance(value, str):
        return value
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _by_relevance(rows: list[Any]) -> list[Any]:
    """Running children first, then the most recently updated: when a long
    list is cut, the entries that matter stay."""

    def _key(row: Any) -> tuple[int, int]:
        if not isinstance(row, dict):
            return (1, 0)
        active = 0 if str(row.get("status") or "") in _ACTIVE_STATUSES else 1
        try:
            updated = -int(row.get("updated_at") or 0)
        except (TypeError, ValueError):
            updated = 0
        return (active, updated)

    return sorted(rows, key=_key)


def _compact_child(row: Any) -> Any:
    """One short row per child: key, runtime, who runs it, status, label,
    times and a task preview (plus the ACP harness facts)."""
    if not isinstance(row, dict):
        return row
    raw_origin = row.get("origin")
    origin: dict[str, Any] = raw_origin if isinstance(raw_origin, dict) else {}
    runtime = row.get("runtime") or origin.get("kind") or "subagent"
    out: dict[str, Any] = {
        "session_key": row.get("session_key"),
        "runtime": runtime,
        "status": row.get("status"),
    }
    if runtime == "acp":
        out["acp_agent"] = row.get("acp_agent") or origin.get("agent_id")
        out["acp_mode"] = row.get("acp_mode") or origin.get("mode")
        out["acp_idle"] = bool(row.get("acp_idle"))
        out["acp_closed"] = bool(row.get("acp_closed"))
    else:
        out["agent_id"] = row.get("agent_id")
    label = row.get("display_name") or row.get("label")
    if label:
        out["label"] = _short(label, _LABEL_CHARS)
    for key in ("started_at", "ended_at"):
        if row.get(key) is not None:
            out[key] = row.get(key)
    task = origin.get("task")
    if isinstance(task, str) and task.strip():
        out["task"] = _short(task, _TASK_PREVIEW_CHARS)
    return {k: v for k, v in out.items() if v is not None}


def _spawned_by(session_or_dict: object) -> object:
    if isinstance(session_or_dict, dict):
        return session_or_dict.get("spawned_by")
    return getattr(session_or_dict, "spawned_by", None)


def _check_spawned_by_or_raise(session_or_dict: object, current_key: str | None) -> None:
    if current_key is None:
        raise ToolError("session context required")
    if _spawned_by(session_or_dict) != current_key:
        raise ToolError("Session was not spawned by this session")


# ---------------------------------------------------------------------------
# agents_list
# ---------------------------------------------------------------------------


@tool(
    name="agents_list",
    concurrency="parallel",
    read_only=True,
    description="List available agent configurations from the agent registry.",
    params={},
    required=[],
    defer_loading=True,
)
async def agents_list() -> str:
    registry = _get_agent_registry()
    list_agents = getattr(registry, "list_agents", None)
    if not callable(list_agents):
        raise ToolError("Agent registry does not expose list_agents")
    entries = await list_agents(include_builtin=True)
    return json.dumps(entries)


# ---------------------------------------------------------------------------
# subagents
# ---------------------------------------------------------------------------


@tool(
    name="subagents",
    description=(
        "List, stop (kill) or steer the sessions you spawned, including coding harnesses "
        "(Claude Code, Codex, ...). When the user asks to stop a harness: "
        'subagents(action="kill", session_key=<the child key>), never a shell command.'
    ),
    params={
        "action": {
            "type": "string",
            "enum": ["list", "kill", "steer"],
            "description": "list your children; kill = stop and close one; steer = redirect it",
        },
        "session_key": {
            "type": "string",
            "description": "Full child session key, e.g. agent:main:acp:1a2b3c4d "
            "(required for kill and steer)",
        },
        "message": {
            "type": "string",
            "description": "Steering message (required for steer)",
        },
    },
    required=["action"],
    # One tool_search away, and loaded by sessions_spawn(runtime="acp"):
    # before this the tool was hidden altogether, and a parent asked to stop
    # a harness ran `agentos acp close` through exec_command instead.
    defer_loading=True,
)
async def subagents(
    action: str,
    session_key: str | None = None,
    message: str | None = None,
) -> str:
    if action not in _VALID_SUBAGENT_ACTIONS:
        raise ToolError(f"Invalid action: {action}. Must be list|kill|steer")

    if action in ("kill", "steer") and not session_key:
        raise ToolError(f"'session_key' required for {action}")
    if action == "steer" and not message:
        raise ToolError("'message' required for steer")

    # --- list ---
    if action == "list":
        try:
            mgr = _get_session_manager()
            current_key = await _current_session_key(mgr)
            if current_key is None:
                log.warning("subagents.list_no_session_context")
                return json.dumps({"action": "list", "subagents": []})
            subs = await _list_children(mgr, current_key)
            from agentos.tools.builtin.acp_spawn import annotate_acp_rows

            rows = [_compact_child(row) for row in _by_relevance(annotate_acp_rows(subs))]
            return json.dumps(
                {"action": "list", "total": len(rows), "subagents": rows}, ensure_ascii=False
            )
        except (ImportError, AttributeError, NotImplementedError) as exc:
            raise _manager_unavailable() from exc

    # --- kill / steer need session lookup ---
    try:
        mgr = _get_session_manager()
        current_key = await _current_session_key(mgr)
        if current_key is None:
            raise ToolError("session context required")
        session = await mgr.get_session(session_key)
        if session is None:
            raise ToolError(f"Subagent not found: {session_key}")

        _check_spawned_by_or_raise(session, current_key)

        from agentos.tools.builtin.acp_spawn import acp_subagent_action, row_is_acp

        if row_is_acp(session):
            return await acp_subagent_action(action, str(session_key), session, message=message)

        # Terminal check
        status = getattr(session, "status", None)
        if isinstance(session, dict):
            status = session.get("status")
        if status in _TERMINAL_STATUSES:
            raise ToolError(f"Session '{session_key}' is already terminated")

        if action == "kill":
            try:
                runtime = _get_task_runtime()
            except ToolError:
                runtime = None
            if runtime is not None:
                await runtime.cancel(
                    session_key=session_key,
                    source="agent_tool_kill",
                    reason="agent_tool_kill",
                )
            await mgr.kill_session(session_key)
            return json.dumps({"action": "kill", "session_key": session_key, "status": "killed"})
        else:  # steer
            try:
                runtime = _get_task_runtime()
            except ToolError:
                runtime = None
            if runtime is not None:
                handle = await runtime.send(
                    session_key,
                    message,
                    provenance={"kind": "internal_system", "source_tool": "subagents.steer"},
                    authority=authority_from_context(current_tool_context.get()),
                )
                return json.dumps(
                    {
                        "action": "steer",
                        "session_key": session_key,
                        "status": "queued",
                        "task_id": handle.task_id,
                    }
                )
            await mgr.inject_message(session_key, message, provenance="internal_system")
            return json.dumps(
                {"action": "steer", "session_key": session_key, "status": "delivered"}
            )
    except ToolError:
        raise
    except (ImportError, AttributeError, NotImplementedError) as exc:
        raise _manager_unavailable() from exc
