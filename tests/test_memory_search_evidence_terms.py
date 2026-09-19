"""memory_search evidence must centre on the line the query hit, in any script.

``_memory_search_query_terms`` used to tokenize with ``[A-Za-z0-9]+``. A query
written in Cyrillic, Greek, Hangul, Japanese or accented Latin produced no usable
terms, so ``_query_centered_evidence`` bailed out and the tool fell back to the
first 900 characters of the file -- reporting a match while showing an excerpt
that does not contain it.

Every assertion here runs through the registered ``memory_search`` tool, not the
helper, so the evidence string checked is the one a model actually receives.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentos.memory.types import MemorySearchResult, MemorySource
from agentos.tools.builtin.memory_tools import _memory_search_query_terms, create_memory_tools
from agentos.tools.registry import ToolRegistry

# _MEMORY_SEARCH_EVIDENCE_CHARS is 900; the padding pushes the tail well past it
# so the centring path is the one under test rather than the "short enough to
# return whole" shortcut.
_PADDING = "\n".join(
    f"unrelated release note line {index} covering rollout and staffing" for index in range(20)
)


def _memory_search(text: str):
    """Register memory_search over a retriever that returns exactly *text*."""

    class _Retriever:
        async def search(self, query, opts, *, intent):
            return [
                MemorySearchResult(
                    chunk_id="chunk-1",
                    path="MEMORY.md",
                    source=MemorySource.memory,
                    start_line=1,
                    end_line=text.count("\n") + 1,
                    snippet=text[:80],
                    score=0.9,
                    text=text,
                )
            ]

    registry = ToolRegistry()
    create_memory_tools(
        stores=SimpleNamespace(),
        retrievers=_Retriever(),
        memory_dir=".",
        registry=registry,
    )
    registered = registry.get("memory_search")
    assert registered is not None
    return registered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "target"),
    [
        # Cyrillic
        ("ротация", "Ротация пароля Постгреса выполняется каждый вторник."),
        # Greek
        ("συνάντηση", "Η συνάντηση με τον πελάτη μεταφέρθηκε στην Τρίτη."),
        # Hangul
        ("배포", "스테이징 배포는 금요일 오후에 동결됩니다."),
        # Japanese kana + kanji (no spaces to tokenize on)
        ("予算", "来期の予算は経理部の承認待ちです。"),
    ],
)
async def test_evidence_centres_on_the_matching_line_for_a_non_latin_query(query, target):
    """RED on main: the excerpt came back as the head of the file, not the hit."""
    text = _PADDING + "\n" + target
    output = await _memory_search(text).handler(query=query)

    assert target in output, (
        f"memory_search reported a hit for {query!r} but the evidence excerpt "
        f"does not contain the matching line.\n--- evidence ---\n{output}"
    )


@pytest.mark.asyncio
async def test_an_accented_query_does_not_centre_on_an_unrelated_line():
    """RED on main: ``café`` was mangled to ``caf``, which hit ``cafeteria``.

    Not merely a missed excerpt -- the old tokenizer pointed the reader at a
    different, unrelated line and gave no sign it had done so.
    """
    decoy = "The cafeteria menu is unchanged for the rest of the quarter."
    target = "Déjà décidé: la réunion passe au bureau de Lyon."
    text = decoy + "\n" + _PADDING + "\n" + target

    output = await _memory_search(text).handler(query="café déjà")

    assert target in output, f"expected the déjà line in the excerpt:\n{output}"
    assert decoy not in output, f"excerpt centred on the unrelated cafeteria line:\n{output}"


@pytest.mark.asyncio
async def test_positive_control_an_ascii_query_still_centres_on_its_line():
    """Guard, green on both sides: proves the centring path is live in this setup.

    Without it a non-Latin assertion could pass vacuously on a harness where the
    excerpt is never centred at all.
    """
    target = "Postgres password rotation runs every Tuesday evening."
    text = _PADDING + "\n" + target

    output = await _memory_search(text).handler(query="postgres rotation")

    assert target in output


def test_ascii_tokenization_is_unchanged():
    """Guard, green on both sides: the widened class must not move ASCII queries.

    ``[^\\W_]+`` keeps underscore a separator precisely so that these stay equal
    to what ``[A-Za-z0-9]+`` produced.
    """
    assert _memory_search_query_terms("Deploy the foo_bar service v2") == (
        "deploy",
        "foo",
        "bar",
        "service",
    )
    # Stop words, sub-3-character tokens and duplicates are still dropped.
    assert _memory_search_query_terms("who has the key to the key") == ("key",)
