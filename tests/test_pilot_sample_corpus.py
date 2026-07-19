"""Offline unit tests for the Pilot corpus sampler (T5).

Covers the deterministic, credential-free logic only — the real HF-stream /
OpenRouter path is exercised separately behind the ``llm`` marker and is NOT
run in CI (spec §9 training-scripts row). What is tested here:

- conversation-id partition determinism + frozen golden + ~70/15/15 spread;
- self-containment LLM filter plumbing with a *mocked* client (yes/no parse,
  cache-hit path, malformed-response handling);
- language / redaction / triviality turn filters;
- MinHash near-dup dedupe on a synthetic fixture;
- coarse category heuristic + stratified selection.

The sampler pulls ``datasets`` only inside the streaming code path, so these
tests import the module without needing that dependency group installed.
"""

from __future__ import annotations

import importlib.util
import json
from collections import Counter
from importlib.util import find_spec
from pathlib import Path

import pytest

# datasketch / langdetect live in the ``pilot-train`` dependency *group*, which
# CI does not sync (it installs extras only — see .github/workflows/ci.yml).
# The pure-Python logic (partition, filters, category, self-containment,
# stratification) is always tested; the two tests that need those optional
# libraries skip cleanly when absent so the default CI run stays green.
_HAS_DATASKETCH = find_spec("datasketch") is not None
_HAS_LANGDETECT = find_spec("langdetect") is not None
requires_datasketch = pytest.mark.skipif(
    not _HAS_DATASKETCH, reason="datasketch (pilot-train group) not installed"
)
requires_langdetect = pytest.mark.skipif(
    not _HAS_LANGDETECT, reason="langdetect (pilot-train group) not installed"
)

# Load scripts/pilot_router/sample_corpus.py by path (scripts/ is not a package
# on sys.path); matches how the other pilot script tests reach their module.
_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "pilot_router"
    / "sample_corpus.py"
)
_spec = importlib.util.spec_from_file_location("pilot_sample_corpus", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)


# --------------------------------------------------------------------------- #
# 1. Partition determinism (spec §6.2 frozen partition contract)
# --------------------------------------------------------------------------- #


def test_partition_is_deterministic_for_same_id():
    for cid in ["abc123", "conversation-hash-xyz", "", "0", "ünïcodé"]:
        assert sc.assign_split(cid) == sc.assign_split(cid)


def test_partition_returns_only_valid_splits():
    for i in range(2000):
        assert sc.assign_split(f"conv-{i}") in {"train", "val", "test"}


def test_partition_distribution_is_roughly_70_15_15():
    counts = Counter(sc.assign_split(f"conv-{i}") for i in range(20_000))
    total = sum(counts.values())
    frac = {k: v / total for k, v in counts.items()}
    # Generous tolerance — this is a hash-bucket split, not exact.
    assert frac["train"] == pytest.approx(0.70, abs=0.02)
    assert frac["val"] == pytest.approx(0.15, abs=0.02)
    assert frac["test"] == pytest.approx(0.15, abs=0.02)


def test_partition_is_frozen_against_golden():
    # Golden values frozen 2026-07-18; if this test fails, the partition
    # function changed — that silently reassigns train/val/test and invalidates
    # every existing label/split. Do NOT update these values without an
    # owner-approved migration.
    golden = {
        "conv-0": "train",
        "conv-1": "train",
        "conv-2": "train",
        "conv-15": "val",
        "conv-11": "test",
        "conv-20": "test",
        "conv-50": "test",
    }
    # Actual assignments must match frozen expectations exactly.
    for cid, expected_split in golden.items():
        assert sc.assign_split(cid) == expected_split
    # Verify the partition function computation from internal bucket formula.
    for cid in golden:
        assert sc.assign_split(cid) == sc._split_for_bucket(sc._bucket(cid))


def test_all_turns_of_a_conversation_share_a_split():
    # The whole point of splitting by conversation_id: no turn leaks across.
    cid = "shared-conversation"
    splits = {sc.assign_split(cid) for _ in range(50)}
    assert len(splits) == 1


# --------------------------------------------------------------------------- #
# 2. Turn filters (language / redaction / triviality)
# --------------------------------------------------------------------------- #


def test_is_english_uses_metadata_when_present():
    assert sc.is_english("Bonjour le monde", lang_meta="English") is True
    assert sc.is_english("Hello world", lang_meta="Chinese") is False


@requires_langdetect
def test_is_english_falls_back_to_detector_without_metadata():
    assert sc.is_english(
        "This is clearly a fully English sentence about databases.", lang_meta=None
    )
    assert not sc.is_english(
        "Ceci est une phrase clairement écrite en français aujourd'hui.",
        lang_meta=None,
    )


def test_redaction_filter_drops_flagged_and_placeholder_turns():
    assert sc.is_redaction_clean("normal text", redacted=False) is True
    assert sc.is_redaction_clean("normal text", redacted=True) is False
    assert sc.is_redaction_clean("my email is [EMAIL]", redacted=False) is False
    assert sc.is_redaction_clean("call me at [PHONE_NUMBER]", redacted=False) is False


def test_triviality_filter():
    assert sc.is_substantive("Write a Python function to reverse a linked list.")
    assert not sc.is_substantive("")
    assert not sc.is_substantive("   \n  ")
    assert not sc.is_substantive("ok")


# --------------------------------------------------------------------------- #
# 3. Near-dup dedupe (MinHash)
# --------------------------------------------------------------------------- #


@requires_datasketch
def test_dedupe_collapses_near_duplicates():
    rows = [
        {"turn_id": 1, "text": "How do I sort a list in Python?"},
        {"turn_id": 2, "text": "how do i sort a list in python"},  # near-dup of 1
        {"turn_id": 3, "text": "Explain quantum entanglement in simple terms."},
        {"turn_id": 4, "text": "Write a haiku about the sea at dawn today."},
    ]
    kept = sc.dedupe_near_duplicates(rows, threshold=0.7)
    kept_ids = {r["turn_id"] for r in kept}
    # One of the two near-duplicates survives; the distinct ones both survive.
    assert 3 in kept_ids and 4 in kept_ids
    assert len({1, 2} & kept_ids) == 1
    assert len(kept) == 3


@requires_datasketch
def test_dedupe_keeps_all_distinct_rows():
    rows = [
        {"turn_id": i, "text": f"A completely distinct question number {i} about topic {i}."}
        for i in range(20)
    ]
    kept = sc.dedupe_near_duplicates(rows, threshold=0.8)
    assert len(kept) == 20


# --------------------------------------------------------------------------- #
# 4. Coarse category heuristic
# --------------------------------------------------------------------------- #


def test_categorize_covers_known_shapes():
    assert sc.categorize("Write a Python function that reverses a string") == "coding"
    assert sc.categorize("def foo():\n    return 1\nWhy does this error?") == "coding"
    assert sc.categorize("What is 12 * 47 + 3? Solve step by step.") == "math_reasoning"
    assert sc.categorize("Write me a short poem about autumn leaves") == "writing"
    assert sc.categorize("What is the capital of France?") == "factual_qa"
    assert sc.categorize("hey how are you doing today") == "chitchat"
    assert (
        sc.categorize("Search the web for the latest news on AI regulation")
        == "tool_use"
    )


def test_categorize_always_returns_a_known_class():
    for text in ["", "asdf qwerty", "The mitochondria is the powerhouse", "!!!"]:
        assert sc.categorize(text) in set(sc.CATEGORIES)


# --------------------------------------------------------------------------- #
# 5. Self-containment LLM filter plumbing (mocked client)
# --------------------------------------------------------------------------- #


class _FakeClient:
    """Records calls and returns a queued sequence of raw content strings."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def complete(self, text: str) -> str:
        self.calls += 1
        return self._responses.pop(0)


def test_self_contained_parses_yes_and_no():
    client = _FakeClient(['{"self_contained": true}', '{"self_contained": false}'])
    cache: dict[str, bool] = {}
    assert sc.check_self_contained("write a retry decorator", "t1", client, cache) is True
    assert sc.check_self_contained("now also add retry to that", "t2", client, cache) is False
    assert client.calls == 2


def test_self_contained_cache_hit_avoids_second_call():
    client = _FakeClient(['{"self_contained": true}'])
    cache: dict[str, bool] = {}
    a = sc.check_self_contained("standalone question", "same-id", client, cache)
    b = sc.check_self_contained("standalone question", "same-id", client, cache)
    assert a is b is True
    assert client.calls == 1  # second was served from cache
    assert cache["same-id"] is True


def test_self_contained_handles_malformed_response():
    # Malformed / non-JSON responses must not crash; they resolve to False
    # (conservative — a turn we cannot verify as standalone is dropped).
    client = _FakeClient(["garbage not json", "", "{}"])
    cache: dict[str, bool] = {}
    assert sc.check_self_contained("x", "a", client, cache) is False
    assert sc.check_self_contained("y", "b", client, cache) is False
    assert sc.check_self_contained("z", "c", client, cache) is False


def test_self_contained_accepts_loose_yes_no_text():
    # Model sometimes ignores the JSON contract and just says yes/no.
    client = _FakeClient(["Yes", "no", "YES it is self-contained"])
    cache: dict[str, bool] = {}
    assert sc.check_self_contained("a", "1", client, cache) is True
    assert sc.check_self_contained("b", "2", client, cache) is False
    assert sc.check_self_contained("c", "3", client, cache) is True


# --------------------------------------------------------------------------- #
# 6. Stratified selection on a synthetic fixture
# --------------------------------------------------------------------------- #


def _synthetic_rows(n_per_cat: int = 10):
    rows = []
    tid = 0
    samples = {
        "coding": "Write a Python function to parse JSON safely",
        "math_reasoning": "Compute the integral of x^2 step by step",
        "writing": "Write a short story about a lighthouse keeper",
        "factual_qa": "What is the tallest mountain in the world?",
        "chitchat": "hey there how is your day going friend",
        "tool_use": "Search the web for today's weather in Tokyo",
    }
    for cat, base in samples.items():
        for i in range(n_per_cat):
            rows.append(
                {
                    "turn_id": tid,
                    "conversation_id": f"conv-{cat}-{i}",
                    "text": f"{base} variant {i}",
                    "category": cat,
                }
            )
            tid += 1
    return rows


def test_stratified_select_hits_target_and_spreads_categories():
    rows = _synthetic_rows(n_per_cat=10)  # 60 rows, 6 cats
    selected = sc.stratified_select(rows, target=30)
    assert len(selected) == 30
    cats = Counter(r["category"] for r in selected)
    # Every category represented, roughly balanced (target/6 = 5 each).
    assert set(cats) == set(sc.CATEGORIES)
    for c in sc.CATEGORIES:
        assert cats[c] >= 4


def test_stratified_select_target_exceeds_supply_returns_all():
    rows = _synthetic_rows(n_per_cat=3)  # 18 rows
    selected = sc.stratified_select(rows, target=1000)
    assert len(selected) == 18


def test_stratified_select_is_deterministic():
    rows = _synthetic_rows(n_per_cat=10)
    a = [r["turn_id"] for r in sc.stratified_select(rows, target=24)]
    b = [r["turn_id"] for r in sc.stratified_select(rows, target=24)]
    assert a == b


# --------------------------------------------------------------------------- #
# 7. Concurrent LLM filter pass: caching, ordering, early-stop
# --------------------------------------------------------------------------- #


class _CountingClient:
    """Thread-safe-ish stub: 'yes' for turns whose text contains 'keep'."""

    def __init__(self):
        import threading

        self._lock = threading.Lock()
        self.calls = 0

    def complete(self, text: str) -> str:
        with self._lock:
            self.calls += 1
        return '{"self_contained": true}' if "keep" in text else '{"self_contained": false}'


def _turns(n):
    # Alternating keep/drop so accepted rate is ~50%.
    return [
        {"turn_id": f"t{i}", "text": ("keep this" if i % 2 == 0 else "drop that")}
        for i in range(n)
    ]


def test_run_llm_filter_returns_accepted_in_input_order(monkeypatch, tmp_path):
    monkeypatch.setattr(sc, "CACHE_PATH", tmp_path / "cache.jsonl")
    client = _CountingClient()
    accepted, screened = sc._run_llm_filter(
        _turns(20), client, cache={}, accept_pool=1000, workers=4
    )
    ids = [t["turn_id"] for t in accepted]
    # Only even-index ("keep") turns accepted, in original order.
    assert ids == [f"t{i}" for i in range(0, 20, 2)]
    assert screened == 20


def test_run_llm_filter_uses_cache_and_skips_calls(monkeypatch, tmp_path):
    monkeypatch.setattr(sc, "CACHE_PATH", tmp_path / "cache.jsonl")
    client = _CountingClient()
    cache = {f"t{i}": True for i in range(20)}  # everything pre-cached
    accepted, screened = sc._run_llm_filter(
        _turns(20), client, cache=cache, accept_pool=1000, workers=4
    )
    assert client.calls == 0  # nothing hit the network
    assert len(accepted) == 20  # cache said all self-contained


def test_run_llm_filter_early_stops_at_accept_pool(monkeypatch, tmp_path):
    monkeypatch.setattr(sc, "CACHE_PATH", tmp_path / "cache.jsonl")
    client = _CountingClient()
    accepted, screened = sc._run_llm_filter(
        _turns(400), client, cache={}, accept_pool=10, workers=8
    )
    # Stops once 10 accepted; must not have screened all 400.
    assert len(accepted) == 10
    assert screened < 400


def test_run_llm_filter_writes_resumable_cache(monkeypatch, tmp_path):
    cache_path = tmp_path / "cache.jsonl"
    monkeypatch.setattr(sc, "CACHE_PATH", cache_path)
    client = _CountingClient()
    sc._run_llm_filter(_turns(8), client, cache={}, accept_pool=1000, workers=4)
    reloaded = sc._load_cache(cache_path)
    assert len(reloaded) == 8
    assert reloaded["t0"] is True and reloaded["t1"] is False


# --------------------------------------------------------------------------- #
# 8. Extension: category quota, share cap, stratified filter, partition check
# --------------------------------------------------------------------------- #


def test_quota_full_requires_total_and_all_floors():
    # Below total target -> not full.
    counts = {c: 1000 for c in sc.CATEGORIES}
    assert sc._quota_full(counts, target=10_000_000) is False
    # Total met but a non-rare category below floor -> not full.
    counts = {c: sc.PER_CATEGORY_FLOOR for c in sc.CATEGORIES}
    counts["writing"] = sc.PER_CATEGORY_FLOOR - 1
    assert sc._quota_full(counts, target=100) is False
    # Total met, all non-rare at floor, tool_use at its soft floor -> full.
    counts = {c: sc.PER_CATEGORY_FLOOR for c in sc.CATEGORIES}
    counts["tool_use"] = sc.TOOL_USE_FLOOR
    assert sc._quota_full(counts, target=100) is True


def test_category_capped_respects_floor_then_share():
    # Under floor -> never capped even if it dominates a tiny total.
    counts = {c: 0 for c in sc.CATEGORIES}
    counts["factual_qa"] = sc.PER_CATEGORY_FLOOR - 1
    assert sc._category_capped("factual_qa", counts) is False
    # At/over floor AND over the share cap -> capped.
    counts = {c: 100 for c in sc.CATEGORIES}
    counts["factual_qa"] = 5000  # way over 35% of the total
    assert sc._category_capped("factual_qa", counts) is True
    # At/over floor but under the share cap -> not capped.
    counts = {c: 1000 for c in sc.CATEGORIES}  # each ~16.7%, under 35%
    assert sc._category_capped("coding", counts) is False


class _CatClient:
    """Returns self_contained=true for every turn (so acceptance is gated only
    by the category quota, not the verdict)."""

    def __init__(self):
        import threading

        self._lock = threading.Lock()
        self.calls = 0

    def complete(self, text: str) -> str:
        with self._lock:
            self.calls += 1
        return '{"self_contained": true}'


def _cat_turns():
    # Heavily skewed toward factual_qa to exercise the share cap.
    rows = []
    tid = 0
    templates = {
        "factual_qa": "What is fact number {i} about the world?",
        "coding": "Write a Python function number {i} to sort data",
        "writing": "Write a short poem number {i} about the sea",
        "math_reasoning": "Compute the integral number {i} step by step",
        "chitchat": "hey how are you doing today friend number {i}",
        "tool_use": "Search the web for topic number {i} online",
    }
    counts = {"factual_qa": 3000, "coding": 300, "writing": 300,
              "math_reasoning": 300, "chitchat": 300, "tool_use": 40}
    for cat, n in counts.items():
        for i in range(n):
            rows.append({"turn_id": f"{cat}-{tid}", "text": templates[cat].format(i=i)})
            tid += 1
    # Interleave so the stream isn't category-sorted.
    rows.sort(key=lambda r: r["turn_id"].split("-")[1])
    return rows


def test_stratified_filter_caps_dominant_category(monkeypatch, tmp_path):
    monkeypatch.setattr(sc, "CACHE_PATH", tmp_path / "cache.jsonl")
    # Small floors/target so the test is fast but exercises the same logic.
    monkeypatch.setattr(sc, "PER_CATEGORY_FLOOR", 50)
    monkeypatch.setattr(sc, "TOOL_USE_FLOOR", 10)
    client = _CatClient()
    accepted, screened, cat_counts = sc._run_llm_filter_stratified(
        _cat_turns(), client, cache={}, target=400, workers=8
    )
    total = len(accepted)
    # No category (that has a cap in play) should exceed the 35% share by much.
    assert cat_counts["factual_qa"] <= 0.36 * total + 1
    # Every non-rare category reached its floor; tool_use took what it found.
    for c in ["factual_qa", "coding", "writing", "math_reasoning", "chitchat"]:
        assert cat_counts[c] >= 50
    assert cat_counts["tool_use"] >= 10


def test_assert_partition_stable_passes_for_frozen_ids(tmp_path):
    corpus = tmp_path / "corpus.jsonl"
    with corpus.open("w") as fh:
        for cid in ["conv-a", "conv-b", "conv-c", "conv-d"]:
            fh.write(json.dumps(
                {"conversation_id": cid, "split": sc.assign_split(cid)}
            ) + "\n")
    assert sc._assert_partition_stable(corpus) == 4


def test_assert_partition_stable_detects_drift(tmp_path):
    corpus = tmp_path / "corpus.jsonl"
    cid = "conv-x"
    wrong = "val" if sc.assign_split(cid) != "val" else "test"
    with corpus.open("w") as fh:
        fh.write(json.dumps({"conversation_id": cid, "split": wrong}) + "\n")
    with pytest.raises(AssertionError, match="partition drift"):
        sc._assert_partition_stable(corpus)


def test_assert_partition_stable_no_prior_corpus(tmp_path):
    assert sc._assert_partition_stable(tmp_path / "missing.jsonl") == 0
