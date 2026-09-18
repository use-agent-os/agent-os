"""``create_pdf_report`` must not drop newlines or tabs (issue #2708).

reportlab's ``Paragraph`` markup collapses a literal "\\n"/"\\t" to a single
space, the same whitespace-folding HTML does -- so even a fix that only
stops ``_font_supports_char`` from deleting these characters still renders a
squished, single-space-separated block, not the line breaks or visible tab
gap the source text asked for. These tests check the rendered PDF's actual
text, not just ``_pdf_markup_text``'s return value, so a fix that "preserves
the characters" without producing a real ``<br/>`` can't pass by accident.
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader

from agentos.artifacts import ArtifactStore
from agentos.tools.builtin.file_authoring import (
    _pdf_markup_text,
    _register_pdf_fonts,
    create_pdf_report,
)
from agentos.tools.types import CallerKind, ToolContext, current_tool_context

CJK_FONT = "STSong-Light"


def _markup(text: str, *, base_font: str) -> str:
    _register_pdf_fonts()
    return _pdf_markup_text(text, base_font=base_font, cjk_font=CJK_FONT)


@pytest.mark.parametrize("base_font", ["Helvetica", "AgentOSPDFSans"])
def test_markup_turns_newlines_into_real_line_breaks(base_font: str) -> None:
    """A literal "<br/>" is what reportlab actually renders as a new line."""
    markup = _markup("Line 1\nLine 2", base_font=base_font)
    assert markup == "Line 1<br/>Line 2"


@pytest.mark.parametrize("base_font", ["Helvetica", "AgentOSPDFSans"])
def test_markup_normalizes_crlf_and_lone_cr_to_one_line_break(base_font: str) -> None:
    assert _markup("A\r\nB", base_font=base_font) == "A<br/>B"
    assert _markup("A\rB", base_font=base_font) == "A<br/>B"


@pytest.mark.parametrize("base_font", ["Helvetica", "AgentOSPDFSans"])
def test_markup_expands_a_tab_to_a_visible_gap(base_font: str) -> None:
    markup = _markup("A\tB", base_font=base_font)
    assert markup == "A&#160;&#160;&#160;&#160;B"


def _channel_artifact_context(tmp_path: Path) -> ToolContext:
    root = tmp_path / "artifacts"
    root.mkdir()
    return ToolContext(
        workspace_dir=str(tmp_path),
        session_key="agent:main:main",
        artifact_session_id="sess-1",
        artifact_media_root=str(root),
        caller_kind=CallerKind.CHANNEL,
    )


async def _render_and_extract_text(tmp_path: Path, **kwargs: object) -> str:
    ctx = _channel_artifact_context(tmp_path)
    token = current_tool_context.set(ctx)
    try:
        result = await create_pdf_report(**kwargs)
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result)
    store = ArtifactStore(ctx.artifact_media_root or "")
    _, path = store.resolve_for_download(
        str(payload["artifact"]["id"]),
        session_id=str(payload["artifact"]["session_id"]),
    )
    material = Path(path).read_bytes()
    return "".join(page.extract_text() for page in PdfReader(BytesIO(material)).pages)


@pytest.mark.asyncio
async def test_report_title_with_embedded_newline_renders_on_two_lines(
    tmp_path: Path,
) -> None:
    """Issue #2708's own repro, checked against the rendered PDF, not the
    intermediate markup string: a real base_font (DejaVu, registered by the
    real _register_pdf_fonts on this host) is exactly the "TTF host"
    condition the issue reports.
    """
    text = await _render_and_extract_text(tmp_path, name="r.pdf", title="Multi\nLine Title")
    lines = [line for line in text.splitlines() if line.strip()]
    assert "Multi" in lines
    assert "Line Title" in lines
    # The bug's failure mode: both halves silently glued into one line.
    assert "MultiLine Title" not in text
    assert "Multi Line Title" not in text


@pytest.mark.asyncio
async def test_report_body_tab_renders_as_a_visible_gap_not_a_single_space(
    tmp_path: Path,
) -> None:
    text = await _render_and_extract_text(
        tmp_path,
        name="r.pdf",
        title="Report",
        sections=[{"heading": "Section", "body": "Column A\tColumn B"}],
    )
    assert "Column A\tColumn B" in text or "Column A    Column B" in text
    assert "Column AColumn B" not in text
    assert "Column A Column B" not in text
