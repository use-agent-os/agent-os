"""Offline unit tests for the Pilot labeling harness (T6).

Covers the deterministic, credential-free logic only — the real OpenCAP
labeling path is exercised separately (dev-time, keyed) and is NOT run in CI.
What is tested here with a *mocked* label client:

- two-pass agreement path (A == B -> no adjudication, agreement=True);
- disagreement -> adjudication path (A != B -> third call decides);
- malformed-JSON retry-then-drop handling (per pass and in adjudication);
- resumable cache hits (cached passes cost no client call);
- boundary_set tagging for adjudicated TEST-split conversations only;
- dry-run stratification over a synthetic corpus fixture;
- reply parsing (strict JSON, loose single-token, unusable -> None);
- prompt orderings differ and never leak pass identity in adjudication;
- gate evaluation (pass / each-criterion-fail) and full-cost projection;
- run durability: unexpected worker exceptions skip-and-log, fatal ones stop;
- WAF-403 handling: HTML-body 403 is transient (retried), JSON 403 stays
  fatal, the consecutive-block fuse aborts, and a success resets the streak;
- rubric file exists and its sha256 is recorded consistently.

The harness pulls ``httpx`` only inside the real client, so these tests import
the module without needing that dependency installed.
"""

from __future__ import annotations

import importlib.util
import json
import threading
from pathlib import Path
from typing import Any

import pytest

# Load scripts/pilot_router/label_corpus.py by path (scripts/ is not a package
# on sys.path); matches how the other pilot script tests reach their module.
_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "pilot_router" / "label_corpus.py"
_spec = importlib.util.spec_from_file_location("pilot_label_corpus", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
lc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lc)


# --------------------------------------------------------------------------- #
# Mock client
# --------------------------------------------------------------------------- #


class ScriptedClient:
    """A LabelClient that returns replies from a per-order queue.

    ``replies`` maps a routing key to a list of raw reply strings consumed in
    order. The routing key is derived from the system prompt so a test can steer
    pass A, pass B, and adjudication independently:

    - pass A system prompt contains the A ordering id
    - pass B system prompt contains the B ordering id
    - adjudication system prompt starts with "Two independent graders"
    """

    def __init__(self, replies: dict[str, list[str]]) -> None:
        self.replies = {k: list(v) for k, v in replies.items()}
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def _route(self, system: str) -> str:
        if system.startswith("Two independent graders"):
            return "ADJ"
        if lc.PROMPT_ORDER_A in system:
            return "A"
        if lc.PROMPT_ORDER_B in system:
            return "B"
        return "?"

    def complete(self, system: str, user: str) -> tuple[str, dict[str, Any]]:
        with self._lock:
            self.calls.append((self._route(system), user))
            key = self._route(system)
            queue = self.replies.get(key, [])
            reply = queue.pop(0) if queue else '{"label": "R1", "why": "default"}'
        return reply, {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0}


def _turn(tid: str = "t1", split: str = "train", cat: str = "coding") -> dict[str, Any]:
    return {
        "turn_id": tid,
        "conversation_id": f"conv-{tid}",
        "split": split,
        "category": cat,
        "text": "write a function to merge overlapping intervals",
    }


def _fresh_cache_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the module's on-disk cache at a temp file so tests never touch the
    real (git-ignored) cache."""
    monkeypatch.setattr(lc, "CACHE_PATH", tmp_path / "label_cache.jsonl")


# --------------------------------------------------------------------------- #
# 1. Reply parsing
# --------------------------------------------------------------------------- #


def test_parse_label_strict_json():
    assert lc.parse_label('{"label": "R2", "why": "multi-step"}') == ("R2", "multi-step")


def test_parse_label_case_insensitive_label():
    assert lc.parse_label('{"label": "r3", "why": "x"}') == ("R3", "x")


def test_parse_label_loose_single_token():
    assert lc.parse_label("The tier is R1.") == ("R1", "")


def test_parse_label_rejects_empty_and_ambiguous():
    assert lc.parse_label("") is None
    assert lc.parse_label("could be R1 or R2") is None  # two distinct tokens
    assert lc.parse_label('{"label": "R9"}') is None  # not a valid tier
    assert lc.parse_label("no label at all") is None


# --------------------------------------------------------------------------- #
# 2. Prompt construction
# --------------------------------------------------------------------------- #


def test_prompt_orderings_differ_and_carry_ids():
    a = lc.build_label_system_prompt(lc.PROMPT_ORDER_A)
    b = lc.build_label_system_prompt(lc.PROMPT_ORDER_B)
    assert lc.PROMPT_ORDER_A in a and lc.PROMPT_ORDER_B in b
    assert a != b
    # A lists R0 before R3; B lists R3 before R0.
    assert a.index("R0 (") < a.index("R3 (")
    assert b.index("R3 (") < b.index("R0 (")


def test_adjudication_prompt_does_not_reveal_pass_identity():
    # Both candidate labels appear sorted; "pass A"/"pass B" never named.
    content = lc.build_adjudication_user_content("some turn", "R3", "R1")
    assert "R1 and R3" in content  # sorted, not "R3 and R1"
    assert "pass A" not in content.lower() and "pass b" not in content.lower()


# --------------------------------------------------------------------------- #
# 3. Two-pass agreement path
# --------------------------------------------------------------------------- #


def test_agreement_path_no_adjudication(tmp_path, monkeypatch):
    _fresh_cache_path(tmp_path, monkeypatch)
    client = ScriptedClient(
        {
            "A": ['{"label": "R2", "why": "coupled steps"}'],
            "B": ['{"label": "R2", "why": "coupled steps"}'],
        }
    )
    row = lc.label_turn(_turn(), client, {}, threading.Lock())
    assert row is not None
    assert row["label"] == "R2"
    assert row["agreement"] is True
    assert row["adjudicated"] is False
    assert row["boundary_set"] is False
    # No adjudication call was made.
    assert not any(r == "ADJ" for r, _ in client.calls)


# --------------------------------------------------------------------------- #
# 4. Disagreement -> adjudication path
# --------------------------------------------------------------------------- #


def test_disagreement_triggers_adjudication(tmp_path, monkeypatch):
    _fresh_cache_path(tmp_path, monkeypatch)
    client = ScriptedClient(
        {
            "A": ['{"label": "R1", "why": "looks simple"}'],
            "B": ['{"label": "R2", "why": "actually coupled"}'],
            "ADJ": ['{"label": "R2", "why": "final call"}'],
        }
    )
    row = lc.label_turn(_turn(), client, {}, threading.Lock())
    assert row is not None
    assert row["label"] == "R2"
    assert row["agreement"] is False
    assert row["adjudicated"] is True
    assert any(r == "ADJ" for r, _ in client.calls)


def test_adjudication_can_pick_a_third_tier(tmp_path, monkeypatch):
    # Adjudicator is not restricted to the two candidates in our contract; the
    # harness records whatever valid tier it returns.
    _fresh_cache_path(tmp_path, monkeypatch)
    client = ScriptedClient(
        {
            "A": ['{"label": "R1"}'],
            "B": ['{"label": "R3"}'],
            "ADJ": ['{"label": "R2", "why": "middle"}'],
        }
    )
    row = lc.label_turn(_turn(), client, {}, threading.Lock())
    assert row is not None and row["label"] == "R2"


# --------------------------------------------------------------------------- #
# 5. Malformed-JSON retry then drop
# --------------------------------------------------------------------------- #


def test_pass_retries_then_succeeds(tmp_path, monkeypatch):
    _fresh_cache_path(tmp_path, monkeypatch)
    client = ScriptedClient(
        {
            "A": ["garbage", '{"label": "R0", "why": "ok now"}'],  # retry succeeds
            "B": ['{"label": "R0", "why": "trivial"}'],
        }
    )
    row = lc.label_turn(_turn(), client, {}, threading.Lock())
    assert row is not None and row["label"] == "R0"


def test_pass_drops_after_exhausting_retries(tmp_path, monkeypatch):
    _fresh_cache_path(tmp_path, monkeypatch)
    # MAX_LABEL_RETRIES=2 -> 3 attempts; all garbage -> drop (row is None).
    client = ScriptedClient({"A": ["x", "y", "z", "still-bad"]})
    row = lc.label_turn(_turn(), client, {}, threading.Lock())
    assert row is None


def test_adjudication_malformed_drops_turn(tmp_path, monkeypatch):
    _fresh_cache_path(tmp_path, monkeypatch)
    client = ScriptedClient(
        {
            "A": ['{"label": "R1"}'],
            "B": ['{"label": "R2"}'],
            "ADJ": ["nope", "still nope", "bad"],  # all unparseable -> drop
        }
    )
    row = lc.label_turn(_turn(), client, {}, threading.Lock())
    assert row is None


# --------------------------------------------------------------------------- #
# 6. Cache hits (resumable)
# --------------------------------------------------------------------------- #


def test_cache_hit_skips_client_calls(tmp_path, monkeypatch):
    _fresh_cache_path(tmp_path, monkeypatch)
    # Pre-populate the in-memory cache for both passes of one turn.
    tid = "t1"
    cache = {
        lc._cache_key(tid, lc.PASS_A): {"label": "R1", "why": "cached A"},
        lc._cache_key(tid, lc.PASS_B): {"label": "R1", "why": "cached A"},
    }
    client = ScriptedClient({})  # any call would append a default reply
    row = lc.label_turn(_turn(tid), client, cache, threading.Lock())
    assert row is not None and row["label"] == "R1"
    assert client.calls == []  # fully served from cache


def test_cache_appended_and_reloaded(tmp_path, monkeypatch):
    _fresh_cache_path(tmp_path, monkeypatch)
    client = ScriptedClient(
        {"A": ['{"label": "R2", "why": "w"}'], "B": ['{"label": "R2", "why": "w"}']}
    )
    cache: dict[str, Any] = {}
    lc.label_turn(_turn("t9"), client, cache, threading.Lock())
    # The on-disk log now round-trips through _load_cache with both passes.
    reloaded = lc._load_cache(lc.CACHE_PATH)
    assert reloaded[lc._cache_key("t9", lc.PASS_A)]["label"] == "R2"
    assert reloaded[lc._cache_key("t9", lc.PASS_B)]["label"] == "R2"


# --------------------------------------------------------------------------- #
# 7. boundary_set tagging (adjudicated TEST-split only)
# --------------------------------------------------------------------------- #


def _adj_client() -> ScriptedClient:
    return ScriptedClient(
        {
            "A": ['{"label": "R1"}'],
            "B": ['{"label": "R2"}'],
            "ADJ": ['{"label": "R2", "why": "f"}'],
        }
    )


def test_boundary_set_true_for_adjudicated_test_split(tmp_path, monkeypatch):
    _fresh_cache_path(tmp_path, monkeypatch)
    row = lc.label_turn(_turn(split="test"), _adj_client(), {}, threading.Lock())
    assert row is not None
    assert row["adjudicated"] is True
    assert row["boundary_set"] is True


def test_boundary_set_false_for_adjudicated_train_split(tmp_path, monkeypatch):
    _fresh_cache_path(tmp_path, monkeypatch)
    row = lc.label_turn(_turn(split="train"), _adj_client(), {}, threading.Lock())
    assert row is not None
    assert row["adjudicated"] is True
    assert row["boundary_set"] is False


def test_boundary_set_false_when_test_split_agrees(tmp_path, monkeypatch):
    # Agreement on a test-split turn is NOT a boundary item (no adjudication).
    _fresh_cache_path(tmp_path, monkeypatch)
    client = ScriptedClient({"A": ['{"label": "R1"}'], "B": ['{"label": "R1"}']})
    row = lc.label_turn(_turn(split="test"), client, {}, threading.Lock())
    assert row is not None
    assert row["boundary_set"] is False


# --------------------------------------------------------------------------- #
# 8. Dry-run stratification
# --------------------------------------------------------------------------- #


def _synthetic_corpus() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    # Lots of coding + factual_qa in train, a few of each other, plus val/test.
    for i in range(50):
        rows.append(_turn(f"code{i}", "train", "coding"))
    for i in range(50):
        rows.append(_turn(f"qa{i}", "train", "factual_qa"))
    for i in range(5):
        rows.append(_turn(f"chit{i}", "train", "chitchat"))
        rows.append(_turn(f"write{i}", "train", "writing"))
        rows.append(_turn(f"math{i}", "train", "math_reasoning"))
        rows.append(_turn(f"tool{i}", "train", "tool_use"))
    for i in range(20):
        rows.append(_turn(f"val{i}", "val", "coding"))
        rows.append(_turn(f"test{i}", "test", "coding"))
    return rows


def test_dry_run_is_train_only_and_stratified():
    rows = _synthetic_corpus()
    sample = lc.stratified_dry_run_sample(rows, 30)
    assert len(sample) == 30
    # Never leaves the train split.
    assert all(r["split"] == "train" for r in sample)
    # Every category present in train appears at least once (round-robin).
    cats = {r["category"] for r in sample}
    assert cats == {
        "coding",
        "factual_qa",
        "chitchat",
        "writing",
        "math_reasoning",
        "tool_use",
    }


def test_dry_run_returns_all_when_train_smaller_than_n():
    rows = [_turn(f"c{i}", "train", "coding") for i in range(5)]
    rows += [_turn("v", "val", "coding")]
    sample = lc.stratified_dry_run_sample(rows, 100)
    assert len(sample) == 5  # all train turns, val excluded


def test_dry_run_is_deterministic():
    rows = _synthetic_corpus()
    a = [r["turn_id"] for r in lc.stratified_dry_run_sample(rows, 40)]
    b = [r["turn_id"] for r in lc.stratified_dry_run_sample(rows, 40)]
    assert a == b


# --------------------------------------------------------------------------- #
# 9. Stats + gate + projection
# --------------------------------------------------------------------------- #


def _rows_for_stats() -> list[dict[str, Any]]:
    def mk(label, split, agree, adj, boundary):
        return {
            "turn_id": "x",
            "conversation_id": "c",
            "split": split,
            "category": "coding",
            "label": label,
            "why": "",
            "agreement": agree,
            "adjudicated": adj,
            "boundary_set": boundary,
        }

    return [
        mk("R0", "train", True, False, False),
        mk("R1", "train", True, False, False),
        mk("R2", "train", False, True, False),  # adjudicated train
        mk("R3", "test", False, True, True),  # adjudicated test -> boundary
    ]


def test_compute_stats_rates_and_distributions():
    stats = lc.compute_stats(_rows_for_stats())
    assert stats["labeled"] == 4
    assert stats["label_counts"] == {"R0": 1, "R1": 1, "R2": 1, "R3": 1}
    assert stats["agreement_rate"] == 0.5
    assert stats["adjudication_rate"] == 0.5
    assert stats["boundary_set_size"] == 1
    assert stats["per_split_label_counts"]["test"]["R3"] == 1


def test_gate_passes_when_all_criteria_met():
    stats = lc.compute_stats(_rows_for_stats())  # agreement 0.5 too low on its own
    # Force an agreement-passing distribution.
    stats["agreement_rate"] = 0.80
    ok, rows = lc.evaluate_gate(stats, projected_cost=30.0)
    assert ok is True
    assert all(r["pass"] for r in rows)


def test_gate_fails_on_low_agreement():
    stats = lc.compute_stats(_rows_for_stats())
    stats["agreement_rate"] = 0.60
    ok, rows = lc.evaluate_gate(stats, projected_cost=30.0)
    assert ok is False
    assert rows[0]["pass"] is False


def test_gate_fails_when_a_class_missing():
    rows_in = _rows_for_stats()[:2]  # only R0, R1 present
    stats = lc.compute_stats(rows_in)
    stats["agreement_rate"] = 0.90
    ok, rows = lc.evaluate_gate(stats, projected_cost=10.0)
    assert ok is False
    assert rows[1]["pass"] is False


def test_gate_fails_when_class_over_70pct():
    rows_in = [
        {
            "turn_id": str(i),
            "conversation_id": "c",
            "split": "train",
            "category": "coding",
            "label": "R1" if i < 9 else ("R0" if i == 9 else "R2"),
            "why": "",
            "agreement": True,
            "adjudicated": False,
            "boundary_set": False,
        }
        for i in range(11)
    ]
    # 9/11 R1 ~ 0.82 > 0.70, and R3 missing.
    stats = lc.compute_stats(rows_in)
    stats["agreement_rate"] = 0.95
    ok, rows = lc.evaluate_gate(stats, projected_cost=10.0)
    assert ok is False
    assert rows[1]["pass"] is False


def test_gate_fails_on_cost_ceiling():
    stats = lc.compute_stats(_rows_for_stats())
    stats["agreement_rate"] = 0.90
    ok, rows = lc.evaluate_gate(stats, projected_cost=75.0)
    assert ok is False
    assert rows[2]["pass"] is False


def test_project_full_cost_scales_per_turn():
    # per-turn 0.005 over 6389 turns -> ~31.9
    assert lc.project_full_cost(0.005, 6389, 0.1) == pytest.approx(31.945, abs=0.01)


# --------------------------------------------------------------------------- #
# 10. Rubric file + sha256 recorded consistently
# --------------------------------------------------------------------------- #


def test_rubric_file_exists_and_hashes_stably():
    assert lc.RUBRIC_PATH.is_file()
    h1 = lc._sha256(lc.RUBRIC_PATH)
    h2 = lc._sha256(lc.RUBRIC_PATH)
    assert h1 == h2 and len(h1) == 64


def test_meta_records_model_pin_and_rubric_sha(tmp_path, monkeypatch):
    monkeypatch.setattr(lc, "LABELS_PATH", tmp_path / "labels.jsonl")
    monkeypatch.setattr(lc, "META_PATH", tmp_path / "labels_meta.json")
    stats = lc.compute_stats(_rows_for_stats())
    rubric_sha = lc._sha256(lc.RUBRIC_PATH)
    meta = lc.write_meta(stats, {}, mode="dry_run", corpus_size=6389, rubric_sha=rubric_sha)
    assert meta["label_model"] == "claude-opus-4.8"
    assert meta["label_provider"] == "opencap"
    assert meta["labeler_pin"] == lc.LABELER_PIN
    assert meta["label_temperature"] == 0.0
    assert meta["rubric_sha256"] == rubric_sha
    assert meta["prompt_order_a"] == lc.PROMPT_ORDER_A
    # Round-trips as JSON on disk.
    on_disk = json.loads((tmp_path / "labels_meta.json").read_text())
    assert on_disk["rubric_sha256"] == rubric_sha


# --------------------------------------------------------------------------- #
# 11. Provider pin: cache namespacing + token-based cost accounting
# --------------------------------------------------------------------------- #


def test_cache_path_is_namespaced_by_labeler_pin():
    # The on-disk cache filename encodes the labeler pin so a provider/model
    # switch can never reuse another pin's verdicts.
    assert lc.LABELER_PIN in ("opencap:claude-opus-4.8@t0.0",)
    assert "opencap" in lc.CACHE_PATH.name and "claude_opus" in lc.CACHE_PATH.name


def test_label_endpoint_read_from_env_no_hardcoded_host(monkeypatch):
    # The OpenCAP base URL is not hardcoded: it comes from OPENCAP_BASE_URL and
    # the module source carries no gateway host literal.
    monkeypatch.setenv("OPENCAP_BASE_URL", "https://example-gw.test")
    assert lc.resolve_label_endpoint() == (
        "https://example-gw.test/api/inference/v1/chat/completions"
    )
    # Trailing slashes are tolerated.
    monkeypatch.setenv("OPENCAP_BASE_URL", "https://example-gw.test/")
    assert lc.resolve_label_endpoint().startswith("https://example-gw.test/api/")
    # No hardcoded host string in the module source.
    src = _MODULE_PATH.read_text(encoding="utf-8")
    assert "capminal" not in src


def test_label_endpoint_errors_when_env_unset(monkeypatch):
    monkeypatch.delenv("OPENCAP_BASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="OPENCAP_BASE_URL"):
        lc.resolve_label_endpoint()


def test_usage_block_uses_token_based_usd_and_records_diem():
    # A stand-in OpenCAP client with only diem populated (usd=0), exercising the
    # token-count USD estimate that the gate relies on.
    client = lc.OpenCAPLabelClient.__new__(lc.OpenCAPLabelClient)
    client.calls = 10
    client.prompt_tokens = 200_000  # -> $1.00 at $5/MTok
    client.completion_tokens = 40_000  # -> $1.00 at $25/MTok
    client.reported_cost_usd = 0.0
    client.reported_cost_diem = 0.5
    usage = lc._usage_block(client, labeled=100)
    assert usage["provider"] == "opencap"
    assert usage["token_based_cost_this_run_usd"] == 2.0  # 1.00 + 1.00
    assert usage["measured_cost_per_turn_usd"] == 0.02  # 2.0 / 100
    assert usage["gateway_reported_cost_this_run_usd"] == 0.0
    assert usage["gateway_reported_cost_this_run_diem"] == 0.5


# --------------------------------------------------------------------------- #
# 12. Run durability: skip-and-log vs fatal-and-stop
# --------------------------------------------------------------------------- #


class _RaisingClient:
    """Raises a chosen exception on the FIRST turn's pass A, succeeds after.

    Used to prove one bad turn does not crash the whole pool."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self._fired = False
        self._lock = threading.Lock()

    def complete(self, system: str, user: str) -> tuple[str, Any]:
        with self._lock:
            if not self._fired and lc.PROMPT_ORDER_A in system:
                self._fired = True
                raise self._exc
        return '{"label": "R1", "why": "ok"}', {}


def test_label_many_skips_unexpected_exception_not_crashes(tmp_path, monkeypatch):
    _fresh_cache_path(tmp_path, monkeypatch)
    turns = [_turn(f"t{i}", "train", "coding") for i in range(5)]
    client = _RaisingClient(ValueError("transient parse blowup"))
    rows, dropped, skipped = lc.label_many(turns, client, {}, workers=2)
    # One turn skipped (the raiser), the rest labeled — run did NOT crash.
    assert len(skipped) == 1
    assert len(rows) == 4
    assert dropped == 0


def test_label_many_propagates_fatal_runtime_error(tmp_path, monkeypatch):
    # A fatal RuntimeError (auth/quota 402-style) must stop the whole run.
    _fresh_cache_path(tmp_path, monkeypatch)
    turns = [_turn(f"t{i}", "train", "coding") for i in range(5)]
    client = _RaisingClient(RuntimeError("OpenCAP rejected (status 402): quota"))
    with pytest.raises(RuntimeError, match="402"):
        lc.label_many(turns, client, {}, workers=2)


# --------------------------------------------------------------------------- #
# 13. WAF-403 handling in the real OpenCAP client (mocked httpx transport)
# --------------------------------------------------------------------------- #


def _opencap_client_with_transport(handler, monkeypatch):
    """Build a real OpenCAPLabelClient but back it with an httpx MockTransport
    and zero backoff sleeps (so the retry path runs instantly in tests)."""
    import httpx

    monkeypatch.setattr(lc.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(lc.random, "uniform", lambda *_a, **_k: 0.0)
    client = lc.OpenCAPLabelClient.__new__(lc.OpenCAPLabelClient)
    # Endpoint host is not hardcoded (resolved from OPENCAP_BASE_URL in prod); the
    # MockTransport intercepts regardless of URL, so any absolute URL works here.
    client._endpoint = "https://opencap.test/api/inference/v1/chat/completions"
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    client._headers = {"Authorization": "Bearer test", "Content-Type": "application/json"}
    client._usage_lock = threading.Lock()
    client.prompt_tokens = 0
    client.completion_tokens = 0
    client.reported_cost_usd = 0.0
    client.reported_cost_diem = 0.0
    client.calls = 0
    client._waf_403_streak = 0
    return client


def _ok_response():
    import httpx

    body = {
        "choices": [{"message": {"content": '{"label": "R2", "why": "ok"}'}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        "cost": {"usd": 0, "diem": 0.01},
    }
    return httpx.Response(200, json=body)


def test_html_403_is_transient_then_succeeds(monkeypatch):
    # A WAF/edge block page (403 with an HTML body) must be retried, not treated
    # as a fatal auth error. Two block pages then a 200 -> the call succeeds.
    import httpx

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(
                403, text="<!DOCTYPE html><html><body>Blocked by WAF</body></html>"
            )
        return _ok_response()

    client = _opencap_client_with_transport(handler, monkeypatch)
    content, _usage = client.complete("sys", "user")
    assert lc.parse_label(content) == ("R2", "ok")
    assert calls["n"] == 3  # two blocks retried, third succeeded
    assert client._waf_403_streak == 0  # streak reset on success


def test_html_403_fuse_aborts_after_20_consecutive(monkeypatch):
    # A hard, persistent WAF block must trip the 20-consecutive fuse and abort
    # (fatal RuntimeError) rather than spinning forever. The streak is instance
    # state that persists ACROSS complete() calls (each call retries up to 5
    # attempts, then returns empty), so a sustained block trips the fuse within
    # a handful of calls — proving the run cannot spin indefinitely.
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="<html>blocked</html>")

    client = _opencap_client_with_transport(handler, monkeypatch)
    with pytest.raises(RuntimeError, match="WAF block"):
        for _ in range(10):  # 10 calls x up to 5 blocks each >> 20-streak fuse
            client.complete("sys", "user")
    assert client._waf_403_streak >= 20


def test_json_403_stays_fatal(monkeypatch):
    # A genuine auth/quota 403 returns JSON (not an HTML block page) and must
    # remain fatal — never silently retried.
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden: quota exceeded"})

    client = _opencap_client_with_transport(handler, monkeypatch)
    with pytest.raises(RuntimeError, match="403"):
        client.complete("sys", "user")


# --------------------------------------------------------------------------- #
# R3-uplift supplement mode (--input): load candidates + §6.2 supplement contract
# --------------------------------------------------------------------------- #


def _find_split_cids(want: str, n: int = 3) -> list[str]:
    """Find n conversation ids that the frozen partition maps to ``want``."""
    out: list[str] = []
    i = 0
    while len(out) < n:
        cid = f"supp-{want}-{i}"
        if lc.assign_split(cid) == want:
            out.append(cid)
        i += 1
    return out


def test_assign_split_imported_matches_sampler():
    # The supplement mode reuses the T5 frozen partition, not a copy.
    for cid in ("a", "b", "conv-1", "deadbeef"):
        assert lc.assign_split(cid) in {"train", "val", "test"}


def test_load_candidates_rederives_split_from_frozen_partition(tmp_path):
    train_cid = _find_split_cids("train", 1)[0]
    path = tmp_path / "cands.jsonl"
    # File claims split=test, but the frozen partition says train -> train wins.
    path.write_text(
        json.dumps(
            {
                "turn_id": "x1",
                "conversation_id": train_cid,
                "text": "design a migration",
                "category": "coding",
                "direction": "r3_like",
                "split": "test",  # bogus, must be ignored
            }
        )
        + "\n"
    )
    rows = lc.load_candidates(path)
    assert len(rows) == 1
    assert rows[0]["split"] == "train"  # re-derived, not the file's "test"
    assert rows[0]["turn_id"] == "x1"


def test_load_candidates_skips_rows_missing_required_fields(tmp_path):
    path = tmp_path / "cands.jsonl"
    path.write_text(
        json.dumps({"turn_id": "ok", "conversation_id": "c", "text": "hi"})
        + "\n"
        + json.dumps({"turn_id": "no_text", "conversation_id": "c"})  # missing text
        + "\n"
        + "not json at all\n"
    )
    rows = lc.load_candidates(path)
    assert [r["turn_id"] for r in rows] == ["ok"]


def test_apply_supplement_contract_keeps_train_discards_val_test():
    train_cids = _find_split_cids("train", 2)
    val_cids = _find_split_cids("val", 3)
    test_cids = _find_split_cids("test", 4)
    rows = (
        [
            {"turn_id": f"tr{i}", "split": "train", "conversation_id": c}
            for i, c in enumerate(train_cids)
        ]
        + [
            {"turn_id": f"va{i}", "split": "val", "conversation_id": c}
            for i, c in enumerate(val_cids)
        ]
        + [
            {"turn_id": f"te{i}", "split": "test", "conversation_id": c}
            for i, c in enumerate(test_cids)
        ]
    )
    kept, counts = lc.apply_supplement_contract(rows)
    assert {r["turn_id"] for r in kept} == {"tr0", "tr1"}
    assert all(r["split"] == "train" for r in kept)
    assert counts == {"kept_train": 2, "discarded_val": 3, "discarded_test": 4}


def test_run_supplement_writes_overlay_not_labels(tmp_path, monkeypatch):
    _fresh_cache_path(tmp_path, monkeypatch)
    # Redirect overlay outputs into tmp so nothing touches the real data dir.
    monkeypatch.setattr(lc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(lc, "SUPPLEMENT_LABELS_PATH", tmp_path / "labels_supplement.jsonl")
    monkeypatch.setattr(lc, "SUPPLEMENT_META_PATH", tmp_path / "labels_supplement_meta.json")
    monkeypatch.setattr(lc, "LABELS_PATH", tmp_path / "labels.jsonl")  # must stay untouched

    train_cid = _find_split_cids("train", 1)[0]
    val_cid = _find_split_cids("val", 1)[0]
    inp = tmp_path / "cands.jsonl"
    inp.write_text(
        json.dumps(
            {
                "turn_id": "t1",
                "conversation_id": train_cid,
                "text": "design X",
                "category": "coding",
            }
        )
        + "\n"
        + json.dumps(
            {"turn_id": "t2", "conversation_id": val_cid, "text": "design Y", "category": "coding"}
        )
        + "\n"
    )
    client = ScriptedClient(
        {
            "A": ['{"label": "R3", "why": "arch"}', '{"label": "R3", "why": "arch"}'],
            "B": ['{"label": "R3", "why": "arch"}', '{"label": "R3", "why": "arch"}'],
        }
    )
    meta = lc.run_supplement(input_path=inp, client=client, workers=2)

    # Overlay file written; original labels.jsonl NOT created.
    assert (tmp_path / "labels_supplement.jsonl").is_file()
    assert not (tmp_path / "labels.jsonl").is_file()
    # Both rows labeled; contract keeps the train one, discards the val one.
    assert meta["mode"] == "supplement"
    assert meta["supplement_contract"]["kept_train"] == 1
    assert meta["supplement_contract"]["discarded_val"] == 1
    assert meta["supplement_contract"]["discarded_test"] == 0
    # The overlay carries ALL labeled rows (both splits) for auditability.
    overlay_text = (tmp_path / "labels_supplement.jsonl").read_text()
    overlay = [json.loads(line) for line in overlay_text.splitlines() if line]
    assert {r["turn_id"] for r in overlay} == {"t1", "t2"}


def test_run_supplement_reuses_cache(tmp_path, monkeypatch):
    _fresh_cache_path(tmp_path, monkeypatch)
    monkeypatch.setattr(lc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(lc, "SUPPLEMENT_LABELS_PATH", tmp_path / "labels_supplement.jsonl")
    monkeypatch.setattr(lc, "SUPPLEMENT_META_PATH", tmp_path / "labels_supplement_meta.json")
    train_cid = _find_split_cids("train", 1)[0]
    inp = tmp_path / "cands.jsonl"
    inp.write_text(
        json.dumps({"turn_id": "t1", "conversation_id": train_cid, "text": "design X"}) + "\n"
    )
    # Pre-seed the cache so no client call is needed (both passes agree).
    cache_path = tmp_path / "label_cache.jsonl"
    with cache_path.open("w") as fh:
        fh.write(json.dumps({"turn_id": "t1", "pass": "A", "label": "R3", "why": ""}) + "\n")
        fh.write(json.dumps({"turn_id": "t1", "pass": "B", "label": "R3", "why": ""}) + "\n")

    class _Boom:
        def complete(self, system, user):
            raise AssertionError("client must not be called when cache hits")

    meta = lc.run_supplement(input_path=inp, client=_Boom(), workers=1)
    assert meta["supplement_contract"]["kept_train"] == 1


def test_html_403_streak_resets_between_successes(monkeypatch):
    # Intermittent block pages interspersed with successes must not accumulate
    # toward the fuse: streak resets each success.
    import httpx

    seq = iter([403, 200, 403, 403, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        code = next(seq, 200)
        if code == 403:
            return httpx.Response(403, text="<html>blocked</html>")
        return _ok_response()

    client = _opencap_client_with_transport(handler, monkeypatch)
    # First complete(): 403 then 200 -> succeeds, streak reset to 0.
    client.complete("sys", "user")
    assert client._waf_403_streak == 0
    # Second complete(): 403, 403 then 200 -> succeeds; streak never hit fuse.
    client.complete("sys", "user")
    assert client._waf_403_streak == 0
