"""What ``create_pdf_report`` does with text the base font cannot draw.

On a host without any of the DejaVu/Arial Unicode TTF candidates -- a bare
Docker or CI image -- the base font resolves to Helvetica, which covers
U+0000-U+00FF. Issue #1739 routed CJK text to ``STSong-Light``; every other
script fell to the same silent ``continue`` and was deleted from the report,
so the sentence closed over the gap and read as if it had been written that
way.

``STSong-Light`` carries more than ideographs, so most of what was dropped can
be drawn after all. What it does not carry cannot be rescued by any registered
font, and those characters are marked rather than deleted -- in particular they
are never left in the Helvetica run, where reportlab's WinAnsi encoding turns
them into unrelated Latin letters and the report states something the caller
never wrote.
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

#: Scripts the CJK font draws: they must survive, not merely be marked.
DRAWN = [
    ("Russian Cyrillic", "Привет"),
    ("basic Greek", "Γεια"),
]

#: Scripts no registered font draws. Emoji are a documented limitation: no
#: bundled font carries astral-plane glyphs.
UNDRAWN = [
    ("Hebrew", "שלום"),
    ("Arabic", "مرحبا"),
    ("Emoji", "✅"),
]


def _markup(text: str) -> str:
    _register_pdf_fonts()
    return _pdf_markup_text(text, base_font="Helvetica", cjk_font=CJK_FONT)


@pytest.mark.parametrize(("script", "text"), DRAWN)
def test_a_script_the_cjk_font_covers_is_routed_to_it(script: str, text: str) -> None:
    assert _markup(f"before {text} after") == (
        f'before <font name="{CJK_FONT}">{text}</font> after'
    ), script


@pytest.mark.parametrize(("script", "text"), UNDRAWN)
def test_a_script_no_font_covers_is_marked_not_deleted(script: str, text: str) -> None:
    """One ``?`` per character, so the reader can see how much is missing."""
    assert _markup(f"before {text} after") == f"before {'?' * len(text)} after", script


@pytest.mark.parametrize(("script", "text"), UNDRAWN)
def test_a_script_no_font_covers_never_becomes_other_letters(script: str, text: str) -> None:
    """The failure mode a reader cannot catch.

    Leaving the character in a Helvetica run does not render a box: reportlab
    encodes it through WinAnsi, so ``Привет`` is drawn as ``nnnnnn`` and
    ``Γειά`` as ``Gein``. The placeholder must be the only substitute glyph.
    """
    assert set(_markup(text)) == {"?"}, script


def test_a_letter_outside_the_cjk_font_s_coverage_is_marked_not_routed() -> None:
    """The gap inside a covered block, which routing alone would still lose.

    ``STSong-Light`` has basic Greek but no accented forms, so an accented
    letter handed to it is dropped by the font itself -- silently, which is
    what this routing exists to end. It stays out of the run and is marked.
    """
    assert _markup("Γειά") == f'<font name="{CJK_FONT}">Γει</font>?'
    # Ukrainian ї is Cyrillic but outside the Russian range the font carries.
    assert _markup("Київ") == f'<font name="{CJK_FONT}">Ки</font>?<font name="{CJK_FONT}">в</font>'


def test_a_latin_space_still_closes_the_run() -> None:
    """Passes either way by design: run splitting is the pre-existing shape.

    A space is Latin-1, so it belongs to the base font and each word gets its
    own span, exactly as CJK text already did.
    """
    assert _markup("Привет мир") == (
        f'<font name="{CJK_FONT}">Привет</font> <font name="{CJK_FONT}">мир</font>'
    )


def test_latin_text_is_never_touched() -> None:
    """Passes either way by design: the branch only fires for undrawable chars."""
    assert _markup("Revenue grew 18% year over year — up") == (
        f'Revenue grew 18% year over year <font name="{CJK_FONT}">—</font> up'
    )


def test_an_ascii_question_mark_is_still_itself() -> None:
    """Passes either way by design: the placeholder is not a marker of its own."""
    assert _markup("really?") == "really?"


def test_without_a_cjk_font_a_covered_script_is_marked_not_dropped() -> None:
    """Passes either way once marking lands: routing needs a font to route to."""
    _register_pdf_fonts()
    assert _pdf_markup_text("Привет", base_font="Helvetica", cjk_font=None) == "??????"


def _artifact_context(tmp_path: Path) -> ToolContext:
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
async def test_the_rendered_pdf_carries_the_text_and_shows_the_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end, through the real report builder and back out of the PDF.

    A host with no local TTF is simulated the way the sibling CJK test does it:
    registration still runs, so ``STSong-Light`` exists, but the report is
    built on Helvetica.
    """
    _register_pdf_fonts()
    monkeypatch.setattr(
        "agentos.tools.builtin.file_authoring._register_pdf_fonts",
        lambda: ("Helvetica", "Helvetica-Bold", CJK_FONT),
    )

    ctx = _artifact_context(tmp_path)
    token = current_tool_context.set(ctx)
    try:
        result = await create_pdf_report(
            name="report.pdf",
            title="Quarterly report",
            sections=[{"heading": "Summary", "body": "Reviewed by Привет ✅ on 2026-05-06"}],
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result)
    store = ArtifactStore(ctx.artifact_media_root or "")
    _, path = store.resolve_for_download(
        str(payload["artifact"]["id"]),
        session_id=str(payload["artifact"]["session_id"]),
    )
    text = "".join(
        page.extract_text() for page in PdfReader(BytesIO(Path(path).read_bytes())).pages
    )

    # The name survives; only the glyph nothing can draw is marked.
    assert "Reviewed by Привет ? on 2026-05-06" in text
    # And it must not read as though the name had never been there, nor carry
    # a name the caller never wrote.
    assert "Reviewed by on" not in text
    assert "nnnnnn" not in text
