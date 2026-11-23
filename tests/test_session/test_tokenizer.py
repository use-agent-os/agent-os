"""Token estimation of text that quotes a tokenizer's reserved spellings (#3739).

tiktoken's ``encode`` refuses text that contains a special token's spelling
(``<|endoftext|>`` and friends) unless told otherwise. Conversation text can
quote one -- a question about tokenizer formats, a fetched page about them --
and the estimator then raised instead of counting, failing every turn of a
session whose transcript held it. The spelling is counted as the ordinary
text it is, never as one special token.

The encoding here is a real ``tiktoken.Encoding`` built offline: one token
per byte, plus ``cl100k_base``'s special tokens. A count equal to the byte
length therefore proves the spelling was read as text.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentos.gateway.config import ContextOverflowPolicy, GatewayConfig
from agentos.gateway.context_overflow import apply_context_overflow_policy
from agentos.session import tokenizer
from agentos.session.tokenizer import estimate_tokens

tiktoken = pytest.importorskip("tiktoken")

#: The special tokens of ``cl100k_base``.
SPECIAL_SPELLINGS = (
    "<|endoftext|>",
    "<|fim_prefix|>",
    "<|fim_middle|>",
    "<|fim_suffix|>",
    "<|endofprompt|>",
)


@pytest.fixture(autouse=True)
def byte_encoding(monkeypatch: pytest.MonkeyPatch) -> object:
    encoding = tiktoken.Encoding(
        name="agentos-test-bytes",
        pat_str=r"\S+|\s+",
        mergeable_ranks={bytes([value]): value for value in range(256)},
        special_tokens={spelling: 256 + i for i, spelling in enumerate(SPECIAL_SPELLINGS)},
    )
    monkeypatch.setattr(tokenizer, "_encoding", encoding)
    monkeypatch.setattr(tokenizer, "_tiktoken_available", True)
    return encoding


def _byte_count(text: str) -> int:
    return len(text.encode("utf-8"))


def test_a_sentence_quoting_a_reserved_spelling_is_counted() -> None:
    text = "Please explain the literal string <|endoftext|>."

    assert estimate_tokens(text) == _byte_count(text)


@pytest.mark.parametrize("spelling", SPECIAL_SPELLINGS)
def test_a_bare_reserved_spelling_counts_as_ordinary_text(spelling: str) -> None:
    # As a special token it would be a single token.
    assert estimate_tokens(spelling) == _byte_count(spelling) > 1


def test_ordinary_text_counts_as_before() -> None:
    assert estimate_tokens("hello world") == _byte_count("hello world")


async def test_the_context_overflow_check_counts_a_quoted_spelling() -> None:
    # ``chat.send`` runs this check before every turn: a raise there fails
    # the turn, and with the spelling in the transcript every later turn too.
    message = "What does <|endoftext|> mean?"
    entry = SimpleNamespace(content="A page about <|fim_prefix|> and <|endofprompt|>.")

    outcome = await apply_context_overflow_policy(
        config=GatewayConfig(),
        message=message,
        transcript=[entry],
        session_key="agent:main:webchat:tokenizer",
        policy_override=ContextOverflowPolicy.REFUSE,
        budget_override=100_000,
    )

    assert not outcome.over_budget
    assert outcome.estimated_tokens == _byte_count(message) + _byte_count(entry.content)
