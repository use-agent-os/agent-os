"""Project tools: create, list, update knowledge, move sessions.

Projects group chat sessions across agents; each project's ``knowledge``
text is injected into the system prompt of every member session. These
tools let the agent manage projects from prompting, but — unlike the
``projects.*`` RPC surface the Web UI and CLI use — they are scoped to the
calling session: knowledge is readable and writable only for the project
the calling session belongs to, and only the calling session itself can be
moved. Knowledge a tool call writes lands in the system prompt of every
member session, so an unscoped surface would hand any prompt-injected
instruction a persistent, cross-project write primitive.
"""

from __future__ import annotations

import json

import structlog

from agentos.tools.registry import tool
from agentos.tools.types import SafeToolError, ToolError, current_tool_context

_log = structlog.get_logger("agentos.tools.projects")

# ---------------------------------------------------------------------------
# Setter-injected session manager (gateway boot calls set_session_manager)
# ---------------------------------------------------------------------------

_session_manager = None


def set_session_manager(mgr: object) -> None:
    """Inject the SessionManager instance (called from gateway boot)."""
    global _session_manager
    _session_manager = mgr


def _get_session_manager():  # noqa: ANN202
    if _session_manager is None:
        raise ToolError("Session manager not available")
    return _session_manager


def _manager_unavailable(exc: Exception) -> ToolError:
    return ToolError(f"Session manager not available: {exc}")


def _project_rejected(exc: ValueError) -> SafeToolError:
    """A ``SessionManager`` project-validation refusal, as the model should see it.

    The manager raises ``ValueError`` with an authored message ("Project name
    cannot be empty", "Project name already exists: …") that the failure
    envelope would otherwise replace with "The tool received an invalid
    argument" (#2889). The text is this codebase's own, never a library's.
    """
    return SafeToolError(str(exc))


def _resolve_agent_id(agent_id: str | None) -> str:
    resolved = (agent_id or "").strip()
    if not resolved:
        ctx = current_tool_context.get()
        resolved = getattr(ctx, "agent_id", "") if ctx else ""
    return resolved or "main"


def _current_session_key() -> str | None:
    ctx = current_tool_context.get()
    return getattr(ctx, "session_key", None) if ctx else None


async def _calling_session_project_id(mgr: object) -> str | None:
    """Project of the calling session, or None (no session / not in a project)."""
    session_key = _current_session_key()
    if not session_key:
        return None
    node = await mgr.get_session(session_key)  # type: ignore[attr-defined]
    return getattr(node, "project_id", None) if node is not None else None


# ---------------------------------------------------------------------------
# projects_create
# ---------------------------------------------------------------------------


@tool(
    name="projects_create",
    description=(
        "Create a project grouping chat sessions. The knowledge text is "
        "injected into the system prompt of every session in the project."
    ),
    params={
        "name": {
            "type": "string",
            "description": "Project name (unique across projects).",
        },
        "knowledge": {
            "type": "string",
            "description": "Optional shared knowledge/instructions text for the project.",
        },
        "agent_id": {
            "type": "string",
            "description": (
                "Default agent for new chats in the project "
                "(defaults to the calling agent); not a membership boundary."
            ),
        },
    },
    required=["name"],
)
async def projects_create(
    name: str = "",
    knowledge: str = "",
    agent_id: str | None = None,
) -> str:
    try:
        mgr = _get_session_manager()
        project = await mgr.create_project(
            agent_id=_resolve_agent_id(agent_id),
            name=name,
            knowledge=knowledge,
        )
        return json.dumps(project, ensure_ascii=False)
    except ToolError:
        raise
    except ValueError as exc:
        raise _project_rejected(exc) from exc
    except (ImportError, AttributeError, NotImplementedError) as exc:
        raise _manager_unavailable(exc) from exc


# ---------------------------------------------------------------------------
# projects_list
# ---------------------------------------------------------------------------


@tool(
    name="projects_list",
    description=(
        "List projects (with per-project session counts). Knowledge text is "
        "included only for the calling session's own project."
    ),
    params={
        "agent_id": {
            "type": "string",
            "description": "Filter by default agent ID (defaults to all agents).",
        },
    },
    required=[],
)
async def projects_list(agent_id: str | None = None) -> str:
    try:
        mgr = _get_session_manager()
        projects = await mgr.list_projects(agent_id=agent_id or None)
        # Knowledge feeds member sessions' system prompts; exposing every
        # project's text would let one injected session read them all.
        own_project = await _calling_session_project_id(mgr)
        for row in projects:
            if row.get("project_id") != own_project:
                row.pop("knowledge", None)
        return json.dumps(projects, ensure_ascii=False)
    except ToolError:
        raise
    except (ImportError, AttributeError, NotImplementedError) as exc:
        raise _manager_unavailable(exc) from exc


# ---------------------------------------------------------------------------
# projects_update
# ---------------------------------------------------------------------------


@tool(
    name="projects_update",
    description=(
        "Rename the calling session's project and/or replace its shared "
        "knowledge text. Member sessions pick up the new knowledge on their "
        "next turn. Only the project this session belongs to can be updated; "
        "other projects are managed from the Web UI or the `agentos projects` "
        "CLI."
    ),
    params={
        "project_id": {
            "type": "string",
            "description": "Project ID to update (must be the calling session's project).",
        },
        "name": {
            "type": "string",
            "description": "New project name.",
        },
        "knowledge": {
            "type": "string",
            "description": "Replacement knowledge text (full replace, not append).",
        },
    },
    required=["project_id"],
)
async def projects_update(
    project_id: str = "",
    name: str | None = None,
    knowledge: str | None = None,
) -> str:
    if not project_id.strip():
        raise ToolError("project_id must not be empty")
    if name is None and knowledge is None:
        raise ToolError("Provide name and/or knowledge to update")
    try:
        mgr = _get_session_manager()
        # Checked before existence so foreign project ids are neither
        # writable nor probeable from a prompt.
        own_project = await _calling_session_project_id(mgr)
        if own_project is None or project_id.strip() != own_project:
            raise ToolError(
                "projects_update can only edit the project the calling session "
                "belongs to; manage other projects from the Web UI or the "
                "`agentos projects` CLI"
            )
        project = await mgr.update_project(project_id.strip(), name=name, knowledge=knowledge)
        return json.dumps(project, ensure_ascii=False)
    except ToolError:
        raise
    except ValueError as exc:
        raise _project_rejected(exc) from exc
    except KeyError as exc:
        raise ToolError(f"Project not found: {project_id}") from exc
    except (ImportError, AttributeError, NotImplementedError) as exc:
        raise _manager_unavailable(exc) from exc


# ---------------------------------------------------------------------------
# projects_move_session
# ---------------------------------------------------------------------------


@tool(
    name="projects_move_session",
    description=(
        "Move the calling session into a project, or detach it by omitting "
        "project_id. Only the calling session can be moved; move other "
        "sessions from the Web UI or the `agentos projects` CLI."
    ),
    params={
        "project_id": {
            "type": "string",
            "description": "Target project ID; omit to detach the session from its project.",
        },
        "session_key": {
            "type": "string",
            "description": "Optional; must match the calling session when provided.",
        },
    },
    required=[],
)
async def projects_move_session(
    project_id: str | None = None,
    session_key: str | None = None,
) -> str:
    calling_key = _current_session_key()
    if not calling_key:
        raise ToolError("projects_move_session requires a calling session")
    requested_key = (session_key or "").strip()
    if requested_key and requested_key != calling_key:
        # Moving a foreign session would pull this project's knowledge into
        # that session's system prompt — an injection hand-off, so refuse.
        raise ToolError(
            "projects_move_session can only move the calling session; move "
            "other sessions from the Web UI or the `agentos projects` CLI"
        )
    resolved_key = calling_key
    try:
        mgr = _get_session_manager()
        node = await mgr.move_session_to_project(
            resolved_key,
            (project_id or "").strip() or None,
        )
        return json.dumps(
            {
                "session_key": node.session_key,
                "project_id": node.project_id,
            }
        )
    except (ToolError, ValueError):
        raise
    except KeyError as exc:
        raise ToolError(str(exc)) from exc
    except (ImportError, AttributeError, NotImplementedError) as exc:
        raise _manager_unavailable(exc) from exc
