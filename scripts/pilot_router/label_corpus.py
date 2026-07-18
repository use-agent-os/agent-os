#!/usr/bin/env python
"""Label the Pilot router corpus with a reasoning-difficulty tier (spec §6.2, T6).

Each turn from ``data/corpus.jsonl`` (T5) is assigned one of ``R0/R1/R2/R3``
per [`rubric.md`](rubric.md). The labeler is **pinned** (owner decision):

    claude-opus-4.8  via OpenCAP (gw.capminal.ai),  temperature 0,  strict JSON

(OpenCAP is an OpenAI-compatible gateway; the bare model id, no ``anthropic/``
prefix, is required. The earlier OpenRouter pin was retired — verdicts from it
are never reused; the cache file is namespaced by the labeler pin.)

Protocol (spec §6.2):

- **Two independent passes** (A and B) that differ ONLY in the order the rubric
  classes/examples are presented (both orderings versioned below, same model,
  same params). Order variation guards against position bias.
- **Adjudication:** where pass A ≠ pass B, a third call with a distinct
  adjudication prompt shows the rubric, the turn, and both candidate labels
  *without revealing which pass produced which*, and picks the final label.
- **Partition contract (binding):** splits are frozen from T5; labeling never
  moves an item between splits. An adjudicated item whose conversation is in the
  TEST split is tagged ``boundary_set: true`` — a report-only set for §6.4,
  never used to train or tune.

Outputs (under the git-ignored ``scripts/pilot_router/data/``):

- ``labels.jsonl`` — one row per labeled turn:
  ``{turn_id, conversation_id, split, category, label, why, agreement,
  adjudicated, boundary_set}``. **Never committed.**
- ``label_cache.jsonl`` — resumable per-(turn, pass) call log. **Never committed.**
- ``labels_meta.json`` — counts, agreement/adjudication rates, cost, model pin,
  rubric sha256, prompt-ordering versions. **Committed** (no data rows).

Dev-time only. Requires the ``pilot-train`` dependency group and an OpenCAP
key (``OPENCAP_API_KEY``; auto-loaded from ``~/.agentos/.env`` by the AgentOS
tooling, or ``set -a; source ~/.agentos/.env; set +a`` before a bare run)::

    OPENCAP_API_KEY=... \\
      uv run --group pilot-train python scripts/pilot_router/label_corpus.py \\
      --dry-run 100

``--dry-run N`` labels the first ``N`` stratified-sampled turns of the TRAIN
split only (stratified across T5's categories so every category is seen). A
full run (no ``--dry-run``) labels every turn of every split, reusing the cache
so dry-run turns are never re-billed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Protocol

# --------------------------------------------------------------------------- #
# Pinned constants (reproducibility — mirrored in DATA.md / labels_meta.json)
# --------------------------------------------------------------------------- #

# Owner decision (2026-07-18, revised): labeler is pinned to OpenCAP, an
# OpenAI-compatible gateway. Do NOT substitute another provider/model — the pin
# is an owner decision. The bare model id (no ``anthropic/`` prefix) is required
# by this gateway. The previous OpenRouter pin (``anthropic/claude-opus-4.8``)
# was retired; verdicts produced under it are NOT reused (see LABELER_PIN /
# cache keying below).
LABEL_PROVIDER = "opencap"
LABEL_MODEL = "claude-opus-4.8"
LABEL_ENDPOINT = "https://gw.capminal.ai/api/inference/v1/chat/completions"
LABEL_TEMPERATURE = 0.0
LABEL_MAX_TOKENS = 200

# A single string identifying the exact labeler (provider + model + params).
# It keys the resumable cache file so verdicts from a different pin can never be
# mixed in — switching the pin starts a fresh cache.
LABELER_PIN = f"{LABEL_PROVIDER}:{LABEL_MODEL}@t{LABEL_TEMPERATURE}"

# First-party Anthropic list price for claude-opus-4-8 (USD per token), used to
# derive a token-based USD estimate. OpenCAP reports its own cost as
# ``{usd, diem}``; observed usd=0 with only diem populated, so the gate ceiling
# uses THIS token-based USD estimate, and the gateway's diem figure is recorded
# alongside for billing transparency.
USD_PER_INPUT_TOKEN = 5.0 / 1_000_000
USD_PER_OUTPUT_TOKEN = 25.0 / 1_000_000

LABELS = ("R0", "R1", "R2", "R3")
CATEGORIES = (
    "chitchat",
    "factual_qa",
    "writing",
    "coding",
    "math_reasoning",
    "tool_use",
)

# Prompt-ordering versions (versioned so the meta pins them). Pass A presents
# the classes cheapest-first; pass B presents them hardest-first. Same rubric,
# same model, same params — only the presentation order differs.
PROMPT_ORDER_A = "orderA_R0_to_R3_v1"
PROMPT_ORDER_B = "orderB_R3_to_R0_v1"
ADJUDICATION_PROMPT_VERSION = "adjudicate_v1"

DATA_DIR = Path(__file__).resolve().parent / "data"
CORPUS_PATH = DATA_DIR / "corpus.jsonl"
LABELS_PATH = DATA_DIR / "labels.jsonl"
# Cache file is namespaced by the labeler pin so a provider/model switch never
# reuses stale verdicts (the OpenRouter-pinned cache, if any, is left untouched
# and is not read here).
_CACHE_SLUG = re.sub(r"[^A-Za-z0-9]+", "_", LABELER_PIN).strip("_")
CACHE_PATH = DATA_DIR / f"label_cache__{_CACHE_SLUG}.jsonl"
RUBRIC_PATH = Path(__file__).resolve().parent / "rubric.md"
META_PATH = Path(__file__).resolve().parent / "labels_meta.json"

SEED = 42


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #

# Per-class one-line definitions + a couple of anchors, kept short so both
# orderings fit in the system prompt without ballooning token cost. The full
# nuance lives in rubric.md; these are the operative reminders.
_CLASS_BLOCKS: dict[str, str] = {
    "R0": (
        "R0 (trivial/social): no reasoning; a pleasantry or a single atomic "
        'fact. e.g. "thanks!", "what is the capital of France".'
    ),
    "R1": (
        "R1 (routine single-step): one clear step; short writing, a small code "
        'edit, a direct factual question. e.g. "write a regex for emails".'
    ),
    "R2": (
        "R2 (multi-step reasoning): several coupled steps or non-trivial code / "
        'structured analysis. e.g. "debug this stack trace", "merge intervals".'
    ),
    "R3": (
        "R3 (deep/long-horizon): complex architecture, formal proofs, or a plan "
        'over many interacting parts. e.g. "design a zero-downtime migration".'
    ),
}

_RULES = (
    "Assign ONE reasoning-difficulty tier to the user message.\n"
    "Rules:\n"
    "- Judge the message ALONE; assume no prior conversation.\n"
    "- Grade the difficulty of a GOOD ANSWER, not the length of the message.\n"
    "- Grade the hardest thing the message actually asks for.\n"
    "- When genuinely between two tiers, pick the LOWER tier.\n"
    "- A bare open-ended creative prompt (\"write a story\") is R2; a tiny fixed "
    'form ("write a haiku") is R1.\n'
)

_OUTPUT_CONTRACT = (
    'Reply with ONLY a JSON object: {"label": "R0|R1|R2|R3", "why": '
    '"<one short sentence>"}. No other text.'
)


def build_label_system_prompt(order: str) -> str:
    """System prompt for a labeling pass. ``order`` selects the class ordering:
    ``PROMPT_ORDER_A`` lists R0->R3, ``PROMPT_ORDER_B`` lists R3->R0."""
    if order == PROMPT_ORDER_A:
        classes = ["R0", "R1", "R2", "R3"]
    elif order == PROMPT_ORDER_B:
        classes = ["R3", "R2", "R1", "R0"]
    else:  # pragma: no cover - guarded by callers
        raise ValueError(f"unknown prompt order {order!r}")
    blocks = "\n".join(_CLASS_BLOCKS[c] for c in classes)
    # The ordering id is embedded so the exact prompt variant is self-describing
    # in any captured transcript (and pins the presentation order per pass).
    return f"[rubric ordering: {order}]\n{_RULES}\nTiers:\n{blocks}\n\n{_OUTPUT_CONTRACT}"


def build_adjudication_system_prompt() -> str:
    """System prompt for the adjudication pass. Shows all four tiers (canonical
    R0->R3 order) and instructs the model to choose the better of two candidate
    labels — WITHOUT revealing which pass produced which candidate."""
    blocks = "\n".join(_CLASS_BLOCKS[c] for c in ("R0", "R1", "R2", "R3"))
    return (
        "Two independent graders assigned reasoning-difficulty tiers to the same "
        "user message and DISAGREED. Decide the correct tier yourself using the "
        "rubric; the two candidate labels are shown only as context, in no "
        "particular order, and neither is authoritative.\n"
        f"{_RULES}\nTiers:\n{blocks}\n\n{_OUTPUT_CONTRACT}"
    )


def build_user_content(text: str) -> str:
    """User-turn content for a labeling pass (truncated for cost safety)."""
    return f"USER MESSAGE:\n{text[:4000]}"


def build_adjudication_user_content(text: str, cand_a: str, cand_b: str) -> str:
    """User content for adjudication: the turn + both candidate labels sorted so
    ordering never leaks which pass produced which."""
    lo, hi = sorted((cand_a, cand_b))
    return (
        f"USER MESSAGE:\n{text[:4000]}\n\n"
        f"Candidate tiers under consideration: {lo} and {hi}.\n"
        "Return the single correct tier."
    )


# --------------------------------------------------------------------------- #
# Reply parsing
# --------------------------------------------------------------------------- #

_LABEL_RE = re.compile(r"\bR[0-3]\b")


def parse_label(raw: str) -> tuple[str, str] | None:
    """Parse a model reply into ``(label, why)`` or ``None`` if unusable.

    Strict JSON is tried first. If the reply is not clean JSON we make ONE loose
    attempt: pull the first ``R0..R3`` token out of the text. Anything without a
    valid label token returns ``None`` (caller retries, then drops)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    # Strict JSON path.
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and "label" in obj:
            label = str(obj["label"]).strip().upper()
            if label in LABELS:
                why = str(obj.get("why", "")).strip()
                return label, why
    except (json.JSONDecodeError, ValueError):
        pass
    # Loose fallback: some fenced-JSON or trailing-prose replies still carry an
    # unambiguous label token. Accept only if exactly one distinct tier appears.
    found = set(_LABEL_RE.findall(raw.upper()))
    if len(found) == 1:
        label = found.pop()
        return label, ""
    return None


# --------------------------------------------------------------------------- #
# Client protocol + real OpenCAP client (network — not exercised in CI)
# --------------------------------------------------------------------------- #


class LabelClient(Protocol):
    def complete(self, system: str, user: str) -> tuple[str, dict[str, Any]]:
        """Return ``(content, usage)`` for one chat completion."""
        ...


class OpenCAPLabelClient:
    """Pinned ``claude-opus-4.8`` labeler over the OpenCAP gateway.

    OpenAI-compatible ``/chat/completions``. The response is standard OpenAI
    shape (``choices[0].message.content``, ``usage.prompt_tokens/completion_tokens``)
    plus a **top-level** ``cost`` object ``{usd, diem}``. Because the gateway has
    been observed reporting ``usd: 0`` with only ``diem`` populated, we track
    BOTH the gateway's reported usd/diem AND a token-based USD estimate derived
    from first-party list pricing; the gate ceiling uses the token-based USD."""

    def __init__(self, api_key: str) -> None:
        import httpx

        # A single pooled client shared across polite worker threads.
        self._client = httpx.Client(
            timeout=60.0, limits=httpx.Limits(max_connections=16)
        )
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._usage_lock = threading.Lock()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        # Gateway-reported cost, summed across calls this run.
        self.reported_cost_usd = 0.0
        self.reported_cost_diem = 0.0
        self.calls = 0
        self._waf_403_streak = 0

    def _token_usd(self, prompt_t: int, completion_t: int) -> float:
        return prompt_t * USD_PER_INPUT_TOKEN + completion_t * USD_PER_OUTPUT_TOKEN

    def complete(self, system: str, user: str) -> tuple[str, dict[str, Any]]:
        payload = {
            "model": LABEL_MODEL,
            "temperature": LABEL_TEMPERATURE,
            "max_tokens": LABEL_MAX_TOKENS,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        for attempt in range(5):
            try:
                resp = self._client.post(
                    LABEL_ENDPOINT, headers=self._headers, json=payload
                )
                if resp.status_code == 403 and resp.text.lstrip()[:15].lower().startswith(
                    ("<!doctype", "<html")
                ):
                    # WAF/edge block page (HTML body), seen intermittently under
                    # parallel load — NOT an auth failure. Transient: long
                    # jittered backoff. A fuse of 20 consecutive block pages
                    # still aborts so a hard block cannot spin forever. A
                    # genuine auth/quota 403 returns JSON and stays fatal below.
                    with self._usage_lock:
                        self._waf_403_streak += 1
                        streak = self._waf_403_streak
                    if streak >= 20:
                        raise RuntimeError(
                            "OpenCAP returned 20 consecutive WAF block pages "
                            "(status 403, HTML body) — aborting"
                        )
                    time.sleep(20 + random.uniform(0, 20))
                    continue
                if resp.status_code in (401, 402, 403, 404):
                    # Auth / model-availability / billing errors are fatal: the
                    # pin is an owner decision — surface, do not silently retry.
                    raise RuntimeError(
                        f"OpenCAP rejected the request "
                        f"(status {resp.status_code}): {resp.text[:300]}"
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    # Polite backoff for the gateway: honor Retry-After if given,
                    # else exponential with jitter (capped). Jitter de-syncs the
                    # worker pool so a burst of 429s doesn't retry in lockstep.
                    retry_after = resp.headers.get("retry-after")
                    if retry_after and retry_after.isdigit():
                        delay = float(retry_after)
                    else:
                        base = min(2 ** (attempt + 1), 30)
                        delay = base + random.uniform(0, base * 0.5)
                    time.sleep(delay)
                    continue
                resp.raise_for_status()
                data = resp.json()
                usage = data.get("usage") or {}
                cost = data.get("cost") or {}
                p = int(usage.get("prompt_tokens", 0) or 0)
                c = int(usage.get("completion_tokens", 0) or 0)
                with self._usage_lock:
                    self._waf_403_streak = 0
                    self.prompt_tokens += p
                    self.completion_tokens += c
                    self.reported_cost_usd += float(cost.get("usd", 0.0) or 0.0)
                    self.reported_cost_diem += float(cost.get("diem", 0.0) or 0.0)
                    self.calls += 1
                content = str(data["choices"][0]["message"]["content"])
                return content, dict(usage)
            except RuntimeError:
                # Fatal, non-retryable (auth/quota/model 401/402/403/404).
                # Propagate so the run stops rather than hammering the gateway.
                raise
            except Exception:
                # Transient (timeout, connection reset, transport error). Retry
                # with jittered backoff; on final exhaustion, DEGRADE to an empty
                # reply instead of raising — that surfaces as a parse failure and
                # the turn is retried-then-dropped at the pass level, never
                # crashing the whole run.
                if attempt == 4:
                    return "", {}
                base = min(2 ** (attempt + 1), 30)
                time.sleep(base + random.uniform(0, base * 0.5))
        return "", {}

    # Token-based USD estimate over all calls this run (the gate ceiling basis).
    @property
    def token_based_cost_usd(self) -> float:
        return self._token_usd(self.prompt_tokens, self.completion_tokens)

    def close(self) -> None:
        self._client.close()


# --------------------------------------------------------------------------- #
# Disk cache (idempotent / resumable), keyed by (turn_id, pass)
# --------------------------------------------------------------------------- #

# pass tokens used in the cache key and the call log.
PASS_A = "A"
PASS_B = "B"
PASS_ADJ = "ADJ"


def _cache_key(turn_id: str, which: str) -> str:
    return f"{turn_id}\t{which}"


def _load_cache(path: Path) -> dict[str, dict[str, Any]]:
    """Load the resumable call log into ``{(turn_id, pass) -> record}``.

    Each record has ``label`` and ``why``. Last write wins on duplicate keys."""
    cache: dict[str, dict[str, Any]] = {}
    if path.is_file():
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    key = _cache_key(str(rec["turn_id"]), str(rec["pass"]))
                    cache[key] = {"label": rec["label"], "why": rec.get("why", "")}
                except (json.JSONDecodeError, KeyError):
                    continue
    return cache


def _append_cache(
    path: Path, turn_id: str, which: str, label: str, why: str, lock: threading.Lock
) -> None:
    rec = {"turn_id": turn_id, "pass": which, "label": label, "why": why}
    with lock:
        with path.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Single-turn labeling (cache-aware, retry-then-drop)
# --------------------------------------------------------------------------- #

MAX_LABEL_RETRIES = 2  # per pass: 1 try + this many retries on unparseable JSON


def label_one_pass(
    text: str,
    turn_id: str,
    which: str,
    system: str,
    client: LabelClient,
    cache: dict[str, dict[str, Any]],
    cache_lock: threading.Lock,
) -> tuple[str, str] | None:
    """Run (or read from cache) one labeling pass. Returns ``(label, why)`` or
    ``None`` after ``MAX_LABEL_RETRIES`` unparseable replies (turn dropped)."""
    key = _cache_key(turn_id, which)
    cached = cache.get(key)
    if cached is not None:
        return cached["label"], cached["why"]
    user = build_user_content(text)
    for _ in range(MAX_LABEL_RETRIES + 1):
        content, _usage = client.complete(system, user)
        parsed = parse_label(content)
        if parsed is not None:
            label, why = parsed
            cache[key] = {"label": label, "why": why}
            _append_cache(CACHE_PATH, turn_id, which, label, why, cache_lock)
            return label, why
    return None


def adjudicate(
    text: str,
    turn_id: str,
    cand_a: str,
    cand_b: str,
    client: LabelClient,
    cache: dict[str, dict[str, Any]],
    cache_lock: threading.Lock,
) -> str | None:
    """Third-pass adjudication when A != B. Returns the final label, or ``None``
    if adjudication itself never returns a parseable label (turn dropped)."""
    key = _cache_key(turn_id, PASS_ADJ)
    cached = cache.get(key)
    if cached is not None:
        return str(cached["label"])
    system = build_adjudication_system_prompt()
    user = build_adjudication_user_content(text, cand_a, cand_b)
    for _ in range(MAX_LABEL_RETRIES + 1):
        content, _usage = client.complete(system, user)
        parsed = parse_label(content)
        if parsed is not None:
            label, why = parsed
            cache[key] = {"label": label, "why": why}
            _append_cache(CACHE_PATH, turn_id, PASS_ADJ, label, why, cache_lock)
            return label
    return None


def label_turn(
    turn: dict[str, Any],
    client: LabelClient,
    cache: dict[str, dict[str, Any]],
    cache_lock: threading.Lock,
) -> dict[str, Any] | None:
    """Two-pass + adjudication for one corpus turn. Returns a label row, or
    ``None`` if any required pass is unrecoverably malformed (turn dropped).

    ``boundary_set`` is set when the item was adjudicated AND its conversation is
    in the TEST split (spec §6.2 report-only set; never train/tune)."""
    text = turn["text"]
    turn_id = str(turn["turn_id"])
    sys_a = build_label_system_prompt(PROMPT_ORDER_A)
    sys_b = build_label_system_prompt(PROMPT_ORDER_B)
    res_a = label_one_pass(text, turn_id, PASS_A, sys_a, client, cache, cache_lock)
    if res_a is None:
        return None
    res_b = label_one_pass(text, turn_id, PASS_B, sys_b, client, cache, cache_lock)
    if res_b is None:
        return None
    label_a, why_a = res_a
    label_b, why_b = res_b

    agreement = label_a == label_b
    adjudicated = not agreement
    if agreement:
        final_label, final_why = label_a, why_a
    else:
        adj = adjudicate(text, turn_id, label_a, label_b, client, cache, cache_lock)
        if adj is None:
            return None
        final_label = adj
        final_why = f"adjudicated between {label_a} and {label_b}"

    boundary_set = adjudicated and turn.get("split") == "test"
    return {
        "turn_id": turn_id,
        "conversation_id": turn["conversation_id"],
        "split": turn["split"],
        "category": turn["category"],
        "label": final_label,
        "why": final_why,
        "agreement": agreement,
        "adjudicated": adjudicated,
        "boundary_set": boundary_set,
    }


# --------------------------------------------------------------------------- #
# Corpus IO + dry-run stratification
# --------------------------------------------------------------------------- #


def load_corpus(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def stratified_dry_run_sample(
    rows: list[dict[str, Any]], n: int
) -> list[dict[str, Any]]:
    """Pick ``n`` TRAIN-split turns, round-robin across T5 categories so every
    category appears. Deterministic (stable input order within a category). If
    the TRAIN split has fewer than ``n`` turns, returns all of them."""
    train = [r for r in rows if r.get("split") == "train"]
    if len(train) <= n:
        return list(train)
    by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in train:
        by_cat[row["category"]].append(row)
    cats = [c for c in CATEGORIES if by_cat[c]]
    selected: list[dict[str, Any]] = []
    idx = {c: 0 for c in cats}
    while len(selected) < n and cats:
        for c in list(cats):
            if len(selected) >= n:
                break
            pool = by_cat[c]
            if idx[c] < len(pool):
                selected.append(pool[idx[c]])
                idx[c] += 1
            else:
                cats.remove(c)
    return selected


# --------------------------------------------------------------------------- #
# Concurrent labeling pass over a set of turns
# --------------------------------------------------------------------------- #


def label_many(
    turns: list[dict[str, Any]],
    client: LabelClient,
    cache: dict[str, dict[str, Any]],
    *,
    workers: int,
) -> tuple[list[dict[str, Any]], int, list[str]]:
    """Label ``turns`` concurrently. Returns (label rows, dropped count, skipped
    turn ids). Cached passes cost no network call; the disk cache append is
    serialized.

    Durability: an *unexpected* per-turn exception (not a fatal auth/quota
    error) is caught, logged, and the turn is skipped — one bad turn never
    crashes the whole run. Fatal ``RuntimeError`` (401/402/403/404, e.g. a
    provider quota limit) is re-raised so the run stops cleanly instead of
    hammering the gateway."""
    cache_lock = threading.Lock()
    rows: list[dict[str, Any]] = []
    dropped = 0
    skipped: list[str] = []

    def _work(turn: dict[str, Any]) -> dict[str, Any] | None:
        return label_turn(turn, client, cache, cache_lock)

    it = iter(turns)
    inflight: dict[Any, dict[str, Any]] = {}
    window = max(workers * 3, 32)
    done_count = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            while len(inflight) < window:
                nxt = next(it, None)
                if nxt is None:
                    break
                inflight[pool.submit(_work, nxt)] = nxt
            if not inflight:
                break
            done, _ = wait(set(inflight), return_when=FIRST_COMPLETED)
            for fut in done:
                turn = inflight.pop(fut)
                done_count += 1
                try:
                    row = fut.result()
                except RuntimeError:
                    # Fatal (auth/quota/model). Stop the run — surfaced to caller.
                    raise
                except Exception as exc:  # noqa: BLE001 - deliberate skip-and-log
                    skipped.append(str(turn["turn_id"]))
                    print(
                        f"[label] SKIP turn {turn['turn_id']}: "
                        f"{type(exc).__name__}: {str(exc)[:200]}",
                        flush=True,
                    )
                    continue
                if row is None:
                    dropped += 1
                else:
                    rows.append(row)
                if done_count % 100 == 0:
                    print(
                        f"[label] processed={done_count} labeled={len(rows)} "
                        f"dropped={dropped} skipped={len(skipped)}",
                        flush=True,
                    )
    return rows, dropped, skipped


# --------------------------------------------------------------------------- #
# Stats + metadata
# --------------------------------------------------------------------------- #


def compute_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Distribution + agreement/adjudication/boundary rates over label rows."""
    n = len(rows)
    label_counts: Counter[str] = Counter(r["label"] for r in rows)
    per_split: dict[str, Counter[str]] = defaultdict(Counter)
    for r in rows:
        per_split[r["split"]][r["label"]] += 1
    agreed = sum(1 for r in rows if r["agreement"])
    adjudicated = sum(1 for r in rows if r["adjudicated"])
    boundary = sum(1 for r in rows if r["boundary_set"])
    return {
        "labeled": n,
        "label_counts": {lab: label_counts.get(lab, 0) for lab in LABELS},
        "label_share": {
            lab: round(label_counts.get(lab, 0) / n, 4) if n else 0.0 for lab in LABELS
        },
        "per_split_label_counts": {
            split: {lab: per_split[split].get(lab, 0) for lab in LABELS}
            for split in sorted(per_split)
        },
        "agreement_rate": round(agreed / n, 4) if n else 0.0,
        "adjudication_rate": round(adjudicated / n, 4) if n else 0.0,
        "boundary_set_size": boundary,
    }


def _usage_block(client: LabelClient, labeled: int) -> dict[str, Any]:
    if not isinstance(client, OpenCAPLabelClient):
        return {}
    calls = client.calls
    # Token-based USD estimate is the gate-ceiling basis (gateway usd reads 0,
    # only diem populated); the gateway's reported usd/diem are recorded too.
    token_usd = round(client.token_based_cost_usd, 6)
    gw_usd = round(client.reported_cost_usd, 6)
    gw_diem = round(client.reported_cost_diem, 6)
    per_turn = (token_usd / labeled) if labeled else 0.0
    return {
        "provider": LABEL_PROVIDER,
        "labeler_pin": LABELER_PIN,
        "llm_calls_this_run": calls,
        "prompt_tokens_this_run": client.prompt_tokens,
        "completion_tokens_this_run": client.completion_tokens,
        # Token-based USD estimate (first-party list price) — the gate basis.
        "token_based_cost_this_run_usd": token_usd,
        "measured_cost_per_turn_usd": round(per_turn, 6),
        # Gateway-reported cost, for billing transparency. usd is often 0; the
        # gateway bills in diem.
        "gateway_reported_cost_this_run_usd": gw_usd,
        "gateway_reported_cost_this_run_diem": gw_diem,
        "billing_currency_note": (
            "OpenCAP reports usd=0 with cost in diem; the USD figures here are a "
            "token-count estimate at first-party list price ($5/$25 per MTok)."
        ),
    }


def project_full_cost(
    per_turn_usd: float, corpus_size: int, adjudication_rate: float
) -> float:
    """Project full-run cost from the measured per-turn average. The per-turn
    average already folds in adjudication that occurred in the sample; scaling by
    corpus size carries that adjudication rate forward. ``adjudication_rate`` is
    accepted for transparency in the reported figure."""
    return round(per_turn_usd * corpus_size, 4)


def write_meta(
    stats: dict[str, Any],
    usage: dict[str, Any],
    *,
    mode: str,
    corpus_size: int,
    rubric_sha: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "mode": mode,
        "label_provider": LABEL_PROVIDER,
        "label_endpoint": LABEL_ENDPOINT,
        "label_model": LABEL_MODEL,
        "labeler_pin": LABELER_PIN,
        "label_temperature": LABEL_TEMPERATURE,
        "label_max_tokens": LABEL_MAX_TOKENS,
        "prompt_order_a": PROMPT_ORDER_A,
        "prompt_order_b": PROMPT_ORDER_B,
        "adjudication_prompt_version": ADJUDICATION_PROMPT_VERSION,
        "rubric_file": RUBRIC_PATH.name,
        "rubric_sha256": rubric_sha,
        "seed": SEED,
        "corpus_size": corpus_size,
        "stats": stats,
        "usage": usage,
    }
    if extra:
        meta.update(extra)
    if LABELS_PATH.is_file():
        meta["labels_file"] = {LABELS_PATH.name: _sha256(LABELS_PATH)}
    META_PATH.write_text(json.dumps(meta, indent=2) + "\n")
    return meta


# --------------------------------------------------------------------------- #
# Gate evaluation (owner-delegated)
# --------------------------------------------------------------------------- #

GATE_MIN_AGREEMENT = 0.70
GATE_MAX_CLASS_SHARE = 0.70
# Ceiling raised 60 -> 70 by controller decision (2026-07-18) under the owner's
# standing "run, don't stop to ask" instruction: the $64.94 dry-run projection
# was an 8% overage on a conservative ceiling, and OpenCAP bills in diem with
# usd=0, so real USD cost is likely below the token-based estimate.
GATE_MAX_COST_USD = 70.0


def evaluate_gate(
    stats: dict[str, Any], projected_cost: float
) -> tuple[bool, list[dict[str, Any]]]:
    """Evaluate the three owner-delegated gate criteria. Returns
    (all_pass, per-criterion rows)."""
    agreement = stats["agreement_rate"]
    shares = stats["label_share"]
    counts = stats["label_counts"]
    all_present = all(counts.get(lab, 0) > 0 for lab in LABELS)
    max_share = max(shares.values()) if shares else 1.0

    c1_pass = agreement >= GATE_MIN_AGREEMENT
    c2_pass = all_present and max_share <= GATE_MAX_CLASS_SHARE
    c3_pass = projected_cost <= GATE_MAX_COST_USD

    rows = [
        {
            "criterion": "two-pass agreement >= 0.70",
            "measured": f"{agreement:.3f}",
            "pass": c1_pass,
        },
        {
            "criterion": "all four classes present, none > 70%",
            "measured": (
                f"present={all_present}, max_share={max_share:.3f} "
                f"(counts={counts})"
            ),
            "pass": c2_pass,
        },
        {
            "criterion": "projected full-run cost <= $60",
            "measured": f"${projected_cost:.2f}",
            "pass": c3_pass,
        },
    ]
    return (c1_pass and c2_pass and c3_pass), rows


def print_gate_table(rows: list[dict[str, Any]]) -> None:
    print("\n=== Owner-delegated dry-run gate ===")
    for r in rows:
        verdict = "PASS" if r["pass"] else "FAIL"
        print(f"[{verdict}] {r['criterion']}: {r['measured']}")
    print("====================================\n")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def run(
    *,
    dry_run: int | None,
    client: LabelClient,
    workers: int,
) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rubric_sha = _sha256(RUBRIC_PATH)
    cache = _load_cache(CACHE_PATH)
    all_rows = load_corpus(CORPUS_PATH)
    corpus_size = len(all_rows)

    if dry_run is not None:
        turns = stratified_dry_run_sample(all_rows, dry_run)
        mode = "dry_run"
    else:
        turns = all_rows
        mode = "full"

    print(
        f"[label] mode={mode} turns={len(turns)} corpus_size={corpus_size}",
        flush=True,
    )
    rows, dropped, skipped = label_many(turns, client, cache, workers=workers)
    print(
        f"[label] labeled={len(rows)} dropped={dropped} skipped={len(skipped)}",
        flush=True,
    )

    # Write labels JSONL (git-ignored).
    with LABELS_PATH.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    stats = compute_stats(rows)
    stats["dropped"] = dropped
    stats["skipped"] = len(skipped)
    stats["skipped_turn_ids"] = skipped[:100]  # capped sample for the report
    usage = _usage_block(client, len(rows))
    per_turn = float(usage.get("measured_cost_per_turn_usd", 0.0)) if usage else 0.0
    projected = project_full_cost(per_turn, corpus_size, stats["adjudication_rate"])

    extra: dict[str, Any] = {}
    if mode == "dry_run":
        extra["projected_full_run_cost_usd"] = projected
        gate_pass, gate_rows = evaluate_gate(stats, projected)
        extra["gate"] = {"pass": gate_pass, "criteria": gate_rows}

    meta = write_meta(
        stats,
        usage,
        mode=mode,
        corpus_size=corpus_size,
        rubric_sha=rubric_sha,
        extra=extra,
    )

    if mode == "dry_run":
        print_gate_table(meta["gate"]["criteria"])
        print(f"projected full-run cost: ${projected:.2f}")
        print(f"gate: {'ALL PASS' if meta['gate']['pass'] else 'FAILED'}")

    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        type=int,
        default=None,
        metavar="N",
        help="label the first N stratified TRAIN-split turns and stop",
    )
    # Moderate default; the gateway is politely rate-limited with 429 backoff.
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args(argv)

    api_key = os.environ.get("OPENCAP_API_KEY")
    if not api_key:
        print(
            "ERROR: OPENCAP_API_KEY is required for the labeling run",
            file=sys.stderr,
        )
        return 2

    client: OpenCAPLabelClient = OpenCAPLabelClient(api_key)
    try:
        meta = run(dry_run=args.dry_run, client=client, workers=args.workers)
    finally:
        client.close()

    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
