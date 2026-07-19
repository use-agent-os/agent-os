#!/usr/bin/env python
"""T9 — evaluation gate: Pilot (pilot-v1) vs v4_phase3 on FINAL engine decisions.

This is the ship-or-stop gate for the trained Pilot artifact (Pilot router spec
Rev 4, §6.4/§6.6). It is honest measurement only: it fills every §6.4 gate row
for BOTH routers on the same held-out test split — opened here for the first
time — and writes the verdict exactly as measured.

**Evaluation contract (binding).** Both routers are scored on the identical test
split (``split=="test"`` rows of ``corpus.jsonl`` ⋈ ``labels.jsonl`` on
``turn_id``). Each turn is routed through the FULL ``apply_agentos_router`` step
— guards enabled, per-conversation history replayed in ``turn_id`` order — never
by calling ``_finalize_decision`` directly. The strategy is injected through the
engine's own ``_get_strategy`` seam (the same seam the engine tests monkeypatch),
so the replay is faithful to production dispatch. The scored decision is the
engine's FINAL tier (``routing_extra["final_route_class"]`` / ``routed_tier``
post-guards), mapped back to the 4-class ``R0..R3`` space.

* Pilot strategy: the T7 staging artifact
  (``scripts/pilot_router/artifacts/pilot_v1/``), the production ``_MiniLMEncoder``,
  thresholds at the defaults (safety_net 0.5 / confidence 0.5).
* v4 strategy: the shipped bundle
  (``src/agentos/agentos_router/models/v4.2_phase3_inference/``) exactly as
  dispatched today.

**Turn ordering.** Within a conversation, ``turn_id`` (WildChat's
``turn_identifier``, a per-turn ordinal that ascends with dialogue position) is
the ordering key — the committed corpus was re-sorted by category so neither
file order nor conversation-contiguity survives, but ascending ``turn_id`` still
reconstructs the dialogue order (verified against discourse cues). History is
accumulated over the SAMPLED test turns of each conversation only (other turns
were never sampled/labeled); this is a documented limitation, not a leak.

Outputs (committed): ``eval_report.md`` (§6.4 table + verdict + confusion
matrices) and ``eval_meta.json``. Raw per-turn rows are written under
``scripts/pilot_router/data/`` (gitignored).

Usage::

    python -m scripts.pilot_router.evaluate                 # full replay both routers
    python -m scripts.pilot_router.evaluate --oracle        # + quality-oracle subset
    python -m scripts.pilot_router.evaluate --limit 40      # smoke: first 40 turns
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from scripts.pilot_router import eval_lib

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(__file__).resolve().parent / "data"
CORPUS_PATH = DATA_DIR / "corpus.jsonl"
LABELS_PATH = DATA_DIR / "labels.jsonl"
PILOT_ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts" / "pilot_v1"
REPORT_PATH = Path(__file__).resolve().parent / "eval_report.md"
META_PATH = Path(__file__).resolve().parent / "eval_meta.json"
RAW_ROWS_PATH = DATA_DIR / "eval_raw_rows.jsonl"  # gitignored (under data/)

TIER_TO_CLASS = {"c0": "R0", "c1": "R1", "c2": "R2", "c3": "R3"}
CLASS_TO_TIER = {v: k for k, v in TIER_TO_CLASS.items()}


# --------------------------------------------------------------------------- #
# Data loading + turn ordering (pure — unit-tested offline)
# --------------------------------------------------------------------------- #


def order_turns(rows: list[dict]) -> list[dict]:
    """Order a conversation's turns by ascending ``turn_id`` (dialogue order).

    ``turn_id`` is WildChat's integer ``turn_identifier``; a numeric sort
    reconstructs the true within-conversation sequence. A non-numeric id (never
    present in the real corpus, but tolerated) sorts lexicographically last.
    """

    def key(row: dict) -> tuple[int, int, str]:
        tid = str(row.get("turn_id", ""))
        return (0, int(tid), "") if tid.isdigit() else (1, 0, tid)

    return sorted(rows, key=key)


def load_test_split() -> list[dict]:
    """Inner-join corpus ⋈ labels on ``turn_id`` for ``split=="test"`` rows.

    Returns rows ``{turn_id, conversation_id, text, category, gold, boundary_set}``.
    THIS IS THE FIRST OPENING of the test labels — legitimate at the eval gate.
    """
    labels: dict[str, dict] = {}
    with LABELS_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if rec.get("split") == "test":
                labels[str(rec["turn_id"])] = rec
    out: list[dict] = []
    with CORPUS_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            tid = str(rec["turn_id"])
            lab = labels.get(tid)
            if lab is None:
                continue
            out.append(
                {
                    "turn_id": tid,
                    "conversation_id": str(rec.get("conversation_id", "")),
                    "text": rec["text"],
                    "category": rec.get("category"),
                    "gold": lab["label"],
                    "boundary_set": bool(lab.get("boundary_set", False)),
                }
            )
    return out


def group_by_conversation(rows: list[dict]) -> list[list[dict]]:
    """Group rows by ``conversation_id`` and order each group by ``turn_id``.

    Returns a list of conversations (each a turn-ordered row list), the outer
    list ordered by the conversation's first ``turn_id`` for a stable replay.
    """
    by_conv: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_conv[row["conversation_id"]].append(row)
    convs = [order_turns(group) for group in by_conv.values()]
    convs.sort(key=lambda g: order_turns(g)[0]["turn_id"])
    return convs


# --------------------------------------------------------------------------- #
# Replay through the REAL apply_agentos_router step
# --------------------------------------------------------------------------- #


def _make_context(text: str, session_key: str) -> Any:
    from agentos.engine.pipeline import TurnContext
    from agentos.gateway.config import GatewayConfig

    config = GatewayConfig()
    config.agentos_router.enabled = True
    config.agentos_router.rollout_phase = "full"
    # Defaults: safety_net 0.5 (pilot sub-table), confidence 0.5.
    return TurnContext(
        message=text,
        session_key=session_key,
        config=config,
        provider=None,
        model=config.llm.model,
        tool_defs=[],
        system_prompt="system",
    )


def _reset_engine_state() -> None:
    from agentos.engine.steps import agentos_router as step

    step._history_store.clear()
    step._strategy = None
    step._strategy_key = None


async def replay_conversation(
    rows: list[dict],
    strategy: Any,
    session_key: str,
) -> list[dict]:
    """Replay one conversation's turns (turn-ordered) through the full step.

    Injects ``strategy`` through the engine's ``_get_strategy`` seam and resets
    per-session history before the first turn, so each conversation starts cold
    (history accumulates only within the conversation). Returns one result dict
    per turn with the engine's FINAL decision plus the raw (pre-guard) route.
    """
    from agentos.engine.steps import agentos_router as step

    step._get_strategy = lambda _config, _llm_cfg=None: strategy  # type: ignore[assignment]
    step._history_store.evict(session_key)

    results: list[dict] = []
    for row in order_turns(rows):
        ctx = _make_context(row["text"], session_key)
        routed = await step.apply_agentos_router(ctx)
        extra = routed.metadata.get("routing_extra") or {}
        final_tier = routed.metadata.get("routed_tier")
        final_route_class = extra.get("final_route_class") or TIER_TO_CLASS.get(
            str(final_tier), None
        )
        base_route_class = extra.get("route_class")
        probs = extra.get("probabilities")
        prob_vec = (
            [float(probs.get(c, 0.0)) for c in eval_lib.CLASSES]
            if isinstance(probs, dict)
            else None
        )
        results.append(
            {
                "turn_id": row["turn_id"],
                "conversation_id": row.get("conversation_id"),
                "text": row["text"],
                "gold": row.get("gold"),
                "boundary_set": row.get("boundary_set", False),
                "category": row.get("category"),
                "routing_source": routed.metadata.get("routing_source"),
                "base_route_class": base_route_class,
                "base_tier": extra.get("base_tier"),
                "final_tier": final_tier,
                "final_route_class": final_route_class,
                "confidence": routed.metadata.get("routing_confidence"),
                "confidence_gate_applied": extra.get("confidence_gate_applied"),
                "safety_net_applied": extra.get("safety_net_applied"),
                "anti_downgrade_applied": extra.get("anti_downgrade_applied"),
                "probabilities": prob_vec,
            }
        )
    return results


async def replay_all(
    conversations: list[list[dict]],
    strategy: Any,
    *,
    router_name: str,
    progress_every: int = 50,
) -> list[dict]:
    """Replay every conversation for one router, resetting engine state first.

    Restores the engine's real ``_get_strategy`` on exit so a shared process
    (e.g. pytest importing this module) is never left with the injected
    strategy leaking into later work.
    """
    from agentos.engine.steps import agentos_router as step

    original_get_strategy = step._get_strategy
    _reset_engine_state()
    all_rows: list[dict] = []
    done = 0
    t0 = time.time()
    try:
        for i, conv in enumerate(conversations):
            session_key = f"eval-{router_name}-conv-{i}"
            all_rows.extend(await replay_conversation(conv, strategy, session_key))
            done += len(conv)
            if progress_every and (i + 1) % progress_every == 0:
                rate = done / max(time.time() - t0, 1e-6)
                print(
                    f"  [{router_name}] {i + 1}/{len(conversations)} convs, "
                    f"{done} turns ({rate:.1f} turns/s)"
                )
    finally:
        step._get_strategy = original_get_strategy  # type: ignore[assignment]
        _reset_engine_state()
    return all_rows


# --------------------------------------------------------------------------- #
# Strategy builders (production-faithful)
# --------------------------------------------------------------------------- #


GOLDEN_PATH = REPO_ROOT / "tests" / "test_agentos_router" / "data" / "pilot_golden.jsonl"
GOLDEN_FLOOR = 0.80


async def score_golden_set(pilot_strategy: Any) -> dict | None:
    """Score the committed golden set through the full step (report companion).

    Mirrors the CI gate (``tests/.../test_pilot_golden.py``): each golden row is
    routed through ``apply_agentos_router`` in an isolated single-turn session
    and scored against its gold class. Report-only here; the numeric ≥0.80 floor
    is enforced by the CI test once the artifact ships.
    """
    if not GOLDEN_PATH.exists():
        return None
    from agentos.engine.steps import agentos_router as step

    rows: list[dict] = []
    for line in GOLDEN_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if "_meta" in rec:
            continue
        rows.append(rec)
    if not rows:
        return None

    original_get_strategy = step._get_strategy
    _reset_engine_state()
    step._get_strategy = lambda _c, _l=None: pilot_strategy  # type: ignore[assignment]
    correct = 0
    per_hit: dict[str, int] = defaultdict(int)
    per_tot: dict[str, int] = defaultdict(int)
    try:
        for i, row in enumerate(rows):
            step._history_store.evict(f"golden-{i}")
            ctx = _make_context(row["text"], f"golden-{i}")
            routed = await step.apply_agentos_router(ctx)
            extra = routed.metadata.get("routing_extra") or {}
            pred = extra.get("final_route_class") or TIER_TO_CLASS.get(
                str(routed.metadata.get("routed_tier")), "R1"
            )
            gold = row["gold_class"]
            per_tot[gold] += 1
            if pred == gold:
                correct += 1
                per_hit[gold] += 1
    finally:
        step._get_strategy = original_get_strategy  # type: ignore[assignment]
        _reset_engine_state()
    acc = correct / len(rows)
    return {
        "n": len(rows),
        "accuracy": acc,
        "floor": GOLDEN_FLOOR,
        "pass": acc >= GOLDEN_FLOOR,
        "per_class_recall": {
            c: (per_hit[c] / per_tot[c] if per_tot[c] else float("nan")) for c in eval_lib.CLASSES
        },
    }


def build_pilot_strategy() -> Any:
    from agentos.agentos_router.pilot import PilotStrategy

    strat = PilotStrategy(
        artifact_dir=str(PILOT_ARTIFACT_DIR),
        safety_net_threshold=0.5,
        confidence_threshold=0.5,
    )
    if not strat._available:
        raise RuntimeError(
            f"Pilot artifact not loadable at {PILOT_ARTIFACT_DIR} — cannot run the gate"
        )
    return strat


def build_v4_strategy() -> Any:
    from agentos.agentos_router.v4_phase3 import V4Phase3Strategy

    strat = V4Phase3Strategy(confidence_threshold=0.5)
    if not strat._available:
        raise RuntimeError("v4 bundle not loadable — cannot run the gate")
    return strat


# --------------------------------------------------------------------------- #
# Gate evaluation
# --------------------------------------------------------------------------- #


def _metrics_for_rows(rows: list[dict]) -> eval_lib.RouterMetrics:
    gold = eval_lib.to_indices([r["gold"] for r in rows])
    pred = eval_lib.to_indices([r["final_route_class"] or "R1" for r in rows])
    probs = [r.get("probabilities") or [] for r in rows]
    return eval_lib.compute_router_metrics(gold, pred, probs)


def _fmt(x: float) -> str:
    if x != x:  # NaN
        return "n/a"
    return f"{x:.4f}"


def _fmt_pct(x: float) -> str:
    if x != x:
        return "n/a"
    return f"{x * 100:.2f}%"


def evaluate_gate(pilot_rows: list[dict], v4_rows: list[dict]) -> dict:
    """Compute both routers' metrics + every §6.4 gate verdict.

    ``pilot_rows`` / ``v4_rows`` are aligned per turn (same test set, same
    order). Boundary-set rows are reported separately (report-only); the gate
    metrics use the FULL test split (boundary rows included) per the contract —
    the gate scores the whole held-out split, boundary rows are additionally
    broken out for context.
    """
    assert len(pilot_rows) == len(v4_rows), "router row counts must match"

    pilot_m = _metrics_for_rows(pilot_rows)
    v4_m = _metrics_for_rows(v4_rows)

    gold = eval_lib.to_indices([r["gold"] for r in pilot_rows])
    pilot_pred = eval_lib.to_indices([r["final_route_class"] or "R1" for r in pilot_rows])
    v4_pred = eval_lib.to_indices([r["final_route_class"] or "R1" for r in v4_rows])
    boot = eval_lib.bootstrap_accuracy_delta(gold, pilot_pred, v4_pred)

    # Boundary-set breakout (report-only).
    b_idx = [i for i, r in enumerate(pilot_rows) if r.get("boundary_set")]
    boundary = None
    if b_idx:
        bp_gold = [gold[i] for i in b_idx]
        bp_pilot = [pilot_pred[i] for i in b_idx]
        bp_v4 = [v4_pred[i] for i in b_idx]
        boundary = {
            "n": len(b_idx),
            "pilot_accuracy": eval_lib.accuracy(bp_gold, bp_pilot),
            "v4_accuracy": eval_lib.accuracy(bp_gold, bp_v4),
        }

    # --- Gate verdicts (spec §6.4) -------------------------------------- #
    gate: list[dict] = []

    def add(metric: str, pilot_val: str, v4_val: str, passed: bool, note: str = "") -> None:
        gate.append(
            {
                "metric": metric,
                "pilot": pilot_val,
                "v4": v4_val,
                "pass": passed,
                "note": note,
            }
        )

    # Accuracy: Pilot >= v4 AND >= 0.70 absolute.
    acc_pass = (pilot_m.accuracy >= v4_m.accuracy) and (pilot_m.accuracy >= 0.70)
    add(
        "Accuracy (4-class, final decision)",
        _fmt(pilot_m.accuracy),
        _fmt(v4_m.accuracy),
        acc_pass,
        "Pilot >= v4 AND >= 0.70",
    )

    # Under-routing: Pilot <= v4.
    ur_pass = pilot_m.under_routing_rate <= v4_m.under_routing_rate
    add(
        "Under-routing rate (pred < gold)",
        _fmt(pilot_m.under_routing_rate),
        _fmt(v4_m.under_routing_rate),
        ur_pass,
        "Pilot <= v4",
    )

    # Severity-weighted under-routing: Pilot <= v4.
    sw_pass = pilot_m.severity_weighted_under_routing <= v4_m.severity_weighted_under_routing
    add(
        "Severity-weighted under-routing",
        _fmt(pilot_m.severity_weighted_under_routing),
        _fmt(v4_m.severity_weighted_under_routing),
        sw_pass,
        "Pilot <= v4",
    )

    # R2 recall >= 0.60 absolute (Pilot).
    r2 = pilot_m.per_class_recall["R2"]
    r2_pass = (r2 == r2) and r2 >= 0.60
    add(
        "R2 recall (Pilot)",
        _fmt(r2),
        _fmt(v4_m.per_class_recall["R2"]),
        r2_pass,
        "Pilot >= 0.60 absolute",
    )

    # R3 recall >= 0.60 absolute (Pilot).
    r3 = pilot_m.per_class_recall["R3"]
    r3_pass = (r3 == r3) and r3 >= 0.60
    add(
        "R3 recall (Pilot)",
        _fmt(r3),
        _fmt(v4_m.per_class_recall["R3"]),
        r3_pass,
        "Pilot >= 0.60 absolute",
    )

    # Over-routing: Pilot <= v4 + 5pp.
    or_pass = pilot_m.over_routing_rate <= v4_m.over_routing_rate + 0.05
    add(
        "Over-routing rate (pred > gold)",
        _fmt(pilot_m.over_routing_rate),
        _fmt(v4_m.over_routing_rate),
        or_pass,
        "Pilot <= v4 + 5pp",
    )

    # Statistical validity: CI lower bound > -1pp.
    ci_pass = boot.ci_low > -0.01
    add(
        "Accuracy-delta 95% bootstrap CI",
        f"delta={_fmt(boot.delta_point)} CI[{_fmt(boot.ci_low)}, {_fmt(boot.ci_high)}]",
        "-",
        ci_pass,
        "CI lower bound > -1pp",
    )

    overall_pass = all(g["pass"] for g in gate)

    return {
        "pilot": pilot_m.to_dict(),
        "v4": v4_m.to_dict(),
        "bootstrap": boot.to_dict(),
        "boundary_set": boundary,
        "gate": gate,
        "gate_pass": overall_pass,
        "verdict": "SHIP" if overall_pass else "STOP",
    }


# --------------------------------------------------------------------------- #
# Quality-oracle subset (report-only, network — gated behind --oracle)
# --------------------------------------------------------------------------- #

# OpenCAP is an OpenAI-compatible gateway; its base URL is NOT hardcoded. The
# oracle endpoint is derived from OPENCAP_BASE_URL (recorded in private ops
# notes), the same env the labeler uses. ``_resolve_oracle_endpoint`` raises a
# clear error when it is unset (mirroring the missing-key posture).
OPENCAP_BASE_URL_ENV = "OPENCAP_BASE_URL"
_ORACLE_ENDPOINT_SUFFIX = "/api/inference/v1/chat/completions"


def _resolve_oracle_endpoint() -> str:
    base = os.environ.get(OPENCAP_BASE_URL_ENV, "").strip().rstrip("/")
    if not base:
        raise RuntimeError(
            f"{OPENCAP_BASE_URL_ENV} is required for the OpenCAP quality-oracle "
            "subset (the gateway base URL is not hardcoded; set it from your "
            "private ops notes, e.g. OPENCAP_BASE_URL=https://<host>)"
        )
    return f"{base}{_ORACLE_ENDPOINT_SUFFIX}"


# OpenCAP-served model ids per tier. The openrouter-prefixed ids in
# agentos.toml.example do not exist on OpenCAP; these bare ids match what the
# gateway serves (the labeler used bare "claude-opus-4.8"). A tier whose id
# OpenCAP rejects is documented in the report as a substitution/skip.
ORACLE_TIER_MODELS = {
    "R0": "deepseek-v4-flash",
    "R1": "minimax-m3",
    "R2": "glm-5.2",
    "R3": "claude-opus-4.8",
}
ORACLE_JUDGE_MODEL = "claude-opus-4.8"


def _load_opencap_key() -> str | None:
    key = os.environ.get("OPENCAP_API_KEY")
    if key:
        return key
    env_path = Path.home() / ".agentos" / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("OPENCAP_API_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return None


class _OpenCAPClient:
    """WAF-tolerant OpenCAP chat client (reuses label_corpus.py's 403-HTML pattern)."""

    def __init__(self, api_key: str) -> None:
        import httpx

        # Endpoint from OPENCAP_BASE_URL (no hardcoded host); raises if unset.
        self._endpoint = _resolve_oracle_endpoint()
        self._client = httpx.Client(timeout=90.0)
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self.cost_usd = 0.0
        self.cost_diem = 0.0
        self.calls = 0

    def complete(
        self, model: str, messages: list[dict], *, temperature: float = 0.0, max_tokens: int = 1200
    ) -> str:
        import random
        import time as _t

        payload = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            "messages": messages,
        }
        waf_streak = 0
        for attempt in range(5):
            try:
                resp = self._client.post(self._endpoint, headers=self._headers, json=payload)
                if resp.status_code == 403 and resp.text.lstrip()[:15].lower().startswith(
                    ("<!doctype", "<html")
                ):
                    waf_streak += 1
                    if waf_streak >= 10:
                        raise RuntimeError("OpenCAP WAF block persisted (10x)")
                    _t.sleep(15 + random.uniform(0, 15))
                    continue
                if resp.status_code in (401, 402, 403, 404):
                    raise RuntimeError(
                        f"OpenCAP rejected model={model} (status {resp.status_code}): "
                        f"{resp.text[:200]}"
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    _t.sleep(min(2 ** (attempt + 1), 30) + random.uniform(0, 5))
                    continue
                resp.raise_for_status()
                data = resp.json()
                cost = data.get("cost") or {}
                self.cost_usd += float(cost.get("usd", 0.0) or 0.0)
                self.cost_diem += float(cost.get("diem", 0.0) or 0.0)
                self.calls += 1
                return str(data["choices"][0]["message"]["content"])
            except RuntimeError:
                raise
            except Exception:
                if attempt == 4:
                    return ""
                _t.sleep(min(2 ** (attempt + 1), 30) + random.uniform(0, 5))
        return ""

    def close(self) -> None:
        self._client.close()


def _stratified_oracle_sample(rows: list[dict], per_class: int, seed: int = 42) -> list[dict]:
    import random

    rng = random.Random(seed)
    by_gold: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_gold[r["gold"]].append(r)
    picked: list[dict] = []
    for cls in eval_lib.CLASSES:
        pool = by_gold.get(cls, [])
        rng.shuffle(pool)
        picked.extend(pool[:per_class])
    return picked


_JUDGE_SYSTEM = (
    "You are a strict answer-quality grader. Given a user prompt and a candidate "
    "answer produced by some model, decide whether the answer is ACCEPTABLE: "
    "correct, complete enough, and genuinely useful for the user's request. "
    'Respond with a single JSON object: {"acceptable": true|false}. No prose.'
)


def run_oracle_subset(
    pilot_rows: list[dict],
    *,
    per_class: int,
    budget_usd: float,
) -> dict:
    """Report-only cheapest-acceptable-tier study over a stratified subset.

    For each sampled turn: generate an answer at the Pilot-predicted tier's model
    and at one tier lower (via OpenCAP), have the pinned judge grade each
    acceptable, and record the cheapest acceptable tier. Aborts cleanly (with a
    documented reason) if the key is missing, a tier model is rejected, or the
    running cost would exceed ``budget_usd``.
    """
    key = _load_opencap_key()
    if not key:
        return {"status": "skipped", "reason": "OPENCAP_API_KEY not found"}
    if not os.environ.get(OPENCAP_BASE_URL_ENV, "").strip():
        return {"status": "skipped", "reason": f"{OPENCAP_BASE_URL_ENV} not set"}

    sample = _stratified_oracle_sample(pilot_rows, per_class)
    client = _OpenCAPClient(key)
    findings: list[dict] = []
    substitutions: dict[str, str] = {}
    try:
        for i, row in enumerate(sample):
            if i % 10 == 0:
                print(
                    f"  [oracle] {i}/{len(sample)} turns, "
                    f"${client.cost_usd:.3f} / {client.cost_diem:.1f} diem, "
                    f"{client.calls} calls",
                    flush=True,
                )
            # OpenCAP frequently reports cost.usd=0 with only the gateway's own
            # billing unit populated, so trip the budget on EITHER the reported
            # USD or a conservative unit→USD conversion (~7 units ≈ $1, observed
            # on the T6 labeling run). A hard call fuse backstops both in case
            # the gateway reports neither.
            diem_usd = client.cost_diem / 7.0
            if client.cost_usd > budget_usd or diem_usd > budget_usd or client.calls > 1200:
                return {
                    "status": "trimmed",
                    "reason": (
                        f"budget cap reached after {i} turns "
                        f"(${client.cost_usd:.2f} usd / {client.cost_diem:.1f} diem / "
                        f"{client.calls} calls)"
                    ),
                    "n": len(findings),
                    "cheapest_acceptable_agreement": (
                        sum(1 for f in findings if f["cheapest_acceptable"] == f["pilot_pred"])
                        / len(findings)
                        if findings
                        else None
                    ),
                    "tier_models": ORACLE_TIER_MODELS,
                    "substitutions": substitutions,
                    "findings": findings,
                    "cost_usd": client.cost_usd,
                    "cost_diem": client.cost_diem,
                    "calls": client.calls,
                }
            pred_class = row["final_route_class"] or "R1"
            pred_idx = eval_lib.CLASS_TO_INT[pred_class]
            lower_idx = max(0, pred_idx - 1)
            tiers_to_try = sorted({pred_idx, lower_idx})
            grades: dict[str, bool] = {}
            for idx in tiers_to_try:
                cls = eval_lib.CLASSES[idx]
                model = ORACLE_TIER_MODELS[cls]
                answer = client.complete(
                    model,
                    [{"role": "user", "content": row["text"]}],
                    max_tokens=700,
                )
                if not answer:
                    continue
                verdict = client.complete(
                    ORACLE_JUDGE_MODEL,
                    [
                        {"role": "system", "content": _JUDGE_SYSTEM},
                        {
                            "role": "user",
                            "content": f"PROMPT:\n{row['text']}\n\nANSWER:\n{answer}",
                        },
                    ],
                    max_tokens=50,
                )
                acceptable = '"acceptable": true' in verdict.lower() or (
                    '"acceptable":true' in verdict.lower()
                )
                grades[cls] = acceptable
            cheapest_ok = None
            for idx in sorted(tiers_to_try):
                cls = eval_lib.CLASSES[idx]
                if grades.get(cls):
                    cheapest_ok = cls
                    break
            findings.append(
                {
                    "turn_id": row["turn_id"],
                    "gold": row["gold"],
                    "pilot_pred": pred_class,
                    "grades": grades,
                    "cheapest_acceptable": cheapest_ok,
                }
            )
        status = "ok"
        reason = ""
    except RuntimeError as exc:
        status = "skipped"
        reason = str(exc)
    finally:
        client.close()

    # Cheapest-acceptable-tier agreement with the Pilot prediction.
    agree = sum(1 for f in findings if f["cheapest_acceptable"] == f["pilot_pred"])
    return {
        "status": status,
        "reason": reason,
        "n": len(findings),
        "tier_models": ORACLE_TIER_MODELS,
        "substitutions": substitutions,
        "cheapest_acceptable_agreement": (agree / len(findings)) if findings else None,
        "findings": findings,
        "cost_usd": client.cost_usd,
        "cost_diem": client.cost_diem,
        "calls": client.calls,
    }


# --------------------------------------------------------------------------- #
# Report writers
# --------------------------------------------------------------------------- #


def _confusion_md(matrix: list[list[int]]) -> str:
    header = "| gold\\pred | R0 | R1 | R2 | R3 | recall |"
    sep = "|---|---|---|---|---|---|"
    lines = [header, sep]
    for i, cls in enumerate(eval_lib.CLASSES):
        support = sum(matrix[i])
        hit = matrix[i][i]
        rec = f"{hit / support:.3f}" if support else "n/a"
        cells = " | ".join(str(matrix[i][j]) for j in range(4))
        lines.append(f"| **{cls}** | {cells} | {rec} |")
    return "\n".join(lines)


def write_report(result: dict, oracle: dict | None, meta: dict, golden: dict | None) -> None:
    pilot = result["pilot"]
    v4 = result["v4"]
    lines: list[str] = []
    lines.append("# Pilot-v1 vs v4_phase3 — T9 evaluation gate report")
    lines.append("")
    lines.append(
        f"**VERDICT: {result['verdict']}** "
        f"({'all gate rows pass' if result['gate_pass'] else 'one or more gate rows FAIL'})"
    )
    lines.append("")
    lines.append(
        "Honest measurement (spec §6.6): every §6.4 gate row is filled for both "
        "routers exactly as measured on the held-out test split, replayed through "
        "the full `apply_agentos_router` step. A failed gate is a valid, final "
        "STOP outcome — no tuning, no threshold nudging, no re-runs."
    )
    lines.append("")
    lines.append(f"- Test turns scored: **{pilot['n']}** (per router)")
    if result.get("boundary_set"):
        lines.append(f"- Boundary-set turns (report-only): **{result['boundary_set']['n']}**")
    lines.append(f"- Pilot artifact: `{PILOT_ARTIFACT_DIR.relative_to(REPO_ROOT)}`")
    lines.append("- Pilot thresholds: safety_net 0.5 / confidence 0.5 (defaults)")
    lines.append(f"- Fitted temperature (in manifest): {meta.get('pilot_temperature')}")
    lines.append("")

    # Gate table.
    lines.append("## §6.4 gate table")
    lines.append("")
    lines.append("| Metric | Pilot | v4_phase3 | Gate | Verdict |")
    lines.append("|---|---|---|---|---|")
    for g in result["gate"]:
        verdict = "PASS" if g["pass"] else "**FAIL**"
        lines.append(f"| {g['metric']} | {g['pilot']} | {g['v4']} | {g['note']} | {verdict} |")
    lines.append("")

    # Report-only metrics.
    lines.append("## Report-only metrics")
    lines.append("")
    lines.append("| Metric | Pilot | v4_phase3 |")
    lines.append("|---|---|---|")
    lines.append(f"| Macro-F1 | {_fmt(pilot['macro_f1'])} | {_fmt(v4['macro_f1'])} |")
    for cls in eval_lib.CLASSES:
        lines.append(
            f"| Recall {cls} | {_fmt(pilot['per_class_recall'][cls])} "
            f"| {_fmt(v4['per_class_recall'][cls])} |"
        )
    lines.append(f"| ECE (15-bin, calibrated) | {_fmt(pilot['ece'])} | {_fmt(v4['ece'])} |")
    lines.append(f"| NLL | {_fmt(pilot['nll'])} | {_fmt(v4['nll'])} |")
    if result.get("boundary_set"):
        b = result["boundary_set"]
        lines.append(
            f"| Boundary-set accuracy | {_fmt(b['pilot_accuracy'])} | {_fmt(b['v4_accuracy'])} |"
        )
    lines.append("")

    # Confusion matrices.
    lines.append("## Confusion matrices (gold rows × pred cols)")
    lines.append("")
    lines.append("### Pilot")
    lines.append("")
    lines.append(_confusion_md(pilot["confusion_matrix"]))
    lines.append("")
    lines.append("### v4_phase3")
    lines.append("")
    lines.append(_confusion_md(v4["confusion_matrix"]))
    lines.append("")

    # Bootstrap.
    boot = result["bootstrap"]
    lines.append("## Statistical validity")
    lines.append("")
    lines.append(
        f"Paired bootstrap ({boot['n_resamples']} resamples, seed {boot['seed']}) on "
        f"`acc(Pilot) - acc(v4)`: point delta **{_fmt(boot['delta_point'])}**, "
        f"95% CI **[{_fmt(boot['ci_low'])}, {_fmt(boot['ci_high'])}]**. "
        f"Gate (CI lower bound > -1pp): "
        f"{'PASS' if boot['ci_low'] > -0.01 else '**FAIL**'}."
    )
    lines.append("")

    # Oracle.
    lines.append("## Quality-oracle subset (report-only)")
    lines.append("")
    if oracle is None:
        lines.append("Not run in this invocation (pass `--oracle`).")
    elif oracle.get("status") in {"skipped"}:
        lines.append(f"**Skipped** — {oracle.get('reason')}")
    else:
        agree = oracle.get("cheapest_acceptable_agreement")
        lines.append(f"- Status: {oracle.get('status')}")
        lines.append(f"- Turns judged: {oracle.get('n')}")
        lines.append(f"- Tier→model: `{oracle.get('tier_models')}`")
        lines.append(
            "- All four tier models were accepted by OpenCAP (no substitutions or skips)."
            if not oracle.get("substitutions")
            else f"- Model substitutions: `{oracle.get('substitutions')}`"
        )
        lines.append(
            "- Cheapest-acceptable-tier agreement with Pilot prediction: "
            f"{_fmt_pct(agree) if agree is not None else 'n/a'}"
        )
        findings = oracle.get("findings") or []
        neither = sum(1 for f in findings if f.get("cheapest_acceptable") is None)
        cheaper = sum(
            1
            for f in findings
            if f.get("cheapest_acceptable") and f["cheapest_acceptable"] != f["pilot_pred"]
        )
        lines.append(f"- Rows where a cheaper tier than Pilot's pick already sufficed: {cheaper}")
        lines.append(
            f"- Rows where NEITHER tried tier was judged acceptable: {neither} "
            "(the pinned opus judge grades strictly; some reasoning-model answers "
            "also returned empty content under the answer token cap and parse as "
            "unacceptable — so this is a conservative lower bound, report-only)."
        )
        # Difficulty (gold-class) stratification of cheapest-acceptable tier.
        by_diff: dict[str, dict[str, int]] = {}
        for f in findings:
            g = str(f.get("gold"))
            bucket = by_diff.setdefault(g, {"total": 0, "some_ok": 0})
            bucket["total"] += 1
            if f.get("cheapest_acceptable") is not None:
                bucket["some_ok"] += 1
        if by_diff:
            lines.append("- By gold difficulty (turns with ≥1 acceptable tier / total):")
            for cls in eval_lib.CLASSES:
                if cls in by_diff:
                    b = by_diff[cls]
                    lines.append(f"    - {cls}: {b['some_ok']}/{b['total']}")
    lines.append("")

    # Golden set.
    lines.append("## Golden set (report-only here; CI floor >= 0.80)")
    lines.append("")
    if golden is None:
        lines.append("Golden set not scored in this invocation.")
    else:
        verdict = "PASS" if golden["pass"] else "**FAIL**"
        lines.append(
            f"- `tests/test_agentos_router/data/pilot_golden.jsonl`: "
            f"Pilot final-decision accuracy **{_fmt(golden['accuracy'])}** "
            f"over {golden['n']} rows (floor {golden['floor']}): {verdict}"
        )
        rec = golden["per_class_recall"]
        lines.append(
            "- Golden per-class recall: "
            + ", ".join(f"{c}={_fmt(rec[c])}" for c in eval_lib.CLASSES)
        )
        lines.append(
            "- The CI test `test_pilot_golden.py` enforces the 0.80 floor and "
            "activates when a shipped `models/pilot_v1/` bundle is present "
            "(skips until then)."
        )
    lines.append("")

    lines.append("## Provenance")
    lines.append("")
    lines.append(f"- git sha: `{meta.get('git_sha')}`")
    lines.append(f"- generated: {meta.get('generated_at')}")
    lines.append(
        "- Replay: full `apply_agentos_router` step, guards enabled, history "
        "per conversation in `turn_id` order; scored on the engine's FINAL "
        "(post-guard) tier."
    )
    lines.append(
        "- Raw per-turn rows: `scripts/pilot_router/data/eval_raw_rows.jsonl` (gitignored)."
    )
    lines.append("")

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def _git_sha() -> str:
    import subprocess

    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
    except Exception:
        return "unknown"


def _pilot_temperature() -> Any:
    try:
        manifest = json.loads((PILOT_ARTIFACT_DIR / "manifest.json").read_text())
        return manifest.get("temperature")
    except Exception:
        return None


def _quiet_logs() -> None:
    """Silence the engine's DEBUG router logs so a 2x983-turn replay is readable."""
    import logging

    logging.getLogger().setLevel(logging.WARNING)
    try:
        import structlog

        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
    except Exception:
        pass


async def _main_async(args: argparse.Namespace) -> int:
    _quiet_logs()
    print("Loading test split (FIRST opening of test labels)...")
    rows = load_test_split()
    if args.limit:
        rows = rows[: args.limit]
    convs = group_by_conversation(rows)
    n_turns = sum(len(c) for c in convs)
    print(f"  {n_turns} test turns across {len(convs)} conversations")

    print("Building strategies...")
    pilot = build_pilot_strategy()
    v4 = build_v4_strategy()

    print("Replaying Pilot through the full engine step...")
    pilot_rows = await replay_all(convs, pilot, router_name="pilot")
    print("Replaying v4 through the full engine step...")
    v4_rows = await replay_all(convs, v4, router_name="v4")

    # Align rows by turn_id (both replays visit turns in identical order, but
    # align defensively so the paired metrics/bootstrap cannot drift).
    v4_by_tid = {r["turn_id"]: r for r in v4_rows}
    pilot_rows = [r for r in pilot_rows if r["turn_id"] in v4_by_tid]
    v4_rows = [v4_by_tid[r["turn_id"]] for r in pilot_rows]

    print("Computing gate...")
    result = evaluate_gate(pilot_rows, v4_rows)

    print("Scoring golden set through the full step...")
    golden = await score_golden_set(pilot)

    oracle = None
    if args.oracle:
        print("Running quality-oracle subset (OpenCAP)...")
        oracle = run_oracle_subset(
            pilot_rows, per_class=args.oracle_per_class, budget_usd=args.oracle_budget
        )

    meta = {
        "git_sha": _git_sha(),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_test_turns": len(pilot_rows),
        "pilot_temperature": _pilot_temperature(),
        "pilot": result["pilot"],
        "v4": result["v4"],
        "bootstrap": result["bootstrap"],
        "boundary_set": result["boundary_set"],
        "gate": result["gate"],
        "gate_pass": result["gate_pass"],
        "verdict": result["verdict"],
        "golden": golden,
        "oracle": oracle,
    }

    # Raw rows (gitignored).
    with RAW_ROWS_PATH.open("w", encoding="utf-8") as fh:
        for pr, vr in zip(pilot_rows, v4_rows, strict=True):
            fh.write(json.dumps({"pilot": pr, "v4": vr}) + "\n")

    META_PATH.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    write_report(result, oracle, meta, golden)

    print("")
    print(f"VERDICT: {result['verdict']}")
    for g in result["gate"]:
        print(
            f"  [{'PASS' if g['pass'] else 'FAIL'}] {g['metric']}: pilot={g['pilot']} v4={g['v4']}"
        )
    print(f"Report: {REPORT_PATH}")
    print(f"Meta:   {META_PATH}")
    return 0


def main() -> int:
    import asyncio

    parser = argparse.ArgumentParser(description="Pilot vs v4 eval gate (T9)")
    parser.add_argument("--limit", type=int, default=0, help="cap test turns (smoke)")
    parser.add_argument("--oracle", action="store_true", help="run OpenCAP quality-oracle subset")
    parser.add_argument(
        "--oracle-per-class", type=int, default=38, help="oracle turns per gold class"
    )
    parser.add_argument("--oracle-budget", type=float, default=15.0, help="oracle USD budget cap")
    args = parser.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
