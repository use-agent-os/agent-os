"""``create_pdf_report`` must not drop Japanese kana.

On hosts without a CJK-capable TTF the base font is Helvetica, which only
renders U+0000-U+00FF. Ideographs go to ``STSong-Light`` and, since #1739, so
does the punctuation between them -- but ``_is_cjk`` names only the ideograph
blocks, so every hiragana and katakana was deleted. A Japanese report kept its
kanji and its ``、。`` and lost the syllables that join them.
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
    _is_cjk,
    _is_kana,
    _pdf_markup_text,
    _register_pdf_fonts,
    create_pdf_report,
)
from agentos.tools.types import CallerKind, ToolContext, current_tool_context

CJK_FONT = "STSong-Light"


def _markup(text: str) -> str:
    _register_pdf_fonts()
    return _pdf_markup_text(text, base_font="Helvetica", cjk_font=CJK_FONT)


@pytest.mark.parametrize("char", ["あ", "ん", "ゔ", "ア", "ヲ", "ー", "゛", "ゝ", "ヽ"])
def test_kana_blocks_are_classified(char: str) -> None:
    assert _is_kana(char)


@pytest.mark.parametrize("char", ["a", "中", "，", "é", "보", "ㇰ", "゠"])
def test_non_kana_chars_are_not_classified_as_kana(char: str) -> None:
    """Hangul and the Ainu extensions stay out: Adobe-GB1 cannot map them.

    Routing ``보`` to ``STSong-Light`` renders it as ``釁`` -- a wrong glyph is
    worse than the drop it would replace, so the classifier stops at what the
    font carries.
    """
    assert not _is_kana(char)


def test_helvetica_base_routes_hiragana_to_the_cjk_font() -> None:
    assert _markup("こんにちは") == f'<font name="{CJK_FONT}">こんにちは</font>'


def test_helvetica_base_routes_katakana_to_the_cjk_font() -> None:
    assert _markup("レポート") == f'<font name="{CJK_FONT}">レポート</font>'


def test_a_japanese_sentence_survives_intact() -> None:
    text = "報告書をご確認ください。"
    assert _markup(text) == f'<font name="{CJK_FONT}">{text}</font>'


def test_kana_and_ideographs_share_one_font_run() -> None:
    """One run, not one per character — the runs are what reaches reportlab."""
    assert _markup("東京タワー") == f'<font name="{CJK_FONT}">東京タワー</font>'


def test_kana_is_not_reclassified_as_an_ideograph() -> None:
    """Guard: ``_is_cjk`` keeps its own meaning; kana route via the fallback."""
    assert not _is_cjk("あ")
    assert not _is_cjk("ア")
    assert _is_cjk("報")


def test_latin_text_is_unaffected() -> None:
    """Guard: the existing base-font path does not change."""
    assert _markup("Quarterly Report 2026") == "Quarterly Report 2026"


def test_without_a_cjk_font_kana_is_dropped_not_crashed() -> None:
    _register_pdf_fonts()
    assert _pdf_markup_text("aあb", base_font="Helvetica", cjk_font=None) == "ab"


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
async def test_create_pdf_report_preserves_kana_on_helvetica_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            name="kana-report.pdf",
            title="四半期レポート",
            sections=[
                {
                    "heading": "概要：セキュリティ対応",
                    "body": "報告書をご確認ください。対応はすべて完了しています。",
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
        "四半期レポート",
        "概要：セキュリティ対応",
        "報告書をご確認ください。",
        "対応はすべて完了しています。",
    ):
        assert needle in text, f"{needle!r} missing from {text!r}"
