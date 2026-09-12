"""``create_pdf_report`` must not drop CJK punctuation and fullwidth symbols (issue #1739).

On hosts without a local TTF the base font is Helvetica, which only renders
U+0000-U+00FF. Ideographs were already routed to ``STSong-Light``; the
punctuation written between them was silently deleted.
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader

from agentos.artifacts import ArtifactStore
from agentos.tools.builtin import file_authoring
from agentos.tools.builtin.file_authoring import (
    _is_cjk_symbol,
    _pdf_markup_text,
    _register_pdf_fonts,
    create_pdf_report,
)
from agentos.tools.types import CallerKind, ToolContext, current_tool_context

CJK_FONT = "STSong-Light"


def _markup(text: str) -> str:
    _register_pdf_fonts()
    return _pdf_markup_text(text, base_font="Helvetica", cjk_font=CJK_FONT)


@pytest.mark.parametrize(
    "char",
    ["，", "。", "：", "！", "《", "》", "“", "”", "—", "…", "／", "Ａ"],
)
def test_cjk_symbol_blocks_are_classified(char: str) -> None:
    assert _is_cjk_symbol(char)


@pytest.mark.parametrize("char", ["a", ",", "é", "中", "✅"])
def test_ordinary_and_ideographic_chars_are_not_cjk_symbols(char: str) -> None:
    assert not _is_cjk_symbol(char)


def test_helvetica_base_routes_cjk_punctuation_to_the_cjk_font() -> None:
    markup = _markup("测试报告：重要通知！")
    assert markup == f'<font name="{CJK_FONT}">测试报告：重要通知！</font>'


def test_helvetica_base_keeps_quotes_dashes_and_ellipsis() -> None:
    text = "你好，世界！这是“测试”——破折号……省略号。"
    markup = _markup(text)
    for char in "，！“”—…。":
        assert char in markup
    assert markup == f'<font name="{CJK_FONT}">{text}</font>'


def test_latin_text_stays_on_the_base_font_and_unsupported_symbols_are_still_dropped() -> None:
    markup = _markup("Title: ok ✅ — done")
    assert markup.startswith("Title: ok ")
    assert "✅" not in markup
    assert f'<font name="{CJK_FONT}">—</font>' in markup


def test_without_a_cjk_font_the_punctuation_is_dropped_not_crashed() -> None:
    _register_pdf_fonts()
    assert _pdf_markup_text("a，b", base_font="Helvetica", cjk_font=None) == "ab"


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


@pytest.mark.asyncio
async def test_create_pdf_report_preserves_cjk_punctuation_on_helvetica_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate a host with no local TTF (Docker, CI, minimal Linux): the real
    # registration still runs (so STSong-Light exists) but the report is built
    # on Helvetica, exactly as ``_register_pdf_fonts`` resolves it there.
    _register_pdf_fonts()
    monkeypatch.setattr(
        file_authoring,
        "_register_pdf_fonts",
        lambda: ("Helvetica", "Helvetica-Bold", CJK_FONT),
    )

    ctx = _channel_artifact_context(tmp_path)
    token = current_tool_context.set(ctx)
    try:
        result = await create_pdf_report(
            name="cjk-report.pdf",
            title="测试报告：重要通知！",
            sections=[
                {
                    "heading": "第一节：说明《细节》",
                    "body": "你好，世界！这是“测试”——破折号……省略号。",
                }
            ],
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result)
    store = ArtifactStore(ctx.artifact_media_root or "")
    _, path = store.resolve_for_download(
        str(payload["artifact"]["id"]),
        session_id=str(payload["artifact"]["session_id"]),
    )
    material = Path(path).read_bytes()
    text = "".join(page.extract_text() for page in PdfReader(BytesIO(material)).pages)
    for needle in (
        "测试报告：重要通知！",
        "第一节：说明《细节》",
        "你好，世界！",
        "“测试”",
        "……省略号。",
    ):
        assert needle in text, f"{needle!r} missing from {text!r}"
