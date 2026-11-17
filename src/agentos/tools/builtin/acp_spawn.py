"""``sessions_spawn(runtime="acp")`` and ACP-aware ``sessions_send``.

The tool modules keep only a few-line branch; everything ACP-specific lives
here. A spawn creates a child session row keyed ``agent:<owner>:acp:<8hex>``
whose ``origin`` carries the harness spec, appends the task to the child
transcript and enqueues it on the task runtime with ``run_kind="acp"`` --
the same path a subagent takes, so depth / ``max_children`` gates, the
completion announce and the parent wake behave identically.

Spawn planning follows OpenClaw ``src/agents/spawn-plan.ts`` and
``subagents/spawn/acp-spawn*.ts`` (MIT): resolve the harness, check policy,
resolve and confine ``cwd``, optionally create a managed worktree, and only
resume a harness session the requester (or one of its children) owns.
"""

from __future__ import annotations

import importlib
import json
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import structlog

from agentos import run_kinds
from agentos.acp.registry import get_harness, normalize_agent_id
from agentos.acp.types import AcpAgentNotAllowedError, AcpError
from agentos.gateway.acp_wiring import acp_config_of, acp_state_dir
from agentos.session.keys import build_acp_session_key, is_acp_key, parse_agent_id
from agentos.tools.types import SafeToolError, ToolError, current_tool_context

log = structlog.get_logger("agentos.tools.acp_spawn")

_VALID_MODES = ("persistent", "oneshot")
_SEND_MODES = ("followup", "steer")
_SUPERSEDED_KEEP = 20


# --------------------------------------------------------------------------
# Config helpers
# --------------------------------------------------------------------------


def launch_overrides(acp_config: Any) -> dict[str, list[str]]:
    """``[acp.agents.<id>] command/args`` as ``resolve_launch`` overrides."""
    out: dict[str, list[str]] = {}
    agents = getattr(acp_config, "agents", None) or {}
    for agent_id, override in agents.items():
        command = getattr(override, "command", None)
        if command:
            out[normalize_agent_id(agent_id)] = [
                command,
                *list(getattr(override, "args", []) or []),
            ]
    return out


def _agent_allowed(acp_config: Any, agent_id: str) -> bool:
    allowed = list(getattr(acp_config, "allowed_agents", None) or ["*"])
    return "*" in allowed or agent_id in {normalize_agent_id(a) for a in allowed}


def _fallback_check_spawn_allowed(acp_config: Any, agent_id: str) -> None:
    if not getattr(acp_config, "enabled", True):
        raise AcpAgentNotAllowedError("ACP is disabled ([acp] enabled = false)")
    if get_harness(agent_id) is None and agent_id not in launch_overrides(acp_config):
        raise AcpAgentNotAllowedError(
            f"Unknown ACP harness '{agent_id}' (not in the registry and no [acp.agents.{agent_id}]"
            " command override)"
        )
    if not _agent_allowed(acp_config, agent_id):
        raise AcpAgentNotAllowedError(f"ACP harness '{agent_id}' is not in [acp] allowed_agents")


def check_spawn_allowed(acp_config: Any, agent_id: str, *, requester_ctx: Any) -> None:
    """Delegate to ``agentos.acp.policy`` when present, else the local rules.

    The local rules always run too: an unknown harness id is refused even if
    the policy module only looks at allow-lists.
    """
    _fallback_check_spawn_allowed(acp_config, agent_id)
    try:
        checker = importlib.import_module("agentos.acp.policy").check_spawn_allowed
    except (ImportError, AttributeError):
        return
    checker(acp_config, agent_id, requester_ctx=requester_ctx)


# --------------------------------------------------------------------------
# cwd confinement
# --------------------------------------------------------------------------


def _real(path: Path) -> Path:
    return Path(os.path.realpath(os.fspath(path)))


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def allowed_cwd_roots(
    *, workspace_dir: Path | None, acp_config: Any, state_dir: Path
) -> list[Path]:
    roots: list[Path] = []
    if workspace_dir is not None:
        roots.append(_real(workspace_dir))
    for raw in getattr(acp_config, "allowed_cwds", None) or []:
        if raw and str(raw).strip():
            roots.append(_real(Path(os.path.expanduser(str(raw).strip()))))
    roots.append(_real(state_dir / "worktrees"))
    return roots


def resolve_spawn_cwd(
    cwd: str | None,
    *,
    workspace_dir: Path | None,
    acp_config: Any,
    state_dir: Path,
) -> Path:
    """Resolve ``cwd`` (default: the agent workspace) and confine it to the
    workspace, ``[acp] allowed_cwds`` or the managed worktree root."""
    if cwd and str(cwd).strip():
        raw = Path(os.path.expanduser(str(cwd).strip()))
        if not raw.is_absolute():
            if workspace_dir is None:
                raise ToolError("Relative cwd needs an agent workspace")
            raw = workspace_dir / raw
    else:
        if workspace_dir is None:
            raise ToolError("No agent workspace; pass an absolute cwd")
        raw = workspace_dir
    resolved = _real(raw)
    if not resolved.is_dir():
        raise ToolError(f"cwd is not an existing directory: {resolved}")
    roots = allowed_cwd_roots(
        workspace_dir=workspace_dir, acp_config=acp_config, state_dir=state_dir
    )
    if not any(_within(resolved, root) for root in roots):
        raise ToolError(cwd_refusal_message(resolved, acp_config, workspace_dir=workspace_dir))
    return resolved


def _home_relative(path: Path) -> str:
    """``path`` as ``~/…`` with forward slashes when it is under the home
    directory (so the suggested config value reads the same on every OS),
    otherwise unchanged."""
    raw_home = os.path.expanduser("~")
    if not raw_home or raw_home in ("/", "~"):
        return str(path)
    for home in (Path(raw_home), _real(Path(raw_home))):
        try:
            rel = path.relative_to(home)
        except ValueError:
            continue
        return "~/" + rel.as_posix() if rel.parts else "~"
    return str(path)


def cwd_refusal_message(
    resolved: Path, acp_config: Any, *, workspace_dir: Path | None = None
) -> str:
    """Why ``cwd`` was refused, what to check first, and the exact command
    that allows the directory if the task really needs it.

    A refused cwd is often a wrong guess (a folder named in the task, such as
    ``agents/trading``, usually lives in the agent workspace), so the agent
    is told to check that before asking the user to widen access.
    ``config set`` replaces the whole list, so the suggested value keeps the
    directories already allowed.
    """
    existing = [
        str(raw).strip()
        for raw in getattr(acp_config, "allowed_cwds", None) or []
        if raw and str(raw).strip()
    ]
    wanted = _home_relative(resolved)
    value = json.dumps([*existing, wanted], ensure_ascii=False)
    workspace = f" ({_home_relative(_real(workspace_dir))})" if workspace_dir is not None else ""
    return (
        f"ACP_CWD_NOT_ALLOWED: cwd {resolved} is outside the agent workspace, the managed "
        "worktrees and [acp] allowed_cwds. First check that this is the right directory: a "
        "relative path or a folder named in the task (for example agents/...) usually lives "
        f"in the agent workspace{workspace}, so pass it relative to the workspace (or omit cwd) "
        "and spawn again. Only if the task really targets this directory: tell the user and "
        f"ask them to allow it with `agentos config set acp.allowed_cwds '{value}'` "
        "(restart the gateway if it says a restart is required), then spawn again."
    )


# --------------------------------------------------------------------------
# Session-row helpers
# --------------------------------------------------------------------------


def _origin(row: Any) -> dict[str, Any]:
    value = row.get("origin") if isinstance(row, Mapping) else getattr(row, "origin", None)
    return dict(value) if isinstance(value, Mapping) else {}


def _field(row: Any, name: str) -> Any:
    return row.get(name) if isinstance(row, Mapping) else getattr(row, name, None)


async def _list_children(mgr: Any, parent_key: str) -> list[Any]:
    list_sessions = getattr(mgr, "list_sessions", None)
    if not callable(list_sessions):
        return []
    rows: list[Any] = []
    page = 0
    page_size = 100
    while True:
        try:
            batch = await list_sessions(
                spawned_by=parent_key, limit=page_size, offset=page * page_size
            )
        except TypeError:
            batch = [
                r
                for r in (await list_sessions(limit=200) or [])
                if _field(r, "spawned_by") == parent_key
            ]
            return rows + list(batch)
        if not batch:
            return rows
        rows.extend(batch)
        if len(batch) < page_size:
            return rows
        page += 1


async def authorize_resume(
    mgr: Any, *, requester_key: str, resume_session_id: str
) -> dict[str, Any]:
    """The ACP origin that recorded ``resume_session_id`` for the requester
    or one of its children; raise when nobody it owns ever had it.

    The id is the harness's ACP session id (also accepted: the record id, or
    an id the child itself was resumed from). A parent that passes a child's
    *session key* (or its short hex) is told to ``sessions_send`` to that
    child instead: that resumes the same harness conversation, where a new
    spawn with a guessed id failed with ``resume_forbidden`` and the parent
    then started over from scratch.
    """
    wanted = resume_session_id.strip()
    candidates: list[Any] = []
    try:
        own = await mgr.get_session(requester_key)
    except Exception:  # noqa: BLE001
        own = None
    if own is not None:
        candidates.append(own)
    children = await _list_children(mgr, requester_key)
    candidates.extend(children)
    matches: list[dict[str, Any]] = []
    for row in candidates:
        origin = _origin(row)
        if origin.get("kind") != "acp":
            continue
        recorded = {
            str(origin.get(field) or "")
            for field in ("acp_session_id", "record_id", "resume_session_id")
        }
        if wanted in recorded - {""}:
            matches.append(origin)
    if matches:
        # Prefer a row that knows the harness session id the record resolves to.
        return next((o for o in matches if o.get("acp_session_id")), matches[0])
    for row in children:
        key = str(_field(row, "session_key") or "")
        if row_is_acp(row) and wanted and (wanted == key or key.endswith(":" + wanted)):
            raise ToolError(
                f'resume_forbidden: "{wanted}" is the session key of your harness child '
                f"{key}, not a harness session id. To continue that child, call "
                f'sessions_send(session_key="{key}", message=...): it resumes the same '
                "harness conversation. Do not spawn a new one."
            )
    raise ToolError(
        "resume_forbidden: resume_session_id was not recorded for this session or its "
        "children. To continue one of your harness children (finished or stopped), "
        "call sessions_send(session_key=<the child key>, message=...) - "
        'subagents(action="list") shows the keys. Do not spawn a new one.'
    )


# --------------------------------------------------------------------------
# Spawn
# --------------------------------------------------------------------------


async def _discard_worktree(info: dict[str, Any]) -> None:
    """Remove a worktree created for a spawn that then failed (it is clean)."""
    from agentos.acp.worktree import WorktreeError, WorktreeInfo, remove_worktree

    try:
        await remove_worktree(WorktreeInfo(**info), delete_branch=True)
    except (WorktreeError, TypeError, OSError):
        log.warning("acp.spawn.worktree_cleanup_failed", path=info.get("path"), exc_info=True)


async def spawn_acp_session(
    *,
    task: str,
    acp_agent: str | None = None,
    cwd: str | None = None,
    mode: str | None = None,
    harness_mode: str | None = None,
    resume_session_id: str | None = None,
    worktree: bool = False,
    worktree_name: str | None = None,
    worktree_base_ref: str | None = None,
    model: str | None = None,
) -> str:
    from agentos.acp.manager import agent_display_name, get_acp_manager
    from agentos.gateway.routing import build_subagent_route_envelope
    from agentos.tools.builtin import sessions as sessions_tool

    mgr = sessions_tool._get_session_manager()
    gateway_config = sessions_tool._gateway_config
    acp_cfg = acp_config_of(gateway_config)

    ctx = current_tool_context.get()
    parent_session_key = ctx.session_key if ctx is not None else None
    parent_task_id = ctx.task_id if ctx is not None else None
    current_depth = (ctx.subagent_depth if ctx is not None else 0) or 0
    try:
        current_session = await mgr.get_current_session()
        if current_session is not None:
            parent_session_key = parent_session_key or getattr(current_session, "session_key", None)
            current_depth = max(current_depth, getattr(current_session, "spawn_depth", 0))
    except (AttributeError, NotImplementedError):
        pass
    if not parent_session_key:
        raise ToolError("Cannot spawn an ACP session without a parent session")
    if current_depth >= sessions_tool._MAX_SPAWN_DEPTH:
        raise ToolError(f"Max spawn depth ({sessions_tool._MAX_SPAWN_DEPTH}) exceeded")

    owner_agent_id = (ctx.agent_id if ctx is not None else None) or parse_agent_id(
        parent_session_key
    )

    manager = get_acp_manager()
    if manager is None or not manager.available:
        reason = manager.unavailable_reason if manager is not None else "not configured"
        raise ToolError(f"ACP unavailable: {reason}")

    harness = normalize_agent_id(acp_agent or getattr(acp_cfg, "default_agent", "") or "")
    if not harness:
        raise ToolError("acp_agent is required (no [acp] default_agent configured)")
    try:
        check_spawn_allowed(acp_cfg, harness, requester_ctx=ctx)
    except AcpError as exc:
        raise ToolError(f"{exc.code}: {exc}") from exc

    session_mode = (mode or "oneshot").strip().lower()
    if session_mode not in _VALID_MODES:
        raise ToolError("mode must be 'persistent' or 'oneshot'")

    workspace_dir: Path | None = None
    if ctx is not None and getattr(ctx, "workspace_dir", None):
        workspace_dir = Path(str(ctx.workspace_dir))
    else:
        from agentos.agents.scope import resolve_agent_workspace_dir

        workspace_dir = resolve_agent_workspace_dir(owner_agent_id, gateway_config)
    state_dir = acp_state_dir(gateway_config)
    resolved_cwd = resolve_spawn_cwd(
        cwd, workspace_dir=workspace_dir, acp_config=acp_cfg, state_dir=state_dir
    )

    resume_origin: dict[str, Any] | None = None
    if resume_session_id and resume_session_id.strip():
        resume_origin = await authorize_resume(
            mgr, requester_key=parent_session_key, resume_session_id=resume_session_id
        )
        if normalize_agent_id(str(resume_origin.get("agent_id") or "")) != harness:
            raise ToolError("resume_session_id belongs to a different harness")

    caller_policy = await sessions_tool._resolve_subagent_policy(mgr, owner_agent_id)
    max_children = caller_policy.get("max_children_per_session")

    run_id = uuid.uuid4().hex[:8]
    session_key = build_acp_session_key(owner_agent_id, run_id)

    worktree_info: dict[str, Any] | None = None
    if worktree:
        from agentos.acp.worktree import WorktreeError, create_worktree, worktrees_root

        try:
            info = await create_worktree(
                resolved_cwd,
                name=(worktree_name or f"acp-{run_id}").strip(),
                root=worktrees_root(state_dir),
                base_ref=worktree_base_ref,
            )
        except WorktreeError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc
        worktree_info = info.to_dict()
        resolved_cwd = Path(info.path)
    elif worktree_name or worktree_base_ref:
        raise ToolError("worktree_name / worktree_base_ref need worktree=true")

    origin: dict[str, Any] = {
        "kind": "acp",
        "agent_id": harness,
        "cwd": str(resolved_cwd),
        "mode": session_mode,
        "record_id": None,
        "parent_session_key": parent_session_key,
        "parent_task_id": parent_task_id,
        "task": task,
    }
    if harness_mode:
        origin["harness_mode"] = harness_mode
    if model:
        origin["model"] = model
    if resume_origin is not None:
        # The harness session id itself, even when the caller named the record.
        origin["resume_session_id"] = resume_origin.get("acp_session_id") or resume_session_id
        if resume_origin.get("record_id"):
            origin["record_id"] = resume_origin.get("record_id")
    if worktree_info is not None:
        origin["worktree"] = worktree_info

    runtime = sessions_tool._get_task_runtime()
    spawn_depth = current_depth + 1
    spawn_lock = sessions_tool._get_spawn_lock(parent_session_key)
    try:
        async with spawn_lock:
            if isinstance(max_children, int) and max_children >= 0:
                active = await sessions_tool._count_active_children(mgr, parent_session_key)
                if active >= max_children:
                    raise ToolError(
                        f"Max active children ({max_children}) exceeded for session "
                        f"'{parent_session_key}'"
                    )
            create = getattr(mgr, "create", None) or getattr(mgr, "create_session")
            await create(
                session_key=session_key,
                agent_id=owner_agent_id,
                model=model,
                spawn_depth=spawn_depth,
                parent_session_key=parent_session_key,
                spawned_by=parent_session_key,
                label=f"acp:{harness}",
                origin=origin,
            )
            entry = await mgr.append_message(session_key, role="user", content=task)
    except BaseException:
        if worktree_info is not None:
            await _discard_worktree(worktree_info)
        raise
    # The spawning turn's context decides whether an operator can answer the
    # harness's permission prompts (cron / channel automation cannot).
    manager.bind_permissions(session_key, requester_ctx=ctx)
    envelope = build_subagent_route_envelope(
        session_key=session_key,
        parent_session_key=parent_session_key,
        agent_id=owner_agent_id,
        run_id=run_id,
        parent_task_id=parent_task_id,
        spawn_depth=spawn_depth,
        origin="sessions_spawn_acp",
    )
    message_id = getattr(entry, "message_id", None)
    enqueue_kwargs: dict[str, Any] = {"mode": "followup", "run_kind": run_kinds.ACP}
    if isinstance(message_id, str) and message_id:
        enqueue_kwargs["persisted_user_message_id"] = message_id
    handle = await runtime.enqueue(envelope, task, **enqueue_kwargs)
    log.info(
        "acp.spawn.queued",
        session_key=session_key,
        agent_id=harness,
        mode=session_mode,
        worktree=bool(worktree_info),
        resumed=resume_origin is not None,
    )
    payload: dict[str, Any] = {
        "session_key": session_key,
        "runtime": "acp",
        "acp_agent": harness,
        "acp_agent_name": agent_display_name(harness),
        "cwd": str(resolved_cwd),
        "mode": session_mode,
        "harness_mode": harness_mode or None,
        "task_id": handle.task_id,
        "status": "queued",
        "spawn_depth": spawn_depth,
        "completion_delivery": "pushed_to_parent_session",
        "yield_instruction": (
            "Call sessions_yield with no session_key after spawning; the harness result "
            "is pushed back to this session when the turn finishes."
        ),
    }
    if worktree_info is not None:
        payload["worktree"] = {
            "path": worktree_info["path"],
            "branch": worktree_info["branch"],
            "base_ref": worktree_info["base_ref"],
        }
    payload["followup"] = "sessions_send(session_key, message, mode='followup'|'steer')"
    payload["stop"] = f'subagents(action="kill", session_key="{session_key}")'
    await _load_subagents_tool()
    return json.dumps(payload)


async def _load_subagents_tool() -> None:
    """Put the (deferred) ``subagents`` tool in the session that just spawned
    a harness, so "stop it" maps to ``subagents(action="kill")``: without it a
    parent stopped a harness by running ``agentos acp close`` in a shell."""
    try:
        from agentos.tools.deferred import load_for_current_session

        await load_for_current_session(["subagents"])
    except Exception:  # noqa: BLE001 - the spawn already succeeded
        log.debug("acp.spawn.subagents_load_failed", exc_info=True)


# --------------------------------------------------------------------------
# Send / steer
# --------------------------------------------------------------------------


async def send_to_acp_session(
    session_key: str,
    message: str,
    *,
    mode: str = "followup",
    source_tool: str = "sessions_send",
) -> str:
    """Queue ``message`` as the next turn of an ACP session.

    ``steer`` cancels the running turn first (its completion is not
    announced: the replacement turn's is). A finished session -- ``persistent``
    or ``oneshot`` -- is reopened: a oneshot resumes its harness session in a
    new process and closes it again after the turn. Only a session an
    operator closed (``acp.close``, ``subagents kill``) is refused.
    """
    from agentos.acp.manager import get_acp_manager, origin_is_closed
    from agentos.gateway.routing import build_subagent_route_envelope
    from agentos.tools.builtin import sessions as sessions_tool

    send_mode = (mode or "followup").strip().lower()
    if send_mode not in _SEND_MODES:
        raise SafeToolError("mode must be 'followup' or 'steer'")
    mgr = sessions_tool._get_session_manager()
    session = await mgr.get_session(session_key)
    if session is None:
        raise SafeToolError(f"Session not found: {session_key}")
    origin = _origin(session)
    if origin.get("kind") != "acp":
        raise SafeToolError(f"Session '{session_key}' is not an ACP session")
    status = str(_field(session, "status") or "running")
    # A stopped turn leaves the row "killed" (the task-status mapping) but the
    # session open; only an operator close (origin.closed) ends it.
    if origin_is_closed(origin, session_status=status):
        raise SafeToolError(
            f"ACP session '{session_key}' is closed; spawn a new one (resume_session_id"
            " can continue the harness conversation)"
        )
    manager = get_acp_manager()
    if manager is None or not manager.available:
        reason = manager.unavailable_reason if manager is not None else "not configured"
        raise SafeToolError(f"ACP unavailable: {reason}")
    runtime = sessions_tool._get_task_runtime()

    ctx = current_tool_context.get()
    changes: dict[str, Any] = {}
    parent_key = origin.get("parent_session_key") or _field(session, "spawned_by")
    if ctx is not None and ctx.session_key and ctx.session_key == parent_key and ctx.task_id:
        # Join the sender's current spawn group so the reply wakes it.
        origin["parent_task_id"] = ctx.task_id

    if send_mode == "steer":
        active_ids: list[str] = []
        try:
            rows = await runtime.list(session_key=session_key)
        except Exception:  # noqa: BLE001
            rows = []
        for row in rows or []:
            row_status = str(getattr(getattr(row, "status", None), "value", _field(row, "status")))
            if row_status in ("queued", "running"):
                task_id = _field(row, "task_id")
                if isinstance(task_id, str):
                    active_ids.append(task_id)
        if active_ids or manager.is_turn_active(session_key):
            manager.note_stop(session_key, "agent")
        if active_ids:
            superseded = list(origin.get("superseded_task_ids") or []) + active_ids
            origin["superseded_task_ids"] = superseded[-_SUPERSEDED_KEEP:]
            changes["origin"] = origin
            await mgr.update(session_key, **changes)
            changes = {}
            await runtime.cancel(session_key=session_key, source="acp_steer", reason="acp_steer")
        elif manager.is_turn_active(session_key):
            await manager.cancel(session_key, reason="acp_steer")

    changes["origin"] = origin
    if status in ("done", "failed", "timeout", "killed"):
        changes.update({"status": "running", "ended_at": None})
    await mgr.update(session_key, **changes)

    if parent_key:
        envelope = build_subagent_route_envelope(
            session_key=session_key,
            parent_session_key=str(parent_key),
            agent_id=str(_field(session, "agent_id") or parse_agent_id(session_key)),
            parent_task_id=origin.get("parent_task_id"),
            spawn_depth=int(_field(session, "spawn_depth") or 1),
            origin=source_tool,
        )
        handle = await runtime.enqueue(envelope, message, mode="followup", run_kind=run_kinds.ACP)
    else:
        handle = await runtime.send(
            session_key, message, provenance={"kind": "inter_session", "source_tool": source_tool}
        )
    return json.dumps(
        {
            "status": "queued",
            "session_key": session_key,
            "task_id": handle.task_id,
            "runtime": "acp",
            "mode": send_mode,
        }
    )


#: Longest tool input value / tool result kept per segment in ``sessions_history``.
HISTORY_TOOL_BODY_CHARS = 500


def _clip(text: str, limit: int = HISTORY_TOOL_BODY_CHARS) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}[…{len(text) - limit} chars]"


def _clip_value(value: Any) -> Any:
    if isinstance(value, str):
        return _clip(value)
    if isinstance(value, dict):
        return {k: _clip_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip_value(v) for v in value]
    return value


def compact_acp_history(messages: list[Any]) -> list[Any]:
    """``sessions_history`` rows of an ACP child with tool bodies clipped.

    A harness turn carries dozens of tool calls with results up to 4k chars
    each; a parent reading the child's history got an 83k-char result that
    was cut before the assistant's report and concluded the history was
    unavailable. Tool inputs and results keep their first
    :data:`HISTORY_TOOL_BODY_CHARS` characters plus a ``[…N chars]`` marker;
    message text is untouched.
    """
    out: list[Any] = []
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("tool_calls"), list):
            out.append(message)
            continue
        segments: list[Any] = []
        for segment in message["tool_calls"]:
            if not isinstance(segment, dict):
                segments.append(segment)
                continue
            segment = dict(segment)
            if segment.get("type") == "tool_result" and isinstance(segment.get("result"), str):
                segment["result"] = _clip(segment["result"])
            elif segment.get("type") == "tool_use" and "input" in segment:
                segment["input"] = _clip_value(segment["input"])
            segments.append(segment)
        out.append({**message, "tool_calls": segments})
    return out


def row_is_acp(row: Any) -> bool:
    if _origin(row).get("kind") == "acp":
        return True
    key = _field(row, "session_key")
    return isinstance(key, str) and is_acp_key(key)


def annotate_acp_rows(rows: list[Any]) -> list[Any]:
    """Tag ACP children in a ``subagents(list)`` result with ``runtime: "acp"``.

    ``acp_closed`` is an operator close (no more turns); a finished oneshot is
    ``acp_idle`` and still takes ``sessions_send`` follow-ups.
    """
    from agentos.acp.manager import origin_is_closed

    out: list[Any] = []
    for row in rows:
        if isinstance(row, dict) and row_is_acp(row):
            origin = _origin(row)
            closed = origin_is_closed(origin, session_status=row.get("status"))
            row = {
                **row,
                "runtime": "acp",
                "acp_agent": origin.get("agent_id"),
                "acp_mode": origin.get("mode"),
                "cwd": origin.get("cwd"),
                "acp_closed": closed,
                "acp_idle": bool(origin.get("idle")) and not closed,
            }
        out.append(row)
    return out


async def acp_subagent_action(
    action: str, session_key: str, session: Any, *, message: str | None = None
) -> str:
    """``subagents(kill|steer)`` for an ACP child."""
    from agentos.acp.manager import get_acp_manager
    from agentos.tools.builtin import sessions as sessions_tool

    if action == "steer":
        return await send_to_acp_session(
            session_key, message or "", mode="steer", source_tool="subagents.steer"
        )
    # kill: cancel the queue, close the harness, mark the row killed.
    origin = _origin(session)
    status = str(_field(session, "status") or "running")
    if status == "killed" and origin.get("closed"):
        raise ToolError(f"Session '{session_key}' is already terminated")
    mgr = sessions_tool._get_session_manager()
    try:
        runtime = sessions_tool._get_task_runtime()
    except ToolError:
        runtime = None
    manager = get_acp_manager()
    if manager is not None:
        manager.note_stop(session_key, "agent")
    if runtime is not None:
        await runtime.cancel(
            session_key=session_key, source="agent_tool_kill", reason="agent_tool_kill"
        )
    if manager is not None:
        await manager.close(session_key, reason="agent_tool_kill")
    else:
        origin.update(closed=True, closed_reason="agent_tool_kill", idle=False)
        await mgr.update(session_key, origin=origin)
    if status != "killed":
        await mgr.kill_session(session_key)
    return json.dumps(
        {"action": "kill", "session_key": session_key, "status": "killed", "runtime": "acp"}
    )
