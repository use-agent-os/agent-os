from __future__ import annotations

import pytest

from agentos.tools.builtin import memory_tools


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # CJK: Japanese
        ("東京カンファレンス", ("東京カンファレンス",)),
        ("東京", ("東京",)),
        ("ありがとう", ("ありがとう",)),
        ("プロジェクト承認完了", ("プロジェクト承認完了",)),
        # CJK: Chinese (Simplified & Traditional)
        ("人工智能", ("人工智能",)),
        ("深度学习模型", ("深度学习模型",)),
        ("繁體中文測試", ("繁體中文測試",)),
        # CJK: Korean (Hangul)
        ("인공지능 프로젝트", ("인공지능", "프로젝트")),
        ("데이터 분석", ("데이터", "분석")),
        # CJK: Single character ideographs
        ("猫", ("猫",)),
        ("車", ("車",)),
        # Cyrillic
        ("Москва конференция", ("москва", "конференция")),
        ("архитектура системы", ("архитектура", "системы")),
        ("привет мир", ("привет", "мир")),
        # Arabic
        ("مؤتمر الذكاء الاصطناعي", ("مؤتمر", "الذكاء", "الاصطناعي")),
        ("تحليل البيانات", ("تحليل", "البيانات")),
        # Accented Latin (German, French, Spanish, Nordic)
        ("Überprüfung der Systeme", ("überprüfung", "der", "systeme")),
        ("café et résumé", ("café", "résumé")),
        ("año nuevo smörgåsbord", ("año", "nuevo", "smörgåsbord")),
        # Greek
        ("συνέδριο τεχνολογίας", ("συνέδριο", "τεχνολογίας")),
        ("τεχνητής νοημοσύνης", ("τεχνητής", "νοημοσύνης")),
        # Mixed scripts & ASCII
        (
            "agentos 東京 release 2026",
            ("agentos", "東京", "release", "2026"),
        ),
        (
            "the 東京 and café with ai",
            ("東京", "café"),
        ),
        # ASCII filtering rules preserved
        ("a to in of", ()),
        ("___", ()),
        ("___init___", ("___init___",)),
        ("東京 東京 café café", ("東京", "café")),
    ],
)
def test_memory_search_query_terms_multilingual_script_families(
    query: str, expected: tuple[str, ...]
) -> None:
    assert memory_tools._memory_search_query_terms(query) == expected


@pytest.mark.parametrize(
    ("script_label", "query", "target_line_content"),
    [
        ("cjk_japanese", "東京カンファレンス", "東京カンファレンスの議事録: プロジェクト承認完了"),
        ("cjk_chinese", "人工智能", "第三季度人工智能模型优化进展报告"),
        ("cjk_korean", "프로젝트", "2026년 차세대 프로젝트 로드맵 승인"),
        ("cjk_single_char", "猫", "研究室のアイドル猫の名前はタマ"),
        ("cyrillic", "конференция", "Москва конференция: результаты ежегодного аудита"),
        ("arabic", "الذكاء", "مؤتمر الذكاء الاصطناعي وتطبيقاته الحديثة"),
        (
            "accented_latin",
            "überprüfung",
            "Wichtige Überprüfung der Systemarchitektur abgeschlossen",
        ),
        ("greek", "συνέδριο", "Διεθνές συνέδριο τεχνολογίας και καινοτομίας 2026"),
    ],
)
def test_bounded_memory_search_evidence_centers_on_all_script_families(
    script_label: str, query: str, target_line_content: str
) -> None:
    lines = [f"line {i}: introductory metadata and baseline context" for i in range(200)]
    lines[150] = target_line_content
    content = "\n".join(lines)

    evidence = memory_tools._bounded_memory_search_evidence(content, query=query)

    # Must contain target line and NOT degrade to head of file (line 0) or tail of file (line 199)
    assert target_line_content in evidence
    assert "line 0:" not in evidence
    assert "line 199:" not in evidence


def test_truncate_line_around_query_unicode() -> None:
    prefix = "alpha " * 50
    unicode_hit = "東京カンファレンス"
    suffix = " omega" * 50
    long_line = f"{prefix} {unicode_hit} {suffix}"

    truncated = memory_tools._truncate_line_around_query(
        long_line, (unicode_hit.lower(),), budget=80
    )
    assert unicode_hit in truncated
    assert len(truncated) <= 80 + len("... ") + len(" ...")
