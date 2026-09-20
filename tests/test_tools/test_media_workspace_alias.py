import tempfile
from pathlib import Path

import pytest

from agentos.tools.builtin.media import (
    _resolve_generated_audio_path,
    _resolve_generated_image_path,
)
from agentos.tools.types import ToolContext, ToolError, current_tool_context


def test_resolve_generated_image_path_workspace_alias() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        ctx = ToolContext(workspace_dir=str(root))
        token = current_tool_context.set(ctx)
        try:
            resolved = _resolve_generated_image_path("/workspace/output.png", "png")
            assert resolved == root / "output.png"
        finally:
            current_tool_context.reset(token)


def test_resolve_generated_audio_path_workspace_alias() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        ctx = ToolContext(workspace_dir=str(root))
        token = current_tool_context.set(ctx)
        try:
            resolved = _resolve_generated_audio_path(
                "/workspace/speech.mp3",
                response_format="mp3",
                mime_type="audio/mp3",
                prefix="test",
            )
            assert resolved == root / "speech.mp3"
        finally:
            current_tool_context.reset(token)


def test_resolve_generated_media_path_outside_workspace_fails() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        ctx = ToolContext(workspace_dir=str(root))
        token = current_tool_context.set(ctx)
        try:
            with pytest.raises(ToolError):
                _resolve_generated_image_path("/etc/passwd.png", "png")

            with pytest.raises(ToolError):
                _resolve_generated_audio_path(
                    "/etc/passwd.mp3",
                    response_format="mp3",
                    mime_type="audio/mp3",
                    prefix="test",
                )
        finally:
            current_tool_context.reset(token)
