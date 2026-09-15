"""Tests for PDF page range parsing and text extraction deduplication."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agentos.tools.builtin.media import _parse_page_range, pdf
from agentos.tools.types import CallerKind, SafeToolError, ToolContext, current_tool_context


def test_parse_page_range_deduplicates_overlapping_and_repeated_pages() -> None:
    # Basic single range
    assert _parse_page_range("1-3", total=5) == [0, 1, 2]

    # Repeated individual pages
    assert _parse_page_range("1, 1, 2, 2", total=5) == [0, 1]

    # Overlapping ranges
    assert _parse_page_range("1-3, 2-4", total=5) == [0, 1, 2, 3]

    # Mixed ranges and individual pages preserving order of first appearance
    assert _parse_page_range("1-2, 5, 2-3", total=5) == [0, 1, 4, 2]


def test_parse_page_range_validation_errors() -> None:
    with pytest.raises(SafeToolError, match="Invalid page range"):
        _parse_page_range("0", total=5)

    with pytest.raises(SafeToolError, match="Invalid page range"):
        _parse_page_range("5-2", total=5)

    with pytest.raises(SafeToolError, match="Invalid page range"):
        _parse_page_range("abc", total=5)

    with pytest.raises(SafeToolError, match="exceeds document length"):
        _parse_page_range("1-10", total=5)


@pytest.mark.asyncio
async def test_pdf_tool_extracts_unique_pages_when_range_overlaps(tmp_path) -> None:
    pdf_file = tmp_path / "sample.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 mock")

    mock_doc = MagicMock()
    page1 = MagicMock()
    page1.extract_text.return_value = "Page 1 content"
    page2 = MagicMock()
    page2.extract_text.return_value = "Page 2 content"
    page3 = MagicMock()
    page3.extract_text.return_value = "Page 3 content"
    mock_doc.pages = [page1, page2, page3]
    mock_doc.__enter__.return_value = mock_doc
    mock_doc.__exit__.return_value = False

    ctx = ToolContext(caller_kind=CallerKind.CLI, workspace_dir=str(tmp_path))
    token = current_tool_context.set(ctx)
    try:
        with patch("pdfplumber.open", return_value=mock_doc):
            res_json = await pdf(path=str(pdf_file), pages="1-2, 2-3")
            data = json.loads(res_json)
            # Text should contain each page exactly once
            assert data["text"] == "Page 1 content\n\nPage 2 content\n\nPage 3 content"
            assert data["total_pages"] == 3
    finally:
        current_tool_context.reset(token)
