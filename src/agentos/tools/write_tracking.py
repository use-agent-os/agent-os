"""Helpers for recording workspace file writes in the active tool context."""

from __future__ import annotations

from pathlib import Path

from agentos.tools.types import current_tool_context


def record_workspace_file_write(path: Path) -> None:
    ctx = current_tool_context.get()
    if ctx is None or not ctx.workspace_dir:
        return
    workspace = Path(ctx.workspace_dir).expanduser().resolve()
    try:
        relative_path = path.resolve(strict=False).relative_to(workspace)
    except ValueError:
        return
    ctx.workspace_file_writes.append(
        {
            "path": str(path.resolve(strict=False)),
            "relative_path": relative_path.as_posix(),
            "name": path.name,
            "suffix": path.suffix.casefold(),
        }
    )
