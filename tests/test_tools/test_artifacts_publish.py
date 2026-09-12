from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentos.tools.builtin.artifacts import publish_artifact
from agentos.tools.types import CallerKind, ToolContext, current_tool_context


@pytest.mark.asyncio
async def test_publish_artifact_distinguishes_files_with_identical_content(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()

    file1 = workspace / "first.txt"
    file2 = workspace / "second.txt"
    content = "shared identical content"
    file1.write_text(content, encoding="utf-8")
    file2.write_text(content, encoding="utf-8")

    ctx = ToolContext(
        workspace_dir=str(workspace),
        artifact_media_root=str(artifacts_root),
        artifact_session_id="sess_1",
        session_key="key_1",
        caller_kind=CallerKind.CLI,
    )
    token = current_tool_context.set(ctx)
    try:
        r1 = json.loads(await publish_artifact("first.txt"))
        assert r1["status"] == "published"
        assert r1["artifact"]["name"] == "first.txt"

        r2 = json.loads(await publish_artifact("second.txt"))
        assert r2["status"] == "published"
        assert r2["artifact"]["name"] == "second.txt"

        # Publishing first.txt again should be recognized as already published
        r1_again = json.loads(await publish_artifact("first.txt"))
        assert r1_again["status"] == "already_published"
        assert r1_again["artifact"]["name"] == "first.txt"
    finally:
        current_tool_context.reset(token)
