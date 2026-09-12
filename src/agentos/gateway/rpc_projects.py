"""Project CRUD RPC — cross-agent grouping of chat sessions.

Projects carry free-form ``knowledge`` text injected into the system prompt
of every member session; sessions of any agent may join any project, and the
project's ``agentId`` only names the default agent for "new chat in
project". Sessions join or leave a project via ``sessions.patch``
(``projectId``); the methods here manage the projects themselves.
Control-plane only.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from agentos.gateway.rpc import RpcContext, RpcHandlerError, RpcUnavailableError, get_dispatcher
from agentos.session.keys import normalize_agent_id
from agentos.session.manager import ProjectUpdateConflictError

log = structlog.get_logger(__name__)

_d = get_dispatcher()


def _project_row(project: dict[str, Any]) -> dict[str, Any]:
    """Emit a project dict in both snake_case and camelCase, like sessions.list."""
    return {
        "project_id": project.get("project_id"),
        "projectId": project.get("project_id"),
        "agent_id": project.get("agent_id"),
        "agentId": project.get("agent_id"),
        "name": project.get("name"),
        "knowledge": project.get("knowledge", ""),
        "created_at": project.get("created_at"),
        "createdAt": project.get("created_at"),
        "updated_at": project.get("updated_at"),
        "updatedAt": project.get("updated_at"),
        "session_count": project.get("session_count", 0),
        "sessionCount": project.get("session_count", 0),
    }


def _require_manager(ctx: RpcContext) -> Any:
    manager = ctx.session_manager
    if manager is None or not hasattr(manager, "create_project"):
        raise RpcUnavailableError("projects.* requires a session manager")
    return manager


def _require_project_id(params: dict | None) -> str:
    project_id = (params or {}).get("projectId") or (params or {}).get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("params.projectId is required")
    return project_id.strip()


async def _broadcast_projects_changed(reason: str, project_id: str | None) -> None:
    """Best-effort notify all authenticated WS connections of project CRUD."""
    from agentos.gateway.websocket import get_registry

    payload = {"schema_version": 1, "reason": reason}
    if project_id:
        payload["project_id"] = project_id
        payload["projectId"] = project_id
    try:
        await get_registry().broadcast("projects.changed", payload)
    except Exception:
        log.warning("projects.changed_broadcast_failed", reason=reason)


@_d.method("projects.create")
async def _handle_projects_create(params: dict | None, ctx: RpcContext) -> dict:
    if not isinstance(params, dict):
        params = {}
    manager = _require_manager(ctx)
    agent_id = normalize_agent_id(params.get("agentId") or params.get("agent_id") or "main")
    name = params.get("name")
    knowledge = params.get("knowledge", "")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("params.name is required")
    if not isinstance(knowledge, str):
        raise ValueError("params.knowledge must be a string")

    project = await manager.create_project(agent_id=agent_id, name=name, knowledge=knowledge)
    await _broadcast_projects_changed("created", project.get("project_id"))
    return {"project": _project_row(project), "ts": int(time.time() * 1000)}


@_d.method("projects.list")
async def _handle_projects_list(params: dict | None, ctx: RpcContext) -> dict:
    manager = _require_manager(ctx)
    raw_agent = (params or {}).get("agentId") or (params or {}).get("agent_id")
    agent_id = normalize_agent_id(raw_agent) if raw_agent else None
    projects = await manager.list_projects(agent_id=agent_id)
    rows = [_project_row(p) for p in projects]
    return {"projects": rows, "count": len(rows), "ts": int(time.time() * 1000)}


@_d.method("projects.get")
async def _handle_projects_get(params: dict | None, ctx: RpcContext) -> dict:
    manager = _require_manager(ctx)
    project_id = _require_project_id(params)
    project = await manager.get_project(project_id)
    if project is None:
        raise RpcHandlerError(
            "project.not_found",
            f"Project '{project_id}' does not exist",
            details={"projectId": project_id},
        )
    return {"project": _project_row(project)}


@_d.method("projects.update")
async def _handle_projects_update(params: dict | None, ctx: RpcContext) -> dict:
    if not isinstance(params, dict):
        params = {}
    manager = _require_manager(ctx)
    project_id = _require_project_id(params)
    name = params.get("name")
    knowledge = params.get("knowledge")
    expected = params.get("expectedUpdatedAt", params.get("expected_updated_at"))
    if name is None and knowledge is None:
        raise ValueError("params.name or params.knowledge is required")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise ValueError("params.name must be a non-empty string")
    if knowledge is not None and not isinstance(knowledge, str):
        raise ValueError("params.knowledge must be a string")
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int)):
        raise ValueError("params.expectedUpdatedAt must be an integer timestamp")

    try:
        project = await manager.update_project(
            project_id,
            name=name,
            knowledge=knowledge,
            expected_updated_at=expected,
        )
    except KeyError as exc:
        raise RpcHandlerError(
            "project.not_found",
            f"Project '{project_id}' does not exist",
            details={"projectId": project_id},
        ) from exc
    except ProjectUpdateConflictError as exc:
        raise RpcHandlerError(
            "project.conflict",
            f"Project '{project_id}' changed since it was read; reload and retry",
            details={"projectId": project_id},
        ) from exc
    await _broadcast_projects_changed("updated", project_id)
    return {"project": _project_row(project)}


@_d.method("projects.delete")
async def _handle_projects_delete(params: dict | None, ctx: RpcContext) -> dict:
    manager = _require_manager(ctx)
    project_id = _require_project_id(params)
    try:
        detached = await manager.delete_project(project_id)
    except KeyError as exc:
        raise RpcHandlerError(
            "project.not_found",
            f"Project '{project_id}' does not exist",
            details={"projectId": project_id},
        ) from exc
    await _broadcast_projects_changed("deleted", project_id)
    return {"deleted": True, "sessions_cleared": detached, "sessionsCleared": detached}
