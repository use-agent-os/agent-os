"""Contract tests for the deterministic task-type detector.

The detector is asymmetric by design: a miss forfeits an optimization, a false
positive sends real work to the cheapest tier. The negative suite is therefore
the load-bearing half of this file — it pins the overloaded verbs that would
otherwise misfire (Vietnamese ``dịch`` inside ``giao dịch``/``dịch vụ``,
English ``translation`` in technical prose, Thai ``แปล`` inside ``แปลก``).
"""

from __future__ import annotations

import pytest

from agentos.agentos_router.task_type import (
    BLOCK_CODE_TARGET,
    INSTRUCTION_HEAD_CHARS,
    TASK_TYPE_TRANSLATE,
    detect_task_type,
)

# One natural-language translate request per supported language.
TRANSLATE_REQUESTS: tuple[tuple[str, str], ...] = (
    ("en", "Translate this sentence into English: The weather is nice today."),
    ("vi", "Dịch câu này sang tiếng Anh: Hôm nay trời đẹp quá."),
    ("zh", "把这句话翻译成英语：今天天气很好。"),
    ("ja", "この文を英語に翻訳してください：今日はいい天気ですね。"),
    ("ko", "이 문장을 영어로 번역해 주세요: 오늘 날씨가 참 좋네요."),
    ("th", "ช่วยแปลประโยคนี้เป็นภาษาอังกฤษ: วันนี้อากาศดีมาก"),
    ("id", "Terjemahkan kalimat ini ke bahasa Inggris: Cuaca hari ini sangat bagus."),
    ("fr", "Traduisez cette phrase en anglais : Il fait beau aujourd'hui."),
    ("es", "Traduce esta frase al inglés: Hoy hace muy buen tiempo."),
    ("de", "Übersetze diesen Satz ins Englische: Das Wetter ist heute schön."),
    ("pt", "Traduza esta frase para o inglês: O tempo está muito bom hoje."),
    ("ru", "Переведите это предложение на английский: Сегодня прекрасная погода."),
    ("ar", "ترجم هذه الجملة إلى الإنجليزية: الطقس جميل اليوم."),
    ("hi", "इस वाक्य का अंग्रेज़ी में अनुवाद करें: आज मौसम बहुत अच्छा है।"),
)


@pytest.mark.parametrize(
    ("lang", "message"), TRANSLATE_REQUESTS, ids=[t[0] for t in TRANSLATE_REQUESTS]
)
def test_translate_request_detected_in_every_supported_language(lang: str, message: str) -> None:
    verdict = detect_task_type(message)
    assert verdict.task_type == TASK_TYPE_TRANSLATE
    assert verdict.matched_language == lang
    assert verdict.evidence
    assert verdict.blocked_by is None


# Messages that contain a translate-adjacent word but are NOT translation work.
# Each would cost a wrong answer if it were capped to the cheapest tier.
NON_TRANSLATE: tuple[tuple[str, str], ...] = (
    ("vi_transaction", "Giao dịch này bị lỗi, kiểm tra lại log của node giúp tôi."),
    ("vi_service", "Dịch vụ thanh toán trả về 502 khi tải cao, tìm nguyên nhân."),
    ("vi_epidemic", "Dịch bệnh ảnh hưởng thế nào tới chuỗi cung ứng năm nay?"),
    ("vi_shift", "Cần dịch chuyển toàn bộ dữ liệu sang cluster mới."),
    ("vi_interpreter", "Thuê một phiên dịch viên cho buổi họp ngày mai."),
    ("en_nat_bug", "Fix the address translation bug in the NAT layer."),
    ("en_i18n_keys", "Our translation keys are out of sync with the locale files."),
    ("en_past_tense", "The translated output was cached before the deploy."),
    ("th_strange", "เรื่องนี้แปลกมาก ช่วยดูให้หน่อย"),
    ("th_convert", "ช่วยแปลงไฟล์นี้เป็น PDF"),
    ("ja_reason", "その訳ではうまくいかない理由を教えてください。"),
)


@pytest.mark.parametrize(("name", "message"), NON_TRANSLATE, ids=[t[0] for t in NON_TRANSLATE])
def test_non_translate_message_is_not_detected(name: str, message: str) -> None:
    verdict = detect_task_type(message)
    assert verdict.task_type is None
    assert verdict.blocked_by is None, "no verb should have matched at all"


def test_empty_and_blank_messages_are_not_detected() -> None:
    assert detect_task_type("").task_type is None
    assert detect_task_type("   \n\t ").task_type is None


class TestEveryTranslationIsATranslation:
    """Operator policy: translation as such never escapes the ceiling.

    A translation that also asks for commentary, or for a poem's form to
    survive, is still a translation. These cases used to be carved out; the
    carve-outs were removed deliberately, so they are pinned here to keep a
    future "helpful" exception from creeping back in unnoticed.
    """

    @pytest.mark.parametrize(
        "message",
        [
            "Translate this idiom into Vietnamese and explain the wordplay.",
            "Dịch đoạn này sang tiếng Anh và phân tích giọng văn.",
            "Translate this poem into English, preserving the rhyme scheme.",
            "Translate the comments to Japanese:\n\n```\n// hello\n```",
            "Translate this contract clause into German for our legal team.",
        ],
    )
    def test_translation_with_extras_is_still_translate(self, message: str) -> None:
        verdict = detect_task_type(message)
        assert verdict.task_type == TASK_TYPE_TRANSLATE
        assert verdict.blocked_by is None


class TestProgrammingLanguageTarget:
    """The one guard: porting code is a different task, not a hard translation."""

    def test_programming_language_target_blocks(self) -> None:
        verdict = detect_task_type("Translate this Python module to Rust, same public API.")
        assert verdict.task_type is None
        assert verdict.blocked_by == BLOCK_CODE_TARGET

    def test_programming_language_target_blocks_in_vietnamese(self) -> None:
        verdict = detect_task_type("Dịch đoạn code này sang Rust giúp tôi.")
        assert verdict.task_type is None
        assert verdict.blocked_by == BLOCK_CODE_TARGET

    @pytest.mark.parametrize(
        "prompt",
        [
            "Translate this Python function to C++.",
            "Translate this algorithm to C++, preserving performance.",
            "Translate this class to C#.",
            "Translate this code to C# with async/await.",
            "Translate this service to .NET.",
            "Translate this module to .NET Core.",
            "Dịch đoạn code này sang C++ giúp tôi.",
            "Dịch code này sang C# nhé.",
        ],
    )
    def test_programming_language_targets_with_symbols_block(self, prompt: str) -> None:
        verdict = detect_task_type(prompt)
        assert verdict.task_type is None
        assert verdict.blocked_by == BLOCK_CODE_TARGET

    def test_language_named_only_in_the_body_does_not_block(self) -> None:
        """A document mentioning Python is not a porting request."""
        body = ("The team migrated the Python service last quarter. " * 40) + ("z" * 1500)
        verdict = detect_task_type(f"Dịch tài liệu sau sang tiếng Việt:\n\n{body}")
        assert verdict.task_type == TASK_TYPE_TRANSLATE


class TestScanWindow:
    """Instructions bracket a pasted body; the middle is not scanned."""

    def test_instruction_at_the_tail_is_detected(self) -> None:
        body = "x" * 4000
        message = f"Quarterly report follows.\n\n{body}\n\nDịch toàn bộ sang tiếng Việt."
        verdict = detect_task_type(message)
        assert verdict.task_type == TASK_TYPE_TRANSLATE
        assert verdict.matched_language == "vi"

    def test_verb_buried_mid_document_is_ignored(self) -> None:
        """A document that merely mentions translation is not a translate turn."""
        filler = "x" * (INSTRUCTION_HEAD_CHARS + 500)
        message = f"Review this report.\n\n{filler}\nplease translate it\n{filler}\nThanks."
        assert detect_task_type(message).task_type is None

    def test_body_content_does_not_veto_the_instruction(self) -> None:
        """A long pasted body must not suppress its own instruction."""
        body = ("The committee met to explain the budget. " * 60) + ("y" * 2000)
        message = f"Translate the following into Korean:\n\n{body}"
        verdict = detect_task_type(message)
        assert verdict.task_type == TASK_TYPE_TRANSLATE
