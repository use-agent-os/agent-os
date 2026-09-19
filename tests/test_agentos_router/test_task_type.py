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

    def test_language_named_only_in_the_body_does_not_block(self) -> None:
        """A document mentioning Python is not a porting request."""
        body = ("The team migrated the Python service last quarter. " * 40) + ("z" * 1500)
        verdict = detect_task_type(f"Dịch tài liệu sau sang tiếng Việt:\n\n{body}")
        assert verdict.task_type == TASK_TYPE_TRANSLATE

    @pytest.mark.parametrize(
        "prompt",
        [
            # Sentence-final / punctuation-adjacent: the originally reported bug.
            "Translate this function to C++.",
            "Translate this code to C#.",
            "Translate this service to .NET.",
            "Translate this module to .NET Core.",
            "Dịch đoạn code này sang C++ giúp tôi.",
            "Dịch code này sang C# nhé.",
            # Version/edition suffix directly attached: a following word
            # character must not defeat the match either.
            "Translate this to C++17.",
            "Translate this to C++11, keep behaviour identical.",
            "Translate this to C#7.",
            "Translate this to C#9 with pattern matching.",
            # Framework name directly attached to ".NET" with no space: a
            # preceding word character must not defeat the match.
            "We use ASP.NET for the backend, please translate this handler.",
            "Please translate this class to VB.NET.",
            # Product name directly attached to "C++" with no space: a
            # *following* letter (not just a digit) must not defeat the
            # match either. This guards against narrowing the fix to a
            # digit-only lookahead exception, which would still miss this.
            "Please translate this to C++Builder syntax.",
        ],
    )
    def test_programming_language_targets_with_symbols_block(self, prompt: str) -> None:
        """``c++``/``c#``/``.net`` block regardless of what is on their non-word edge.

        ``+``, ``#``, and the leading ``.`` are never word characters, so a
        plain ``\\b`` on *that* edge alone is enough — it must not also
        require the punctuation/version-suffix/attached-name edge to look a
        particular way.
        """
        verdict = detect_task_type(prompt)
        assert verdict.task_type is None
        assert verdict.blocked_by == BLOCK_CODE_TARGET

    @pytest.mark.parametrize(
        "prompt",
        [
            # `golang` was listed but the name everyone actually writes was
            # not, so a Go port read as a translation and was capped to the
            # cheapest tier.
            "Translate this module to Go.",
            "Translate the function below into Go, keep the signatures.",
            # The C family: `c++` and `c#` were special-cased, the parent
            # language and Objective-C were not.
            "Translate this script to C.",
            "Translate this class to Objective-C.",
            # `f#` sits directly beside the already-handled `c#`.
            "Translate this to F#.",
            "Translate this module to F# with records.",
            # The MS stack: `.net` was handled, its languages were not.
            "Translate this snippet to Visual Basic.",
            "Translate this macro to VBA.",
            # JS frameworks were covered; the runtime they all run on was not.
            "Translate the attached handler to Node.js.",
            "Translate this worker to nodejs.",
        ],
    )
    def test_missing_language_targets_block(self, prompt: str) -> None:
        """Every name in the guard's own families must block a port request.

        A miss here is the harmful direction: the verb still matches, so the
        turn is capped to ``translate_ceiling_tier`` (c0 by default) and a
        code-porting request is handed to the cheapest model.
        """
        verdict = detect_task_type(prompt)
        assert verdict.task_type is None
        assert verdict.blocked_by == BLOCK_CODE_TARGET

    @pytest.mark.parametrize(
        "prompt",
        [
            # "go" and "c" are ordinary English too. They are recognised only
            # in target position, so these must stay real translations.
            "Translate this paragraph to French and go ahead with the rest.",
            "Let's go through the attached page and translate it to Spanish.",
            "Translate this to German, then go over the glossary с нами.",
        ],
    )
    def test_ordinary_english_go_is_not_a_language_target(self, prompt: str) -> None:
        """The short-name rule must not suppress genuine translation work."""
        verdict = detect_task_type(prompt)
        assert verdict.task_type == TASK_TYPE_TRANSLATE
        assert verdict.blocked_by is None

    @pytest.mark.parametrize(
        "prompt",
        [
            # "c++"/"c#" embedded inside a longer identifier must still NOT
            # match — dropping the boundary assertion on *both* edges would
            # wrongly block these on the strength of an unrelated substring.
            "Translate this doc, mention the abc++def helper by name.",
            "Translate this doc, reference ticket src#123 in the notes.",
            "Translate this ticket management#42 into French.",
        ],
    )
    def test_embedded_symbol_substrings_do_not_block(self, prompt: str) -> None:
        """A ``c++``/``c#`` substring embedded in a longer identifier is not a target.

        The leading ``\\b`` on ``c++``/``c#`` must still require a real word
        boundary before the ``c`` — otherwise any identifier that happens to
        contain ``c++`` or ``c#`` (e.g. a variable or ticket name) would be
        misclassified as a code-porting request.
        """
        verdict = detect_task_type(prompt)
        assert verdict.task_type == TASK_TYPE_TRANSLATE
        assert verdict.blocked_by is None


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
