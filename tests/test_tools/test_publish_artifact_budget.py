"""Tests for publish_artifact and artifact size validation before reading (#1760)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agentos.artifacts import (
    DEFAULT_ARTIFACT_MAX_BYTES,
    ArtifactBudgetError,
    ArtifactStore,
    file_sha256,
)
from agentos.engine.artifact_delivery import auto_publish_omitted_workspace_artifacts
from agentos.tools.builtin.artifacts import publish_artifact
from agentos.tools.types import ToolContext, ToolError, current_tool_context


def test_file_sha256_streaming_matches_hashlib(tmp_path: Path) -> None:
    test_file = tmp_path / "sample.bin"
    # Create ~150 KB data (spanning multiple 64 KB chunks)
    data = b"x" * (150 * 1024)
    test_file.write_bytes(data)

    import hashlib

    expected_sha = hashlib.sha256(data).hexdigest()
    assert file_sha256(test_file) == expected_sha
    assert file_sha256(test_file, chunk_size=32 * 1024) == expected_sha


@pytest.mark.asyncio
async def test_publish_artifact_rejects_oversized_file_without_reading(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    media_root = tmp_path / "media"
    media_root.mkdir()

    # File exceeding the custom max_bytes budget
    large_file = workspace / "large.bin"
    large_file.write_bytes(b"A" * 5000)

    ctx = ToolContext(
        workspace_dir=str(workspace),
        artifact_media_root=str(media_root),
        artifact_session_id="sess-1",
        session_key="agent:main:main",
        artifact_max_bytes=1000,
    )
    token = current_tool_context.set(ctx)
    try:
        with patch.object(Path, "read_bytes") as mock_read_bytes:
            with pytest.raises(ToolError, match="artifact exceeds per-file budget"):
                await publish_artifact("large.bin")
            mock_read_bytes.assert_not_called()
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_publish_artifact_rejects_empty_file_without_reading(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    media_root = tmp_path / "media"
    media_root.mkdir()

    empty_file = workspace / "empty.bin"
    empty_file.write_bytes(b"")

    ctx = ToolContext(
        workspace_dir=str(workspace),
        artifact_media_root=str(media_root),
        artifact_session_id="sess-1",
        session_key="agent:main:main",
        artifact_max_bytes=1000,
    )
    token = current_tool_context.set(ctx)
    try:
        with patch.object(Path, "read_bytes") as mock_read_bytes:
            with pytest.raises(ToolError, match="artifact payload is empty"):
                await publish_artifact("empty.bin")
            mock_read_bytes.assert_not_called()
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_publish_artifact_computes_hash_in_chunks_and_publishes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    media_root = tmp_path / "media"
    media_root.mkdir()

    # 100 KB file within budget
    content = b"valid artifact content" * 4000
    valid_file = workspace / "report.pdf"
    valid_file.write_bytes(content)

    ctx = ToolContext(
        workspace_dir=str(workspace),
        artifact_media_root=str(media_root),
        artifact_session_id="sess-1",
        session_key="agent:main:main",
        artifact_max_bytes=DEFAULT_ARTIFACT_MAX_BYTES,
    )
    token = current_tool_context.set(ctx)
    try:
        result_json = await publish_artifact("report.pdf")
        assert '"status": "published"' in result_json
        assert len(ctx.published_artifacts) == 1
        assert ctx.published_artifacts[0]["size"] == len(content)
    finally:
        current_tool_context.reset(token)


def test_artifact_store_publish_file_validates_size_before_reading(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    store = ArtifactStore(media_root)

    oversized_file = tmp_path / "oversized.bin"
    oversized_file.write_bytes(b"B" * 10000)

    with patch.object(Path, "read_bytes") as mock_read_bytes:
        with pytest.raises(ArtifactBudgetError, match="artifact exceeds per-file budget"):
            store.publish_file(
                oversized_file,
                session_id="sess-1",
                session_key="agent:main:main",
                source="test",
                max_bytes=2000,
            )
        mock_read_bytes.assert_not_called()


def test_auto_publish_omitted_workspace_artifacts_validates_size_before_reading(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    media_root = tmp_path / "media"
    media_root.mkdir()

    oversized_file = workspace / "large.pdf"
    oversized_file.write_bytes(b"C" * 10000)

    ctx = ToolContext(
        workspace_dir=str(workspace),
        artifact_media_root=str(media_root),
        artifact_session_id="sess-1",
        session_key="agent:main:main",
        artifact_max_bytes=1000,
        workspace_file_writes=[{"path": str(oversized_file), "name": "large.pdf"}],
    )

    with patch.object(Path, "read_bytes") as mock_read_bytes:
        result = auto_publish_omitted_workspace_artifacts(
            ctx,
            final_text="I have generated the file large.pdf for you.",
        )
        mock_read_bytes.assert_not_called()
        assert len(result.artifacts) == 0
        assert any("artifact exceeds per-file budget" in err for err in result.failure_summaries)
