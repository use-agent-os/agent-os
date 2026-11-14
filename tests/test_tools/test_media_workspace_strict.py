"""AOS-19: pdf/image/audio honour ``workspace_strict`` like read_file does.

The media tools resolved a model path (absolute or ``../`` relative) and
applied only the sensitive-path denylist, so under ``workspace_strict`` they
read PDFs, images and audio outside the workspace that read_file refuses.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image
from reportlab.pdfgen import canvas

from agentos.tools.builtin import media
from agentos.tools.types import ToolContext, WorkspaceAccessError, current_tool_context

SECRET = "DUMMY-PRIVATE-PDF-CONTENT"


def _write_pdf(path: Path, text: str) -> None:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(240, 160))
    pdf.drawString(20, 80, text)
    pdf.save()
    path.write_bytes(buffer.getvalue())


def _write_png(path: Path) -> None:
    Image.new("RGB", (4, 4), (255, 0, 0)).save(path, format="PNG")


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Path]]:
    proj = tmp_path / "proj"
    outside = tmp_path / "outside"
    proj.mkdir()
    outside.mkdir()
    _write_pdf(outside / "private.pdf", SECRET)
    _write_png(outside / "private.png")
    (outside / "private.mp3").write_bytes(b"ID3fake-audio")
    _write_pdf(proj / "inside.pdf", "INSIDE-OK")

    seen: list[str] = []

    async def fake_vision(b64_data: str, media_type: str, prompt: str) -> str:
        seen.append(b64_data)
        return "described"

    monkeypatch.setattr(media, "_call_vision_provider", fake_vision)
    yield {"proj": proj, "outside": outside, "seen": seen}  # type: ignore[dict-item]


def _strict(proj: Path, strict: bool = True) -> object:
    return current_tool_context.set(ToolContext(workspace_dir=str(proj), workspace_strict=strict))


@pytest.mark.asyncio
@pytest.mark.parametrize("relative", [True, False])
async def test_pdf_outside_workspace_is_refused_under_strict(layout, relative: bool) -> None:
    target = "../outside/private.pdf" if relative else str(layout["outside"] / "private.pdf")
    token = _strict(layout["proj"])
    try:
        with pytest.raises(WorkspaceAccessError, match="outside active read roots"):
            await media.pdf(target)
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("relative", [True, False])
async def test_image_outside_workspace_is_refused_under_strict(layout, relative: bool) -> None:
    target = "../outside/private.png" if relative else str(layout["outside"] / "private.png")
    token = _strict(layout["proj"])
    try:
        with pytest.raises(WorkspaceAccessError, match="outside active read roots"):
            await media.image(target, "describe")
    finally:
        current_tool_context.reset(token)

    assert layout["seen"] == []


@pytest.mark.asyncio
async def test_audio_outside_workspace_is_refused_under_strict(layout) -> None:
    token = _strict(layout["proj"])
    try:
        with pytest.raises(WorkspaceAccessError, match="outside active read roots"):
            await media._resolve_supported_audio_file_for_tool(
                tool_name="transcribe", path="../outside/private.mp3"
            )
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_pdf_inside_workspace_still_reads_under_strict(layout) -> None:
    token = _strict(layout["proj"])
    try:
        result = json.loads(await media.pdf("inside.pdf"))
    finally:
        current_tool_context.reset(token)

    assert "INSIDE-OK" in result["text"]


@pytest.mark.asyncio
async def test_outside_reads_unchanged_without_strict(layout) -> None:
    token = _strict(layout["proj"], strict=False)
    try:
        result = json.loads(await media.pdf("../outside/private.pdf"))
    finally:
        current_tool_context.reset(token)

    assert SECRET in result["text"]
