"""CJK bigrams were paired across the whole snippet, not within a run.

``_jaccard_similarity`` collected every CJK character in a snippet into one
flat list and paired neighbours in that list. Characters separated by Latin
text, punctuation or spaces are not adjacent, so the pairing invented tokens
that appear nowhere in the snippet::

    "会议 notes and 计划 draft"  ->  ... '议计' ...
                                        ^^^^  the tail of 会议 glued to the
                                              head of 计划 across " notes and "

Two snippets that share nothing then share a token, and the similarity used for
MMR diversity is inflated. ``_mmr_rerank`` penalises a candidate by its maximum
similarity to what is already selected, so an inflated score drops a memory the
search should have returned.

This is #3180, which ``memory_tools._memory_search_query_terms`` was corrected
for in ``e48c77cf`` ("build CJK search bigrams within a run, not across the
query"). That function's docstring names ``_jaccard_similarity`` as the shape it
shares -- the correction was not applied here.

Everything below asserts on ``_jaccard_similarity`` and ``_mmr_rerank``
themselves. The tokenizer is a closure with no seam, and a test that rebuilt it
would pass against the broken code it was meant to catch.
"""

from __future__ import annotations

import random

import pytest

from agentos.memory.retrieval import _jaccard_similarity, _mmr_rerank
from agentos.memory.types import MemorySearchResult, MemorySource


def _result(snippet: str, score: float, chunk_id: str) -> MemorySearchResult:
    return MemorySearchResult(
        chunk_id=chunk_id,
        path="MEMORY.md",
        source=MemorySource.memory,
        start_line=1,
        end_line=1,
        snippet=snippet,
        score=score,
    )


# ── the report ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "phantom", "real"),
    [
        ("会议 notes and 计划 draft", "议计", "会议"),
        ("天气预报, 会议记录", "报会", "预报"),
        ("项目A 客户B", "目客", "项目"),
        ("发票\n合同", "票合", "发票"),
    ],
)
def test_a_bigram_spanning_a_gap_is_not_shared(text, phantom, real):
    """A two-character probe that does not occur in *text* must score below one
    that does. Under the flat pairing both were tokens, so both scored alike."""
    assert phantom not in text, "the probe must genuinely not occur"
    assert real in text

    assert _jaccard_similarity(text, phantom) < _jaccard_similarity(text, real), (
        f"{phantom!r} is not in {text!r} but scores as high as {real!r}"
    )


def test_two_unrelated_snippets_do_not_share_an_invented_token():
    left = "会议 notes and 计划 draft"
    right = "议计 quarterly budget review"
    # 议 and 计 are still shared as unigrams -- that is real. The bigram that
    # exists in neither snippet no longer inflates the score on top of them.
    assert _jaccard_similarity(left, right) < 0.25


def test_the_same_characters_adjacent_do_score_higher():
    """The control for the test above: when the run really is unbroken, the
    bigram is real and the score is higher."""
    spanning = _jaccard_similarity("会议 notes 计划", "议计")
    adjacent = _jaccard_similarity("会议计划", "议计")
    assert adjacent > spanning


# ── the consequence for search results ─────────────────────────────────────


def test_mmr_keeps_the_memory_an_invented_token_displaced():
    """A membership change, not a reordering: one memory is swapped for another."""
    results = [
        _result("update 客户 发票 会议", 0.930, "0"),
        _result("budget 会议 客户 预算", 0.994, "1"),
        _result("meeting 计划 项目", 0.640, "2"),
        _result("meeting plan", 0.885, "3"),
        _result("report 项目", 0.947, "4"),
        _result("notes meeting report", 0.585, "5"),
    ]
    picked = [r.chunk_id for r in _mmr_rerank(results, lam=0.7, k=3)]
    assert picked == ["1", "4", "3"]


# ── the property ───────────────────────────────────────────────────────────


def test_fuzz_a_probe_absent_from_a_snippet_never_beats_one_present():
    """5,000 snippets. Any two-character probe that is not a substring of the
    snippet must not score at or above one that is."""
    rng = random.Random(7)
    words = ["会议", "计划", "预算", "天气预报", "报告", "项目", "客户", "发票"]
    latin = ["notes", "draft", "review", "plan", "-", ",", "\n"]
    failures = []
    for _ in range(5_000):
        text = " ".join(
            rng.choice(words) if rng.random() < 0.5 else rng.choice(latin)
            for _ in range(rng.randint(2, 6))
        )
        cjk = [c for c in text if "一" <= c <= "鿿"]
        for i in range(len(cjk) - 1):
            probe = cjk[i] + cjk[i + 1]
            if probe in text:
                continue
            present = next((w for w in words if len(w) == 2 and w in text), None)
            if present is None:
                continue
            if _jaccard_similarity(text, probe) >= _jaccard_similarity(text, present):
                failures.append((text, probe, present))
                break
    assert failures == [], failures[:3]


# ── guards ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("会议计划", "会议计划", 1.0),
        ("会议计划", "天气预报", 0.0),
        ("meeting plan", "meeting plan", 1.0),
        ("budget review notes", "budget review notes", 1.0),
        ("budget review", "weather summary", 0.0),
    ],
)
def test_identical_and_disjoint_snippets_are_unchanged(left, right, expected):
    assert _jaccard_similarity(left, right) == pytest.approx(expected)


def test_an_unbroken_run_still_matches_its_own_bigrams():
    """The bigrams that should exist still do: an exact substring run scores
    higher against the whole than an unrelated run of the same length."""
    assert _jaccard_similarity("会议计划", "会议计") > _jaccard_similarity("会议计划", "天气预")
