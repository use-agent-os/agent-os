"""Offline unit tests for the Pilot R3-uplift hard-candidate miner.

Covers the deterministic, credential-free logic only — the real HF-stream /
OpenRouter prescreen path is exercised separately (dev-time, keyed) and is NOT
run in CI. What is tested here:

- direction heuristics (R3-like / R0-like prefilter table);
- prescreen reply parsing (strict JSON both-booleans, loose fallback, unusable);
- accept rule (both self_contained AND tier_match);
- concurrent prescreen plumbing with a *mocked* client: acceptance, per-direction
  target stop, cache-hit path, resumable cache append/reload;
- existing-corpus dedupe (turn id / conversation id / near-dup) + intra-run dedupe;
- frozen partition reuse (assign_split identity with the T5 sampler).

The miner pulls ``datasets`` / ``datasketch`` only inside optional code paths, so
the pure-Python logic imports without those groups; the LSH tests skip cleanly
when ``datasketch`` is absent (CI installs extras only, not the group).
"""

from __future__ import annotations

import importlib.util
import json
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import pytest

_HAS_DATASKETCH = find_spec("datasketch") is not None
requires_datasketch = pytest.mark.skipif(
    not _HAS_DATASKETCH, reason="datasketch (pilot-train group) not installed"
)

# Load scripts/pilot_router/mine_hard_candidates.py by path (scripts/ is not a
# package on sys.path); matches the other pilot script tests.
_MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "pilot_router" / "mine_hard_candidates.py"
)
_spec = importlib.util.spec_from_file_location("pilot_mine_hard_candidates", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
mn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mn)


# --------------------------------------------------------------------------- #
# Mock prescreen client
# --------------------------------------------------------------------------- #


class ScriptedPrescreen:
    """A PrescreenClient returning replies from a per-turn-text queue, or a
    default. Records (text, direction) calls."""

    def __init__(self, replies: dict[str, str] | None = None, default: str = "") -> None:
        self.replies = dict(replies or {})
        self.default = default
        self.calls: list[tuple[str, str]] = []

    def complete(self, text: str, direction: str) -> str:
        self.calls.append((text, direction))
        return self.replies.get(text, self.default)


# --------------------------------------------------------------------------- #
# 1. Direction heuristics (prefilter table)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,expected",
    [
        # R3: long
        ("x " * 400, mn.R3_LIKE),
        # R3: code fence
        ("fix this\n```python\nprint(1)\n```", mn.R3_LIKE),
        # R3: math/proof marker
        ("prove that the sum of two even numbers is even", mn.R3_LIKE),
        # R3: architecture keyword
        ("design a zero-downtime migration for our postgres shard", mn.R3_LIKE),
        # R3: security-analysis keyword
        ("do a security review of this auth flow and threat model it", mn.R3_LIKE),
        # R0: greeting
        ("hey there!", mn.R0_LIKE),
        ("thanks so much", mn.R0_LIKE),
        # R0: simple atomic lookup
        ("what is the capital of France", mn.R0_LIKE),
        ("who is the CEO of Apple", mn.R0_LIKE),
        # Neither: a medium factual paragraph question without hard markers
        (
            "Can you tell me a bit about how photosynthesis works in plants "
            "generally speaking for a school assignment please",
            None,
        ),
    ],
)
def test_direction_heuristic_table(text: str, expected: str | None) -> None:
    assert mn.direction_for(text) == expected


def test_r0_excludes_code_and_long() -> None:
    assert mn.is_r0_like("hello") is True
    # Long greeting-ish text is not R0 (over the char cap).
    assert mn.is_r0_like("hello " * 40) is False
    # Code fence disqualifies even if short.
    assert mn.is_r0_like("hi ```code```") is False


def test_r3_long_threshold_boundary() -> None:
    assert mn.is_r3_like("a" * (mn.R3_MIN_CHARS - 1)) is False
    assert mn.is_r3_like("a" * mn.R3_MIN_CHARS) is True


# --------------------------------------------------------------------------- #
# 2. Prescreen reply parsing + accept rule
# --------------------------------------------------------------------------- #


def test_parse_prescreen_strict_json() -> None:
    assert mn.parse_prescreen('{"self_contained": true, "tier_match": true}') == (
        True,
        True,
    )
    assert mn.parse_prescreen('{"self_contained": false, "tier_match": true}') == (
        False,
        True,
    )


def test_parse_prescreen_loose_fallback() -> None:
    raw = "Sure! self_contained: true, tier_match: false"
    assert mn.parse_prescreen(raw) == (True, False)


def test_parse_prescreen_unusable_returns_none() -> None:
    assert mn.parse_prescreen("") is None
    assert mn.parse_prescreen("no idea") is None
    # Missing one key -> unusable.
    assert mn.parse_prescreen('{"self_contained": true}') is None


def test_accepted_by_prescreen_requires_both() -> None:
    assert mn.accepted_by_prescreen((True, True)) is True
    assert mn.accepted_by_prescreen((True, False)) is False
    assert mn.accepted_by_prescreen((False, True)) is False
    assert mn.accepted_by_prescreen(None) is False


# --------------------------------------------------------------------------- #
# 3. Concurrent prescreen plumbing
# --------------------------------------------------------------------------- #


def _cand(tid: str, text: str, direction: str) -> dict[str, Any]:
    return {
        "turn_id": tid,
        "conversation_id": f"conv-{tid}",
        "text": text,
        "direction": direction,
    }


def test_prescreen_accepts_both_true(tmp_path: Path) -> None:
    cands = [_cand("t1", "design a system", mn.R3_LIKE)]
    client = ScriptedPrescreen({"design a system": '{"self_contained": true, "tier_match": true}'})
    accepted, screened, kept = mn.run_prescreen(
        cands, client, {}, r3_target=5, r0_target=5, workers=2, cache_path=tmp_path / "c.jsonl"
    )
    assert [c["turn_id"] for c in accepted] == ["t1"]
    assert kept[mn.R3_LIKE] == 1
    assert screened == 1


def test_prescreen_rejects_tier_false(tmp_path: Path) -> None:
    cands = [_cand("t1", "x", mn.R3_LIKE)]
    client = ScriptedPrescreen({"x": '{"self_contained": true, "tier_match": false}'})
    accepted, _, kept = mn.run_prescreen(
        cands, client, {}, r3_target=5, r0_target=5, workers=2, cache_path=tmp_path / "c.jsonl"
    )
    assert accepted == []
    assert kept[mn.R3_LIKE] == 0


def test_prescreen_stops_at_direction_target(tmp_path: Path) -> None:
    # 10 R3 candidates all accepted, but target is 3 -> at most 3 kept and the
    # rest not screened (early stop / skip).
    cands = [_cand(f"t{i}", f"design {i}", mn.R3_LIKE) for i in range(10)]
    client = ScriptedPrescreen(default='{"self_contained": true, "tier_match": true}')
    accepted, screened, kept = mn.run_prescreen(
        cands, client, {}, r3_target=3, r0_target=0, workers=2, cache_path=tmp_path / "c.jsonl"
    )
    assert kept[mn.R3_LIKE] == 3
    assert len(accepted) == 3


def test_prescreen_cache_hit_skips_client(tmp_path: Path) -> None:
    cands = [_cand("t1", "design a system", mn.R3_LIKE)]
    client = ScriptedPrescreen()  # would return "" (unusable) if called
    cache = {"t1": (True, True)}
    accepted, _, kept = mn.run_prescreen(
        cands, client, cache, r3_target=5, r0_target=5, workers=2, cache_path=tmp_path / "c.jsonl"
    )
    assert client.calls == []  # served from cache
    assert [c["turn_id"] for c in accepted] == ["t1"]


def test_prescreen_cache_append_and_reload(tmp_path: Path) -> None:
    cache_path = tmp_path / "c.jsonl"
    cands = [_cand("t1", "design a system", mn.R3_LIKE)]
    client = ScriptedPrescreen({"design a system": '{"self_contained": true, "tier_match": true}'})
    mn.run_prescreen(cands, client, {}, r3_target=5, r0_target=5, workers=2, cache_path=cache_path)
    reloaded = mn._load_cache(cache_path)
    assert reloaded == {"t1": (True, True)}


# --------------------------------------------------------------------------- #
# 4. Existing-corpus + intra-run dedupe
# --------------------------------------------------------------------------- #


def test_load_existing_corpus_ids(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(
        json.dumps({"turn_id": "a", "conversation_id": "c1", "text": "hi"})
        + "\n"
        + json.dumps({"turn_id": "b", "conversation_id": "c2", "text": "yo"})
        + "\n"
    )
    tids, cids = mn.load_existing_corpus_ids(corpus)
    assert tids == {"a", "b"}
    assert cids == {"c1", "c2"}


def test_load_existing_corpus_ids_missing_file(tmp_path: Path) -> None:
    tids, cids = mn.load_existing_corpus_ids(tmp_path / "nope.jsonl")
    assert tids == set() and cids == set()


@requires_datasketch
def test_is_novel_drops_existing_turn_and_conv(tmp_path: Path) -> None:
    from datasketch import MinHashLSH

    seen = MinHashLSH(threshold=0.85, num_perm=mn.MINHASH_PERMS)
    # turn_id already in corpus.
    assert (
        mn.is_novel(
            {"turn_id": "a", "conversation_id": "cX", "text": "brand new text here"},
            existing_turn_ids={"a"},
            existing_conv_ids=set(),
            existing_lsh=None,
            seen_lsh=seen,
        )
        is False
    )
    # conversation_id already in corpus (whole conversation already sampled).
    assert (
        mn.is_novel(
            {"turn_id": "z", "conversation_id": "c1", "text": "brand new text here"},
            existing_turn_ids=set(),
            existing_conv_ids={"c1"},
            existing_lsh=None,
            seen_lsh=seen,
        )
        is False
    )


@requires_datasketch
def test_is_novel_drops_near_dup_within_run(tmp_path: Path) -> None:
    from datasketch import MinHashLSH

    seen = MinHashLSH(threshold=0.85, num_perm=mn.MINHASH_PERMS)
    text = "design a highly available distributed queue with exactly once delivery"
    first = mn.is_novel(
        {"turn_id": "t1", "conversation_id": "c1", "text": text},
        existing_turn_ids=set(),
        existing_conv_ids=set(),
        existing_lsh=None,
        seen_lsh=seen,
    )
    second = mn.is_novel(
        {"turn_id": "t2", "conversation_id": "c2", "text": text},
        existing_turn_ids=set(),
        existing_conv_ids=set(),
        existing_lsh=None,
        seen_lsh=seen,
    )
    assert first is True
    assert second is False  # near-dup of the just-accepted candidate


@requires_datasketch
def test_is_novel_drops_near_dup_of_existing_corpus(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus.jsonl"
    text = "explain the tradeoffs between optimistic and pessimistic locking here"
    corpus.write_text(
        json.dumps({"turn_id": "old", "conversation_id": "cold", "text": text}) + "\n"
    )
    from datasketch import MinHashLSH

    existing_lsh = mn.build_existing_lsh(corpus, threshold=0.85)
    seen = MinHashLSH(threshold=0.85, num_perm=mn.MINHASH_PERMS)
    assert (
        mn.is_novel(
            {"turn_id": "new", "conversation_id": "cnew", "text": text},
            existing_turn_ids=set(),
            existing_conv_ids=set(),
            existing_lsh=existing_lsh,
            seen_lsh=seen,
        )
        is False
    )


# --------------------------------------------------------------------------- #
# 5. Frozen partition reuse
# --------------------------------------------------------------------------- #


def test_assign_split_is_the_frozen_t5_function() -> None:
    # The miner re-exports T5's assign_split; identical mapping, no drift.
    for cid in ("conv-1", "abc123", "deadbeef", "x"):
        assert mn.assign_split(cid) == mn.sc.assign_split(cid)
        assert mn.assign_split(cid) in {"train", "val", "test"}
