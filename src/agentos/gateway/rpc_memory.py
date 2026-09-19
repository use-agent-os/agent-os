"""RPC handlers for read-only memory inspection."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentos.gateway.rpc import RpcContext, RpcUnavailableError, get_dispatcher
from agentos.gateway.session_services import get_session_storage
from agentos.memory.types import (
    DEFAULT_MEMORY_SEARCH_MIN_SCORE,
    DEFAULT_MEMORY_SEARCH_RESULTS,
    MemorySearchOpts,
    MemorySource,
    SearchIntent,
    normalize_memory_search_min_score,
    normalize_memory_source_filter,
)
from agentos.session.keys import normalize_agent_id
from agentos.tools.builtin.memory_tools import _is_memory_source_path

_d = get_dispatcher()

_MAX_MEMORY_SHOW_CHARS = 8000
_MAX_MEMORY_SHOW_LINES = 500
_MAX_MEMORY_SHOW_FILE_BYTES = 1024 * 1024
_MAX_REPAIR_ENTRY_CHARS = 4000
_MAX_REPAIR_SHOW_ENTRIES = 100
_MAX_REPAIR_LIST_LIMIT = 200
_REPAIR_SCAN_LIMIT = 1000
_HEALTH_SCAN_LIMIT = 1000
# Durable-receipt statuses meaning "this session's memory never landed". The
# health surface counts them as a backlog; with the repair service gone nothing
# retries them, so the count is a signal to a human rather than a work queue.
_REPAIR_PENDING_STATUSES = ("repair_pending", "distill_failed", "flush_failed")
_SAFETY_ERROR_STATUSES = {"checkpoint_failed", "receipt_orphaned"}
_HASH_MISMATCH_MARKERS = ("hash_mismatch", "hash mismatch")
_SEMANTIC_WARNING_AGE_MS = 24 * 60 * 60 * 1000


def _require_memory_manager(ctx: RpcContext, agent_id: str | None) -> tuple[str, Any]:
    managers = getattr(ctx, "memory_managers", None) or {}
    if not managers:
        raise RpcUnavailableError("No memory managers configured")
    resolved_agent = normalize_agent_id(agent_id or "main")
    manager = managers.get(resolved_agent)
    if manager is None:
        raise KeyError(f"Memory manager not found for agent: {resolved_agent}")
    return resolved_agent, manager


def _require_session_manager(ctx: RpcContext) -> Any:
    manager = getattr(ctx, "session_manager", None)
    if manager is None:
        raise RpcUnavailableError("Session manager is not configured")
    return manager


def _agent_session_key_prefix(agent_id: str | None) -> str | None:
    return f"agent:{normalize_agent_id(agent_id)}:" if agent_id else None


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return getattr(row, key, default)


def _is_safety_error_receipt(row: Any) -> bool:
    status = str(_row_value(row, "status", "") or "").lower()
    reason = str(_row_value(row, "reason", "") or "").lower()
    if status in _SAFETY_ERROR_STATUSES:
        return True
    return any(marker in status or marker in reason for marker in _HASH_MISMATCH_MARKERS)


async def _recent_durable_receipts(storage: Any, *, agent_id: str) -> list[Any]:
    agent_prefix = _agent_session_key_prefix(agent_id)
    conn = getattr(storage, "conn", None)
    if conn is not None:
        agent_clause = ""
        params: list[Any] = []
        if agent_prefix is not None:
            agent_clause = "WHERE substr(session_key, 1, ?) = ?"
            params.extend((len(agent_prefix), agent_prefix))
        params.append(_HEALTH_SCAN_LIMIT)
        async with conn.execute(
            f"""
            SELECT * FROM memory_durable_receipts
            {agent_clause}
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?
            """,
            params,
        ) as cur:
            sql_rows = await cur.fetchall()
        return list(sql_rows)

    list_receipts = getattr(storage, "list_memory_durable_receipts", None)
    if not callable(list_receipts):
        return []
    receipt_rows: list[Any] = []
    for status in (*_SAFETY_ERROR_STATUSES, "hash_mismatch"):
        receipt_rows.extend(await list_receipts(status=status, limit=_HEALTH_SCAN_LIMIT))
    if agent_prefix is not None:
        receipt_rows = [
            row
            for row in receipt_rows
            if str(_row_value(row, "session_key", "") or "").startswith(agent_prefix)
        ]
    receipt_rows.sort(
        key=lambda row: (
            int(_row_value(row, "created_at", 0) or 0),
            str(_row_value(row, "receipt_id", "") or ""),
        ),
        reverse=True,
    )
    return list(receipt_rows[:_HEALTH_SCAN_LIMIT])


async def _pending_repair_receipts(storage: Any, *, agent_id: str) -> list[Any]:
    """Durable receipts still awaiting repair, for the health surface.

    Queries the durable-receipt ledger directly. That ledger is written on the
    compaction path, so this stays meaningful without the repair service --
    which is why the health check does not die with it.
    """
    agent_prefix = _agent_session_key_prefix(agent_id)
    conn = getattr(storage, "conn", None)
    if conn is not None:
        placeholders = ", ".join("?" for _ in _REPAIR_PENDING_STATUSES)
        params: list[Any] = [*_REPAIR_PENDING_STATUSES]
        agent_clause = ""
        if agent_prefix is not None:
            agent_clause = "AND substr(session_key, 1, ?) = ?"
            params.extend((len(agent_prefix), agent_prefix))
        params.append(_HEALTH_SCAN_LIMIT)
        async with conn.execute(
            f"""
            SELECT * FROM memory_durable_receipts
            WHERE status IN ({placeholders})
            {agent_clause}
            ORDER BY created_at ASC, rowid ASC
            LIMIT ?
            """,
            params,
        ) as cur:
            return list(await cur.fetchall())

    list_receipts = getattr(storage, "list_memory_durable_receipts", None)
    if not callable(list_receipts):
        return []
    rows: list[Any] = []
    for status in _REPAIR_PENDING_STATUSES:
        rows.extend(await list_receipts(status=status, limit=_HEALTH_SCAN_LIMIT))
    if agent_prefix is not None:
        rows = [
            row
            for row in rows
            if str(_row_value(row, "session_key", "") or "").startswith(agent_prefix)
        ]
    return rows[:_HEALTH_SCAN_LIMIT]


def _semantic_repair_status(backlog_count: int, oldest_pending_ms: int | None) -> str:
    if backlog_count <= 0:
        return "healthy"
    if backlog_count > 10:
        return "warning"
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    if oldest_pending_ms is None or now_ms - oldest_pending_ms > _SEMANTIC_WARNING_AGE_MS:
        return "warning"
    return "degraded"


async def memory_health_from_durable_ledger(
    session_manager: Any,
    *,
    agent_id: str,
) -> dict[str, Any]:
    storage = get_session_storage(session_manager)
    if storage is None:
        return {
            "memorySafety": {"status": "ok"},
            "semanticMemory": {"status": "healthy", "repairBacklogCount": 0},
        }

    recent_rows = await _recent_durable_receipts(storage, agent_id=agent_id)
    safety_status = "error" if any(_is_safety_error_receipt(row) for row in recent_rows) else "ok"
    pending_rows = await _pending_repair_receipts(storage, agent_id=agent_id)
    backlog_count = len(pending_rows)
    oldest_pending_ms = min(
        (
            int(created_at)
            for row in pending_rows
            if (created_at := _row_value(row, "created_at", None)) is not None
        ),
        default=None,
    )
    return {
        "memorySafety": {"status": safety_status},
        "semanticMemory": {
            "status": _semantic_repair_status(backlog_count, oldest_pending_ms),
            "repairBacklogCount": backlog_count,
        },
    }


def _int_param(
    params: dict[str, Any],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = params.get(name, default)
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"params.{name} must be an integer") from exc
    if number < minimum:
        raise ValueError(f"params.{name} must be >= {minimum}")
    if number > maximum:
        raise ValueError(f"params.{name} must be <= {maximum}")
    return number


def _bool_param(params: dict[str, Any], name: str, default: bool = False) -> bool:
    if name not in params:
        return default
    value = params[name]
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
        raise ValueError(f"params.{name} must be a boolean")
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in ("true", "1", "yes", "on"):
            return True
        if cleaned in ("false", "0", "no", "off"):
            return False
    raise ValueError(f"params.{name} must be a boolean")


def _result_to_wire(result: Any) -> dict[str, Any]:
    source = getattr(result, "source", "")
    source_value = getattr(source, "value", source)
    return {
        "chunkId": getattr(result, "chunk_id", ""),
        "path": getattr(result, "path", ""),
        "source": str(source_value),
        "startLine": getattr(result, "start_line", 0),
        "endLine": getattr(result, "end_line", 0),
        "snippet": getattr(result, "snippet", ""),
        "score": getattr(result, "score", 0.0),
        "vectorScore": getattr(result, "vector_score", None),
        "textScore": getattr(result, "text_score", None),
        "chunkHash": getattr(result, "chunk_hash", None),
        "citation": getattr(result, "citation", None),
    }


def _memory_source_rows(
    root: Path,
    source_filter: MemorySource | None = None,
) -> list[dict[str, Any]]:
    resolved_root = root.resolve()
    candidates: list[tuple[Path, str]] = []

    if source_filter is None or source_filter is MemorySource.memory:
        memory_md = resolved_root / "MEMORY.md"
        if memory_md.is_file():
            candidates.append((memory_md, "memory"))
        memory_dir = resolved_root / "memory"
        if memory_dir.is_dir():
            candidates.extend(
                (path, "memory") for path in memory_dir.rglob("*.md") if path.is_file()
            )

    if source_filter is None or source_filter is MemorySource.knowledge_base:
        kb_dir = resolved_root / "knowledge_base"
        if kb_dir.is_dir():
            candidates.extend(
                (path, "knowledge_base") for path in kb_dir.rglob("*") if path.is_file()
            )

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for file_path, src_type in candidates:
        try:
            resolved_file = file_path.resolve()
            rel = resolved_file.relative_to(resolved_root).as_posix()
        except ValueError:
            continue
        if rel in seen:
            continue
        if src_type == "memory" and not _is_memory_source_path(rel):
            continue
        if any(part.startswith(".") for part in Path(rel).parts):
            continue
        stat = resolved_file.stat()
        line_count = 0
        try:
            with resolved_file.open("r", encoding="utf-8", errors="replace") as handle:
                line_count = sum(1 for _ in handle)
        except Exception:
            pass
        seen.add(rel)
        rows.append(
            {
                "path": rel,
                "source": src_type,
                "sizeBytes": stat.st_size,
                "modifiedAt": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                "lineCount": line_count,
            }
        )
    return sorted(rows, key=lambda row: str(row["path"]))


async def _manager_status_wire(manager: Any) -> dict[str, Any]:
    status_fn = getattr(manager, "status", None)
    if not callable(status_fn):
        return {}
    status = await status_fn()
    return {
        "fileCount": status.get("file_count"),
        "chunkCount": status.get("chunk_count"),
        "sourceCounts": status.get("source_counts", {}),
        "vecAvailable": bool(status.get("vec_available", False)),
        "ftsAvailable": bool(status.get("fts_available", False)),
    }


@_d.method("memory.list")
async def _handle_memory_list(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    agent_id, manager = _require_memory_manager(ctx, (params or {}).get("agentId"))
    source_raw = (params or {}).get("source")
    try:
        source_filter = normalize_memory_source_filter(source_raw, allow_all=True)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    root = _memory_root(manager)
    rows = _memory_source_rows(root, source_filter=source_filter)
    return {"agentId": agent_id, "count": len(rows), "files": rows}


@_d.method("memory.search")
async def _handle_memory_search(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    query = str(params.get("query") or "").strip()
    if not query:
        raise ValueError("params.query is required")
    limit = _int_param(params, "limit", DEFAULT_MEMORY_SEARCH_RESULTS, minimum=1, maximum=20)
    try:
        min_score = normalize_memory_search_min_score(
            params.get("minScore", DEFAULT_MEMORY_SEARCH_MIN_SCORE),
            strict=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("params.minScore must be a number") from exc
    try:
        source = normalize_memory_source_filter(params.get("source") or "memory")
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    agent_id, manager = _require_memory_manager(ctx, params.get("agentId"))
    opts = MemorySearchOpts(max_results=limit, min_score=min_score, source=source)
    results = await manager.search(query, opts, intent=SearchIntent.CONTROL)
    rows = [_result_to_wire(result) for result in results]
    return {"agentId": agent_id, "query": query, "count": len(rows), "results": rows}


def _memory_root(manager: Any) -> Path:
    root = getattr(manager, "workspace_dir", None) or getattr(manager, "memory_dir", None)
    if root is None:
        raise RpcUnavailableError("Memory workspace directory is not configured")
    return Path(root)


def _repair_memory_roots(ctx: RpcContext) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for agent_id, manager in (getattr(ctx, "memory_managers", None) or {}).items():
        root = getattr(manager, "workspace_dir", None) or getattr(manager, "memory_dir", None)
        if root is not None:
            roots[normalize_agent_id(str(agent_id))] = Path(root)
    return roots


@_d.method("memory.index")
async def _handle_memory_index(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    params = params or {}
    agent_id, manager = _require_memory_manager(ctx, params.get("agentId"))
    force = _bool_param(params, "force", False)
    if force:
        store = getattr(manager, "store", None)
        rebuild = getattr(store, "rebuild", None)
        if not callable(rebuild):
            raise RpcUnavailableError("Memory store rebuild is not available")
        await rebuild()
    sync = getattr(manager, "sync", None)
    if not callable(sync):
        raise RpcUnavailableError("Memory manager sync is not available")
    await sync(reason="manual", force=force)
    payload: dict[str, Any] = {
        "agentId": agent_id,
        "force": force,
    }
    payload.update(await _manager_status_wire(manager))
    return payload


def _validate_memory_path(path: str) -> None:
    if not path.strip():
        raise ValueError("params.path is required")
    rel = Path(path.strip())
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError("path traversal is not allowed")
    if (
        not _is_memory_source_path(path)
        and rel.parts != ("USER.md",)
        and not (len(rel.parts) >= 2 and rel.parts[0] == "knowledge_base")
    ):
        raise ValueError(
            "params.path must be MEMORY.md, USER.md, memory/**/*.md, or knowledge_base/**/*"
        )


def _raw_fallback_rel_path(path: str) -> str:
    raw = path.strip()
    if not raw:
        raise ValueError("params.path is required")
    rel = Path(raw)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError("path traversal is not allowed")
    if len(rel.parts) == 1:
        rel = Path("memory") / ".raw_fallbacks" / rel
    if len(rel.parts) != 3 or rel.parts[:2] != ("memory", ".raw_fallbacks"):
        raise ValueError("params.path must be memory/.raw_fallbacks/*.md")
    if rel.suffix.lower() != ".md" or rel.name.startswith("."):
        raise ValueError("params.path must be memory/.raw_fallbacks/*.md")
    return rel.as_posix()


def _raw_fallback_reason(path: Path) -> str | None:
    try:
        first_line = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (IndexError, OSError):
        return None
    prefix = "# Raw flush ("
    suffix = ")"
    if first_line.startswith(prefix) and first_line.endswith(suffix):
        return first_line[len(prefix) : -len(suffix)]
    return None


def _raw_fallback_rows(root: Path) -> list[dict[str, Any]]:
    raw_root = root / "memory" / ".raw_fallbacks"
    if not raw_root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for file_path in sorted(path for path in raw_root.glob("*.md") if path.is_file()):
        stat = file_path.stat()
        rows.append(
            {
                "path": (Path("memory") / ".raw_fallbacks" / file_path.name).as_posix(),
                "sizeBytes": stat.st_size,
                "modifiedAt": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                "reason": _raw_fallback_reason(file_path),
            }
        )
    return rows


def _read_memory_content(
    file_path: Path,
    *,
    from_line: int | None,
    lines: int | None,
) -> tuple[str, int, bool]:
    if from_line is None and lines is None:
        content = file_path.read_text(encoding="utf-8", errors="replace")
        return (
            content[:_MAX_MEMORY_SHOW_CHARS],
            len(content.splitlines()),
            len(content) > _MAX_MEMORY_SHOW_CHARS,
        )

    start_line = int(from_line or 1)
    max_lines = int(lines) if lines is not None else None
    parts: list[str] = []
    char_count = 0
    selected_line_count = 0
    truncated = False

    with file_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, start=1):
            if line_no < start_line:
                continue
            if max_lines is not None and selected_line_count >= max_lines:
                break
            if char_count >= _MAX_MEMORY_SHOW_CHARS:
                truncated = True
                break

            text = line.rstrip("\r\n")
            piece = text if selected_line_count == 0 else f"\n{text}"
            remaining = _MAX_MEMORY_SHOW_CHARS - char_count
            if len(piece) > remaining:
                if remaining > 0:
                    parts.append(piece[:remaining])
                    selected_line_count += 1
                truncated = True
                break

            parts.append(piece)
            char_count += len(piece)
            selected_line_count += 1

    return "".join(parts), selected_line_count, truncated


@_d.method("memory.show")
async def _handle_memory_show(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    raw_path = str(params.get("path") or "")
    agent_id, manager = _require_memory_manager(ctx, params.get("agentId"))

    _validate_memory_path(raw_path)

    from_line = params.get("fromLine")
    if from_line is not None:
        from_line = _int_param(params, "fromLine", 1, minimum=1, maximum=1_000_000)
    lines = params.get("lines")
    if lines is not None:
        lines = _int_param(params, "lines", 1, minimum=1, maximum=_MAX_MEMORY_SHOW_LINES)

    root = _memory_root(manager).resolve()
    file_path = (root / raw_path).resolve()
    try:
        file_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("path traversal is not allowed") from exc
    if not file_path.is_file():
        raise KeyError(f"Memory source not found: {raw_path}")

    if (
        from_line is None
        and lines is None
        and file_path.stat().st_size > _MAX_MEMORY_SHOW_FILE_BYTES
    ):
        raise ValueError("memory source is too large; request a line slice")

    content, selected_line_count, truncated = _read_memory_content(
        file_path,
        from_line=from_line,
        lines=lines,
    )

    return {
        "agentId": agent_id,
        "path": raw_path,
        "fromLine": int(from_line or 1),
        "lineCount": selected_line_count,
        "truncated": truncated,
        "content": content,
    }


@_d.method("memory.raw_fallbacks.list")
async def _handle_raw_fallbacks_list(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    agent_id, manager = _require_memory_manager(ctx, (params or {}).get("agentId"))
    rows = _raw_fallback_rows(_memory_root(manager).resolve())
    return {"agentId": agent_id, "count": len(rows), "files": rows}


@_d.method("memory.raw_fallbacks.show")
async def _handle_raw_fallbacks_show(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    raw_path = _raw_fallback_rel_path(str(params.get("path") or ""))
    agent_id, manager = _require_memory_manager(ctx, params.get("agentId"))

    from_line = params.get("fromLine")
    if from_line is not None:
        from_line = _int_param(params, "fromLine", 1, minimum=1, maximum=1_000_000)
    lines = params.get("lines")
    if lines is not None:
        lines = _int_param(params, "lines", 1, minimum=1, maximum=_MAX_MEMORY_SHOW_LINES)

    root = _memory_root(manager).resolve()
    file_path = (root / raw_path).resolve()
    raw_root = (root / "memory" / ".raw_fallbacks").resolve()
    try:
        file_path.relative_to(raw_root)
    except ValueError as exc:
        raise ValueError("path traversal is not allowed") from exc
    if not file_path.is_file():
        raise KeyError(f"Raw fallback not found: {raw_path}")
    if (
        from_line is None
        and lines is None
        and file_path.stat().st_size > _MAX_MEMORY_SHOW_FILE_BYTES
    ):
        raise ValueError("raw fallback is too large; request a line slice")

    content, selected_line_count, truncated = _read_memory_content(
        file_path,
        from_line=from_line,
        lines=lines,
    )
    return {
        "agentId": agent_id,
        "path": raw_path,
        "fromLine": int(from_line or 1),
        "lineCount": selected_line_count,
        "truncated": truncated,
        "content": content,
        "reason": _raw_fallback_reason(file_path),
    }


# ── Curated memory management ──────────────────────────────────────────────────


@_d.method("memory.curated.get")
async def _handle_memory_curated_get(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    params = params or {}
    agent_id, manager = _require_memory_manager(ctx, params.get("agentId"))
    target = str(params.get("target") or "memory").strip().lower()
    if target not in ("memory", "user"):
        raise ValueError("params.target must be 'memory' or 'user'")
    store = manager.curated_store()
    entries = store.entries_for(target)
    limit = store.char_limit(target)
    usage = store.usage_for(target)
    char_count = store.char_count(target)
    return {
        "agentId": agent_id,
        "target": target,
        "entries": entries,
        "usage": usage,
        "charCount": char_count,
        "charLimit": limit,
        "loadFailed": bool(store.load_failed.get(target, False)),
    }


@_d.method("memory.curated.add")
async def _handle_memory_curated_add(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    agent_id, manager = _require_memory_manager(ctx, params.get("agentId"))
    target = str(params.get("target") or "memory").strip().lower()
    if target not in ("memory", "user"):
        raise ValueError("params.target must be 'memory' or 'user'")
    content = str(params.get("content") or "").strip()
    if not content:
        raise ValueError("params.content is required")
    store = manager.curated_store()
    res = store.add(target, content)
    if not res.get("success"):
        raise ValueError(res.get("error") or "Failed to add curated entry")
    return {
        "agentId": agent_id,
        "target": target,
        "entries": store.entries_for(target),
        "usage": store.usage_for(target),
        "message": res.get("message", "Entry added."),
    }


@_d.method("memory.curated.replace")
async def _handle_memory_curated_replace(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    agent_id, manager = _require_memory_manager(ctx, params.get("agentId"))
    target = str(params.get("target") or "memory").strip().lower()
    if target not in ("memory", "user"):
        raise ValueError("params.target must be 'memory' or 'user'")
    old_text = str(params.get("oldText") or "").strip()
    if not old_text:
        raise ValueError("params.oldText is required")
    new_content = str(params.get("newContent") or "").strip()
    if not new_content:
        raise ValueError("params.newContent is required")
    store = manager.curated_store()
    res = store.replace(target, old_text, new_content)
    if not res.get("success"):
        raise ValueError(res.get("error") or "Failed to replace curated entry")
    return {
        "agentId": agent_id,
        "target": target,
        "entries": store.entries_for(target),
        "usage": store.usage_for(target),
        "message": res.get("message", "Entry replaced."),
    }


@_d.method("memory.curated.remove")
async def _handle_memory_curated_remove(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    agent_id, manager = _require_memory_manager(ctx, params.get("agentId"))
    target = str(params.get("target") or "memory").strip().lower()
    if target not in ("memory", "user"):
        raise ValueError("params.target must be 'memory' or 'user'")
    old_text = str(params.get("oldText") or "").strip()
    if not old_text:
        raise ValueError("params.oldText is required")
    store = manager.curated_store()
    res = store.remove(target, old_text)
    if not res.get("success"):
        raise ValueError(res.get("error") or "Failed to remove curated entry")
    return {
        "agentId": agent_id,
        "target": target,
        "entries": store.entries_for(target),
        "usage": store.usage_for(target),
        "message": res.get("message", "Entry removed."),
    }


@_d.method("memory.curated.batch")
async def _handle_memory_curated_batch(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    agent_id, manager = _require_memory_manager(ctx, params.get("agentId"))
    target = str(params.get("target") or "memory").strip().lower()
    if target not in ("memory", "user"):
        raise ValueError("params.target must be 'memory' or 'user'")
    operations = params.get("operations")
    if not isinstance(operations, list):
        raise ValueError("params.operations must be a list")
    store = manager.curated_store()
    res = store.apply_batch(target, operations)
    if not res.get("success"):
        raise ValueError(res.get("error") or "Batch operation failed")
    return {
        "agentId": agent_id,
        "target": target,
        "entries": store.entries_for(target),
        "usage": store.usage_for(target),
        "message": res.get("message", "Batch applied."),
    }


# ── Knowledge Base ingestion and management ───────────────────────────────────


@_d.method("memory.knowledge_base.ingest")
async def _handle_knowledge_base_ingest(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    agent_id, manager = _require_memory_manager(ctx, params.get("agentId"))
    raw_path = str(params.get("path") or "").strip()
    content = params.get("content")
    filename = str(params.get("filename") or params.get("name") or "").strip()
    recursive = _bool_param(params, "recursive", default=True) if "recursive" in params else True

    from agentos.memory.ingest import ingest_directory, ingest_document

    store = manager.store
    workspace = _memory_root(manager).resolve()

    if content is not None:
        if not filename:
            filename = "document.txt"
        clean_name = Path(filename).name
        if not clean_name or clean_name in (".", ".."):
            clean_name = "document.txt"
        rel_path = f"knowledge_base/{clean_name}"
        doc_bytes: bytes
        if isinstance(content, str):
            doc_bytes = content.encode("utf-8")
        elif isinstance(content, bytes):
            doc_bytes = content
        else:
            raise ValueError("content must be string or bytes")
        kb_dir = workspace / "knowledge_base"
        kb_dir.mkdir(parents=True, exist_ok=True)
        (kb_dir / clean_name).write_bytes(doc_bytes)
        res = await ingest_document(store, doc_bytes, rel_path=rel_path, title=clean_name)
        return {"agentId": agent_id, "results": [res.as_dict()]}

    if not raw_path:
        raise ValueError("params.path or params.content is required")

    target_path = Path(raw_path)
    if not target_path.is_absolute():
        target_path = (workspace / target_path).resolve()
    else:
        target_path = target_path.resolve()

    try:
        rel_to_ws = target_path.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("path traversal is not allowed") from exc

    if not target_path.exists():
        raise FileNotFoundError(f"Path does not exist: {raw_path}")

    kb_root = (workspace / "knowledge_base").resolve()
    try:
        target_path.relative_to(kb_root)
        under_kb = True
    except ValueError:
        under_kb = False

    if target_path.is_dir():
        if under_kb:
            rel_prefix = rel_to_ws.as_posix()
            if not rel_prefix or rel_prefix == ".":
                rel_prefix = "knowledge_base"
            ingest_root = target_path
        else:
            # Reject dirs that contain knowledge_base/ (e.g. workspace root).
            # copytree into knowledge_base/<name> would nest kb into itself.
            try:
                kb_root.relative_to(target_path)
            except ValueError:
                pass
            else:
                raise ValueError(
                    "cannot ingest a directory that contains knowledge_base/; "
                    "ingest a subdirectory or individual files instead"
                )
            dirname = target_path.name
            if not dirname or dirname in (".", ".."):
                dirname = "imported"
            dest_dir = (kb_root / dirname).resolve()
            kb_root.mkdir(parents=True, exist_ok=True)
            if dest_dir != target_path:
                shutil.copytree(target_path, dest_dir, dirs_exist_ok=True)
            rel_prefix = f"knowledge_base/{dirname}"
            ingest_root = dest_dir
        results = await ingest_directory(
            store, ingest_root, base_rel_prefix=rel_prefix, recursive=recursive
        )
        return {"agentId": agent_id, "results": [r.as_dict() for r in results]}

    if under_kb:
        rel_path = rel_to_ws.as_posix()
        ingest_path = target_path
    else:
        name = target_path.name
        dest = (kb_root / name).resolve()
        kb_root.mkdir(parents=True, exist_ok=True)
        if dest != target_path:
            shutil.copy2(target_path, dest)
        rel_path = f"knowledge_base/{name}"
        ingest_path = dest
    res = await ingest_document(store, ingest_path, rel_path=rel_path, title=ingest_path.name)
    return {"agentId": agent_id, "results": [res.as_dict()]}


@_d.method("memory.knowledge_base.list")
async def _handle_knowledge_base_list(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    agent_id, manager = _require_memory_manager(ctx, (params or {}).get("agentId"))
    root = _memory_root(manager)
    rows = _memory_source_rows(root, source_filter=MemorySource.knowledge_base)
    return {"agentId": agent_id, "count": len(rows), "documents": rows}


@_d.method("memory.knowledge_base.remove")
async def _handle_knowledge_base_remove(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    raw_path = str(params.get("path") or "").strip()
    if not raw_path:
        raise ValueError("params.path is required")
    agent_id, manager = _require_memory_manager(ctx, params.get("agentId"))
    workspace = _memory_root(manager).resolve()
    kb_root = (workspace / "knowledge_base").resolve()

    rel = Path(raw_path)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError("path traversal is not allowed")
    if len(rel.parts) < 2 or rel.parts[0] != "knowledge_base":
        raise ValueError("params.path must be within knowledge_base/**")

    target_file = (workspace / rel).resolve()
    try:
        target_file.relative_to(kb_root)
    except ValueError as exc:
        raise ValueError("path traversal is not allowed") from exc

    await manager.store.remove_file(raw_path)
    if target_file.is_file():
        try:
            target_file.unlink()
        except OSError:
            pass

    return {"agentId": agent_id, "path": raw_path, "removed": True}
