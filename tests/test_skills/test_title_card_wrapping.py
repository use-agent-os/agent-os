"""Issue #2265: title-card wrapping treated every script above U+4E00 as CJK.

``_wrap_text`` asked ``all(ord(c) < 0x4E00 for c in text)`` to decide whether a
string was whitespace-delimited. That threshold is not a script boundary — it
is just where CJK Unified Ideographs start — so any string containing a single
character above it was character-chunked at a fixed width:

* **Korean Hangul** (U+AC00–U+D7AF) is space-delimited like Latin, and
  ``"안녕하세요 여러분 환영합니다"`` came out broken mid-word.
* **Emoji** (U+1F000+) dragged an otherwise-Latin headline onto the CJK path,
  so ``"Welcome to the 🎬 Film Festival 🎥"`` was chopped at ``"Welcome to t"``.

The inverse is also true and the issue does not mention it: **Japanese kana**
(U+3040–U+30FF) sits *below* the threshold, so a kana headline took the
whitespace path, ``split()`` produced one enormous word, and nothing wrapped it
at all — it simply overflowed the card.

Two more shapes had no wrapping either: a word wider than the line, and any
line inside a multi-line input, since explicit newlines were returned
untouched.

Slicing is now glyph-aware as well. Cutting on a raw code-point count splits a
ZWJ emoji sequence into separate people and strands a combining accent on its
own line.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT / "src" / "agentos" / "skills" / "bundled" / "title-card-image" / "scripts" / "render.py"
)


def _render():
    spec = importlib.util.spec_from_file_location("title_card_render", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["title_card_render"] = module
    spec.loader.exec_module(module)
    return module


render = _render()
wrap = render._wrap_text

KOREAN = "안녕하세요 여러분 환영합니다"
KANA = "ひらがなだけのテキストです"
KANJI = "四半期業績報告書の概要です"
EMOJI_LATIN = "Welcome to the 🎬 Film Festival 🎥"


# ── space-delimited scripts wrap on their spaces ────────────────────────────


def test_korean_wraps_on_its_spaces() -> None:
    """The reported bug. Modern Korean separates words with spaces, so every
    line must start and end on one."""
    lines = wrap(KOREAN, 12)

    assert len(lines) > 1, "12 chars cannot hold this headline"
    for line in lines:
        assert line == line.strip()
        assert line in KOREAN, f"{line!r} was cut out of the middle of a word"


def test_korean_words_are_never_split() -> None:
    rejoined = " ".join(wrap(KOREAN, 12))

    assert rejoined == KOREAN


def test_emoji_does_not_drag_latin_onto_the_cjk_path() -> None:
    """The issue's second reproduction: one emoji used to chop the whole
    headline at a character count."""
    lines = wrap(EMOJI_LATIN, 12)

    assert " ".join(lines) == EMOJI_LATIN
    assert not any(line.startswith("he ") for line in lines)


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("cyrillic", "Добро пожаловать на фестиваль"),
        ("greek", "Καλώς ήρθατε στο φεστιβάλ"),
        ("arabic", "مرحبا بكم في المهرجان"),
        ("hebrew", "ברוכים הבאים לפסטיבל"),
        ("devanagari", "फिल्म महोत्सव में आपका स्वागत है"),
        ("thai-with-spaces", "ยินดี ต้อนรับ สู่ เทศกาล"),
        ("korean", KOREAN),
        ("emoji-only", "🎬 🎥 🍿 🎞 📽 🎦"),
    ],
)
def test_every_spaced_script_keeps_its_words(name: str, text: str) -> None:
    """None of these is written without spaces, so none should be chunked."""
    assert " ".join(wrap(text, 12)) == text


# ── scripts written without spaces are chunked ──────────────────────────────


@pytest.mark.parametrize(
    ("name", "text"),
    [("kana", KANA), ("kanji", KANJI), ("mixed", "四半期ひらがな報告")],
)
def test_a_spaceless_script_is_chunked_to_fit(name: str, text: str) -> None:
    """Kana is the case the threshold got backwards: below U+4E00, so it took
    the whitespace path, had no spaces to break on, and never wrapped."""
    lines = wrap(text, 8)

    assert "".join(lines) == text
    assert all(len(line) <= 8 for line in lines)


def test_kana_was_not_wrapped_at_all_before() -> None:
    """Pinned explicitly: a kana headline longer than the line must produce
    more than one line."""
    assert len(wrap(KANA, 8)) > 1


def test_fullwidth_punctuation_counts_as_spaceless() -> None:
    lines = wrap("これは、テストです。ながいぶんしょう", 6)

    assert all(len(line) <= 6 for line in lines)


# ── a word wider than the line ──────────────────────────────────────────────


def test_a_long_word_is_split_rather_than_left_to_overflow() -> None:
    lines = wrap("Supercalifragilisticexpialidocious", 12)

    assert all(len(line) <= 12 for line in lines)
    assert "".join(lines) == "Supercalifragilisticexpialidocious"


def test_a_long_word_among_short_ones_does_not_lose_its_neighbours() -> None:
    text = "a Supercalifragilisticexpialidocious b"
    lines = wrap(text, 12)

    assert all(len(line) <= 12 for line in lines)
    assert "".join(lines).replace(" ", "") == text.replace(" ", "")


# ── explicit newlines ───────────────────────────────────────────────────────


def test_explicit_newlines_are_honoured() -> None:
    lines = wrap("first\nsecond", 40)

    assert lines == ["first", "second"]


def test_a_long_line_inside_a_multiline_input_is_still_wrapped() -> None:
    """Newlines used to be returned untouched, so one long line overflowed the
    card even though the wrapper had been asked to wrap it."""
    lines = wrap("short\na very long second line that should wrap somewhere", 12)

    assert lines[0] == "short"
    assert all(len(line) <= 12 for line in lines)
    assert len(lines) > 2


def test_an_empty_line_inside_a_multiline_input_survives() -> None:
    assert wrap("a\n\nb", 12) == ["a", "", "b"]


# ── slicing does not cut a glyph in half ────────────────────────────────────


def test_a_zwj_emoji_sequence_is_not_split() -> None:
    """``👨‍👩‍👧‍👦`` is one glyph made of four emoji joined by ZWJ. Cutting
    between them renders as separate people."""
    family = "\U0001f468‍\U0001f469‍\U0001f467‍\U0001f466"
    lines = wrap(f"{family} Family Night", 12)

    assert lines[0] == family
    for line in lines:
        assert not line.startswith("‍")
        assert not line.endswith("‍")


def test_a_combining_accent_is_not_stranded() -> None:
    """``e`` + U+0301 is one rendered character; a line must never begin with
    the accent alone."""
    text = "é" * 20

    for line in wrap(text, 7):
        assert not line.startswith("́")


def test_a_flag_is_not_split_between_its_regional_indicators() -> None:
    """A flag is two regional indicators; one on its own renders as a letter."""
    flags = "\U0001f1fa\U0001f1f8\U0001f1ec\U0001f1e7\U0001f1eb\U0001f1f7"
    lines = wrap(flags, 3)

    assert "".join(lines) == flags
    for line in lines:
        assert len(line) % 2 == 0, f"{line!r} splits a flag"


def test_a_skin_tone_modifier_stays_with_its_emoji() -> None:
    text = "\U0001f44d\U0001f3fd" * 8

    for line in wrap(text, 5):
        assert not line.startswith("\U0001f3fd")


# ── shapes that must not regress ────────────────────────────────────────────


def test_plain_ascii_wraps_as_before() -> None:
    assert wrap("the quick brown fox jumps", 12) == ["the quick", "brown fox", "jumps"]


def test_text_shorter_than_the_line_is_one_line() -> None:
    assert wrap("short", 40) == ["short"]


def test_empty_text_returns_one_empty_line() -> None:
    assert wrap("", 12) == [""]


@pytest.mark.parametrize("max_chars", [0, -1, -100])
def test_a_nonsense_width_does_not_hang_or_crash(max_chars: int) -> None:
    """``--font-size`` drives ``max_chars``, so a caller can land on zero. A
    zero-width line would slice forever."""
    lines = wrap("hello world", max_chars)

    assert lines
    assert all(lines)


@pytest.mark.parametrize("text", [KOREAN, KANA, EMOJI_LATIN, "plain ascii text here"])
@pytest.mark.parametrize("max_chars", [1, 2, 3, 5, 8, 40])
def test_no_content_is_ever_lost(text: str, max_chars: int) -> None:
    """Across every width, the characters that went in come back out."""
    joined = "".join(wrap(text, max_chars)).replace(" ", "")

    assert joined == text.replace(" ", "")
