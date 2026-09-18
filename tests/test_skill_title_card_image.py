"""title-card-image — headline wrapping across scripts.

``_wrap_text`` has two strategies: whitespace wrapping for scripts that
separate their words with spaces, and character-count chunking for CJK, which
does not. Picking the wrong one is what these tests pin down, in both
directions — a space-delimited script must not be chunked, and an ideographic
one must not be handed to ``str.split``.

Everything here is offline and deterministic: the wrapper is a pure function,
and the one end-to-end case renders a PNG with Pillow's bundled font.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

BUNDLED = Path(__file__).resolve().parents[1] / "src" / "agentos" / "skills" / "bundled"
RENDER_SCRIPT = BUNDLED / "title-card-image" / "scripts" / "render.py"


def _load_render() -> Any:
    spec = importlib.util.spec_from_file_location("title_card_render", RENDER_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


render = _load_render()


class TestSpaceDelimitedScriptsWrapOnWhitespace:
    """The reported bug: every script above U+4E00 was treated as CJK."""

    def test_hangul_wraps_on_its_spaces(self) -> None:
        """Korean is space-delimited; syllables must not be chopped mid-word."""
        assert render._wrap_text("안녕하세요 여러분 환영합니다", 12) == [
            "안녕하세요 여러분",
            "환영합니다",
        ]

    def test_hangul_keeps_short_text_on_one_line(self) -> None:
        assert render._wrap_text("환영합니다 여러분", 20) == ["환영합니다 여러분"]

    def test_emoji_does_not_drag_latin_into_chunking(self) -> None:
        """An emoji sits above U+4E00 but says nothing about the script."""
        assert render._wrap_text("Welcome to the 🎬 Film Festival 🎥", 20) == [
            "Welcome to the 🎬",
            "Film Festival 🎥",
        ]

    def test_an_emoji_alone_is_not_cjk(self) -> None:
        assert render._wrap_text("🎬 🎥 🎞", 3) == ["🎬 🎥", "🎞"]

    @pytest.mark.parametrize(
        ("label", "text", "expected"),
        [
            ("cyrillic", "Добро пожаловать друзья", ["Добро", "пожаловать", "друзья"]),
            ("greek", "Καλώς ήρθατε φίλοι", ["Καλώς", "ήρθατε", "φίλοι"]),
            ("arabic", "أهلا وسهلا بكم", ["أهلا وسهلا", "بكم"]),
            ("devanagari", "नमस्ते दोस्तों", ["नमस्ते", "दोस्तों"]),
            ("thai", "ยินดี ต้อนรับ", ["ยินดี", "ต้อนรับ"]),
        ],
    )
    def test_other_space_delimited_scripts(
        self, label: str, text: str, expected: list[str]
    ) -> None:
        """Below the old threshold, so these pass either way — guards only."""
        assert render._wrap_text(text, 10) == expected


class TestCjkStillChunksByCharacterCount:
    """The direction the issue did not report: do not over-correct.

    Narrowing the test to real ideographs must not push CJK onto the
    whitespace path, where a space-less headline would become one long line.
    """

    def test_simplified_chinese_chunks(self) -> None:
        assert render._wrap_text("这是一个很长的中文标题需要换行处理", 8) == [
            "这是一个很长的中",
            "文标题需要换行处",
            "理",
        ]

    def test_traditional_chinese_chunks(self) -> None:
        assert render._wrap_text("這是一個很長的標題", 4) == ["這是一個", "很長的標", "題"]

    def test_compatibility_ideographs_chunk(self) -> None:
        """U+F900–U+FAFF was already CJK before the change and stays CJK."""
        assert render._wrap_text("豈更車賈滑串句龜龜契", 4) == ["豈更車賈", "滑串句龜", "龜契"]

    def test_mixed_cjk_and_latin_still_breaks_at_the_character_count(self) -> None:
        """SKILL.md promises mixed strings break at the CJK character count."""
        assert render._wrap_text("AgentOS 发布会 2026", 8) == ["AgentOS ", "发布会 2026"]

    def test_a_single_ideograph_makes_the_whole_string_cjk(self) -> None:
        assert render._wrap_text("Coffee 店 meetup here", 6) == [
            "Coffee",
            " 店 mee",
            "tup he",
            "re",
        ]


class TestSpacelessTextAlwaysWraps:
    """A headline with no spaces still has to fit the canvas."""

    def test_hangul_without_spaces_is_still_broken_up(self) -> None:
        """The regression guard for moving Hangul off the CJK path."""
        assert render._wrap_text("안녕하세요여러분환영합니다", 12) == [
            "안녕하세요여러분환영합니",
            "다",
        ]

    def test_pure_hiragana_wraps(self) -> None:
        """Japanese kana sits below U+4E00, so it never wrapped at all."""
        assert render._wrap_text("こんにちはみなさんようこそ", 12) == [
            "こんにちはみなさんようこ",
            "そ",
        ]

    def test_pure_katakana_wraps(self) -> None:
        assert render._wrap_text("コーヒーショップデアイ", 6) == ["コーヒーショ", "ップデアイ"]

    def test_extension_a_ideographs_wrap(self) -> None:
        """U+3400–U+4DBF is CJK the issue enumerated, and also sits below U+4E00."""
        assert render._wrap_text("㐀㐁㐂㐃㐄㐅㐆㐇㐈", 4) == ["㐀㐁㐂㐃", "㐄㐅㐆㐇", "㐈"]


class TestOverlongWordIsBroken:
    """A word wider than the line has no whitespace to break at."""

    def test_a_long_latin_word_is_split(self) -> None:
        assert render._wrap_text("Supercalifragilisticexpialidocious", 12) == [
            "Supercalifra",
            "gilisticexpi",
            "alidocious",
        ]

    def test_a_long_word_flushes_the_line_it_cannot_join(self) -> None:
        """The tail of a broken word stays greedy and takes the next word."""
        assert render._wrap_text("Go Supercalifragilistic now", 8) == [
            "Go",
            "Supercal",
            "ifragili",
            "stic now",
        ]

    def test_a_word_that_divides_evenly_leaves_no_empty_line(self) -> None:
        assert render._wrap_text("abcdefgh", 4) == ["abcd", "efgh"]

    def test_a_word_exactly_the_line_width_is_left_alone(self) -> None:
        assert render._wrap_text("abcd efgh", 4) == ["abcd", "efgh"]


class TestDegenerateMaxChars:
    """``--max-chars-per-line`` is an unvalidated int straight from the CLI."""

    def test_zero_does_not_crash_on_cjk(self) -> None:
        """``range(0, n, 0)`` raised ValueError before."""
        assert render._wrap_text("发布会发布会", 0) == ["发", "布", "会", "发", "布", "会"]

    def test_negative_does_not_drop_the_headline(self) -> None:
        """A negative step returned [], rendering a card with no text at all."""
        assert render._wrap_text("发布会发布会", -3) == ["发", "布", "会", "发", "布", "会"]

    def test_zero_does_not_hang_on_a_long_latin_word(self) -> None:
        assert render._wrap_text("hello", 0) == ["h", "e", "l", "l", "o"]


class TestUntouchedBehaviour:
    """Guards: these pass either way, and are here to prove nothing moved."""

    def test_empty_text(self) -> None:
        assert render._wrap_text("", 12) == [""]

    def test_whitespace_only_text_is_returned_as_is(self) -> None:
        assert render._wrap_text("   ", 12) == ["   "]

    def test_explicit_newlines_are_honoured_before_any_wrapping(self) -> None:
        assert render._wrap_text("line one\nline two", 4) == ["line one", "line two"]

    def test_plain_latin_is_unchanged(self) -> None:
        assert render._wrap_text("Welcome to the Film Festival", 20) == [
            "Welcome to the Film",
            "Festival",
        ]

    def test_ascii_punctuation_is_not_a_script(self) -> None:
        assert render._wrap_text("Q1 — Q2: done!", 8) == ["Q1 — Q2:", "done!"]


class TestIsCjkChar:
    """Boundaries of the classifier, one code point either side of each range."""

    @pytest.mark.parametrize(
        ("label", "code_point"),
        [
            ("ext-A first", 0x3400),
            ("ext-A last", 0x4DBF),
            ("unified first", 0x4E00),
            ("unified last", 0x9FFF),
            ("compatibility first", 0xF900),
            ("compatibility last", 0xFAFF),
            ("ext-B first", 0x20000),
            ("ext-B last", 0x2A6DF),
            ("compatibility supplement first", 0x2F800),
            ("compatibility supplement last", 0x2FA1F),
        ],
    )
    def test_ideographs_are_cjk(self, label: str, code_point: int) -> None:
        assert render._is_cjk_char(chr(code_point))

    @pytest.mark.parametrize(
        ("label", "code_point"),
        [
            ("latin A", 0x0041),
            ("hiragana あ", 0x3042),
            ("katakana コ", 0x30B3),
            ("cyrillic Д", 0x0414),
            ("hangul 안", 0xAC00),
            ("hangul last", 0xD7AF),
            ("emoji 🎬", 0x1F3AC),
            ("just below ext-A", 0x33FF),
            ("just above ext-A", 0x4DC0),
            ("just above unified", 0xA000),
            ("just below compatibility", 0xF8FF),
            ("just above compatibility", 0xFB00),
        ],
    )
    def test_everything_else_is_not_cjk(self, label: str, code_point: int) -> None:
        assert not render._is_cjk_char(chr(code_point))


class TestRenderEndToEnd:
    """The CLI path, proving the wrapper is reached with real arguments."""

    def test_korean_headline_renders(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "card.png"
        monkeypatch.setattr(
            sys,
            "argv",
            ["render.py", "--text", "안녕하세요 여러분 환영합니다", "--output", str(out)],
        )
        assert render.main() == 0
        capsys.readouterr()
        assert out.is_file()
        assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_zero_max_chars_no_longer_crashes_the_cli(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "card.png"
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "render.py",
                "--text",
                "发布会",
                "--output",
                str(out),
                "--max-chars-per-line",
                "0",
            ],
        )
        assert render.main() == 0
        capsys.readouterr()
        assert out.is_file()
