"""Tests for MIME type inference precedence in publish_artifact.

publish_artifact allows callers to specify a custom download filename via `name`.
When `mime` is omitted, the MIME type must be inferred from the intended download
filename (`artifact_name`), not the raw source file in the workspace (`target.name`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentos.tools.builtin.artifacts import publish_artifact
from agentos.tools.types import CallerKind, ToolContext, current_tool_context


def _ctx(tmp_path: Path, *, session: str = "session-mime") -> ToolContext:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return ToolContext(
        caller_kind=CallerKind.WEB,
        workspace_dir=str(workspace),
        artifact_media_root=str(tmp_path / "media"),
        artifact_session_id=session,
        session_key=f"agent:main:webchat:{session}",
    )


def _write(ctx: ToolContext, name: str, content: str = "sample payload\n") -> Path:
    path = Path(ctx.workspace_dir) / name
    path.write_text(content, encoding="utf-8")
    return path


async def _publish(ctx: ToolContext, path: str, **kwargs: object) -> dict:
    token = current_tool_context.set(ctx)
    try:
        return json.loads(await publish_artifact(path=path, **kwargs))
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_publish_artifact_infers_pdf_mime_from_custom_name(tmp_path: Path) -> None:
    """Publishing a text file with name='report.pdf' must register as application/pdf."""
    ctx = _ctx(tmp_path)
    _write(ctx, "output.txt")

    result = await _publish(ctx, "output.txt", name="report.pdf")

    assert result["status"] == "published"
    assert result["artifact"]["name"] == "report.pdf"
    assert result["artifact"]["mime"] == "application/pdf"
    assert ctx.published_artifacts[0]["mime"] == "application/pdf"


@pytest.mark.asyncio
async def test_publish_artifact_infers_image_png_mime_from_custom_name(tmp_path: Path) -> None:
    """Publishing a text/raw file with name='chart.png' must register as image/png."""
    ctx = _ctx(tmp_path)
    _write(ctx, "raw_data.txt")

    result = await _publish(ctx, "raw_data.txt", name="chart.png")

    assert result["status"] == "published"
    assert result["artifact"]["name"] == "chart.png"
    assert result["artifact"]["mime"] == "image/png"
    assert ctx.published_artifacts[0]["mime"] == "image/png"


@pytest.mark.asyncio
async def test_publish_artifact_infers_json_mime_from_custom_name(tmp_path: Path) -> None:
    """Publishing a text file with name='metrics.json' must register as application/json."""
    ctx = _ctx(tmp_path)
    _write(ctx, "export.txt")

    result = await _publish(ctx, "export.txt", name="metrics.json")

    assert result["status"] == "published"
    assert result["artifact"]["name"] == "metrics.json"
    assert result["artifact"]["mime"] == "application/json"
    assert ctx.published_artifacts[0]["mime"] == "application/json"


@pytest.mark.asyncio
async def test_publish_artifact_falls_back_to_target_mime_when_name_has_no_extension(
    tmp_path: Path,
) -> None:
    """When custom name has no extension, it inherits target.suffix and target mime."""
    ctx = _ctx(tmp_path)
    _write(ctx, "document.pdf")

    result = await _publish(ctx, "document.pdf", name="final_report")

    assert result["status"] == "published"
    assert result["artifact"]["name"] == "final_report.pdf"
    assert result["artifact"]["mime"] == "application/pdf"


@pytest.mark.asyncio
async def test_publish_artifact_falls_back_to_target_mime_when_custom_extension_unknown(
    tmp_path: Path,
) -> None:
    """When custom name has an unknown extension, fall back to target mime."""
    ctx = _ctx(tmp_path)
    _write(ctx, "data.json")

    result = await _publish(ctx, "data.json", name="custom_export.xyz123")

    assert result["status"] == "published"
    assert result["artifact"]["name"] == "custom_export.xyz123"
    assert result["artifact"]["mime"] == "application/json"


@pytest.mark.asyncio
async def test_publish_artifact_respects_explicit_mime_override(tmp_path: Path) -> None:
    """Guard: an explicit mime parameter overrides both target and custom name guesses."""
    ctx = _ctx(tmp_path)
    _write(ctx, "output.txt")

    result = await _publish(
        ctx,
        "output.txt",
        name="report.pdf",
        mime="application/vnd.custom.report",
    )

    assert result["status"] == "published"
    assert result["artifact"]["name"] == "report.pdf"
    assert result["artifact"]["mime"] == "application/vnd.custom.report"
