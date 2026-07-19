#!/usr/bin/env python
"""Mine hard (R3-like) and trivial (R0-like) candidate turns from WildChat.

Pilot-v1 R3 uplift round (owner-approved amendment, 2026-07-19): the config-grid
round proved config levers cannot move R3 recall (bit-identical 0.226 across all
four configs) — the only R3 lever is **more real R3 data**. This script streams
NEW WildChat conversations (not already in the T5 corpus) and mines two targeted
pools:

- **R3-like** (deep/long-horizon): heuristic prefilter on length / code fence /
  math-proof markers / architecture-design-migration-security keywords, then a
  cheap-LLM prescreen that asks BOTH "self-contained?" AND "plausibly hardest
  tier (R3)?".
- **R0-like** (trivial/social): heuristic prefilter for very short greetings /
  simple atomic lookups (R0 is also under-represented — only 370 in the T5
  corpus), prescreened for "self-contained?" AND "plausibly trivial (R0)?".

Pipeline (each user turn evaluated independently — labels must be derivable
from the current message alone, same contract as T5):

    stream WildChat  →  user turns  →  English  →  redaction-clean  →
    NOT already in the T5 corpus (turn_id + MinHash-LSH near-dup)  →
    direction heuristic (R3-like / R0-like)  →
    cheap-LLM prescreen (self-contained AND tier-plausible, strict JSON)  →
    accept up to per-direction targets

Reuses T5 infrastructure directly (imported from ``sample_corpus``): the frozen
``assign_split``, the streaming turn iterator, the English / redaction /
triviality filters, the MinHash shingling, and the pinned deepseek-v4-flash
client shape. The prescreen prompt is NEW (asks a two-part question) and its
verdicts are cached in a SEPARATE, prompt-namespaced cache file so they never
mix with T5's self-containment cache.

Outputs (under the git-ignored ``scripts/pilot_router/data/``):

- ``mined_candidates.jsonl`` — one row per accepted candidate:
  ``{turn_id, conversation_id, text, category, direction, split}`` where
  ``direction`` ∈ {``r3_like``, ``r0_like``}. **Never committed.**
- ``mine_meta.json`` — per-stage counts, per-direction counts, dedupe stats,
  cost, dataset revision, prescreen prompt version, seed. **Committed.**
- ``prescreen_cache__<slug>.jsonl`` — resumable per-turn prescreen verdict log.
  **Never committed.**

Progress is logged unbuffered to stdout (redirect to ``data/mine_run.log``).

Dev-time only. Requires the ``pilot-train`` dependency group::

    OPENROUTER_API_KEY=... \\
      uv run --group pilot-train python \\
      scripts/pilot_router/mine_hard_candidates.py \\
      --r3-target 900 --r0-target 400 --screen-cap 150000

A ``--screen-cap N`` smoke run validates the real HF + OpenRouter path
end-to-end on a tiny slice before the full run.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Protocol

# Load the T5 sampler by path (scripts/ is not a package on sys.path); this
# mirrors how the pilot script tests reach their modules and lets us reuse the
# frozen partition, the WildChat stream, the turn filters, and the MinHash
# shingling WITHOUT duplicating any of them.
_SAMPLE_PATH = Path(__file__).resolve().parent / "sample_corpus.py"
_spec = importlib.util.spec_from_file_location("pilot_sample_corpus_for_mining", _SAMPLE_PATH)
assert _spec is not None and _spec.loader is not None
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)

# Re-exported T5 primitives (single source of truth — do not re-implement).
assign_split = sc.assign_split
is_english = sc.is_english
is_redaction_clean = sc.is_redaction_clean
is_substantive = sc.is_substantive
_shingles = sc._shingles
categorize = sc.categorize

# --------------------------------------------------------------------------- #
# Pinned constants (reproducibility — mirrored in DATA.md / mine_meta.json)
# --------------------------------------------------------------------------- #

DATASET_ID = sc.DATASET_ID
DATASET_REVISION = sc.DATASET_REVISION
SEED = sc.SEED

# Same cheap-LLM pin as T5's self-containment filter (deepseek-v4-flash @ t0),
# but the prescreen asks a TWO-PART question, so it needs more output tokens
# than the 8-token self-containment yes/no.
PRESCREEN_MODEL = sc.FILTER_MODEL
PRESCREEN_TEMPERATURE = 0.0
PRESCREEN_MAX_TOKENS = 40
OPENROUTER_URL = sc.OPENROUTER_URL

MINHASH_PERMS = sc.MINHASH_PERMS

# Directions.
R3_LIKE = "r3_like"
R0_LIKE = "r0_like"
DIRECTIONS = (R3_LIKE, R0_LIKE)

DEFAULT_R3_TARGET = 900
DEFAULT_R0_TARGET = 400
DEFAULT_SCREEN_CAP = 150_000

# Heuristic thresholds (R3 direction).
R3_MIN_CHARS = 600
# Trivial direction: short + no hard-signal markers.
R0_MAX_CHARS = 120

# Prompt (versioned — mine_meta pins this id). Asks a compound question so ONE
# call decides both self-containment and tier plausibility for a direction.
PRESCREEN_PROMPT_V1 = (
    "You are a data-mining classifier for a reasoning-difficulty router. For a "
    "single user chat message you output TWO booleans:\n"
    "1. self_contained: could a reader act on this message WITHOUT seeing any "
    "earlier messages? (concrete standalone request = true; 'now add retry to "
    "that', 'yes do that', 'the second one', 'continue' = false)\n"
    "2. tier_match: does the message plausibly belong to the TARGET tier "
    "described below? Grade the difficulty of a GOOD ANSWER, not message length.\n\n"
    "Reply with ONLY a JSON object: "
    '{"self_contained": true/false, "tier_match": true/false}. No other text.'
)

# Per-direction description appended to the prompt so ONE prompt text serves both
# directions (the description is part of the cache key namespace via the slug).
_TIER_DESCRIPTIONS: dict[str, str] = {
    R3_LIKE: (
        "TARGET tier = R3 (deep / long-horizon): complex system architecture or "
        "design, formal proofs, multi-step migrations, security analysis, or a "
        "plan over many interacting parts. NOT a single fact, a one-liner, or a "
        "small self-contained snippet."
    ),
    R0_LIKE: (
        "TARGET tier = R0 (trivial / social): no reasoning at all — a pleasantry, "
        "a greeting, or a single atomic fact lookup (e.g. 'thanks!', 'hello', "
        "'what is the capital of France'). NOT anything requiring even one "
        "reasoning step."
    ),
}

PRESCREEN_PROMPT_VERSION = "prescreen_v1"

DATA_DIR = Path(__file__).resolve().parent / "data"
CORPUS_PATH = DATA_DIR / "corpus.jsonl"
CANDIDATES_PATH = DATA_DIR / "mined_candidates.jsonl"
META_PATH = Path(__file__).resolve().parent / "mine_meta.json"


def _cache_path_for(prompt_version: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", f"{PRESCREEN_MODEL}_{prompt_version}").strip("_")
    return DATA_DIR / f"prescreen_cache__{slug}.jsonl"


CACHE_PATH = _cache_path_for(PRESCREEN_PROMPT_VERSION)


# --------------------------------------------------------------------------- #
# Direction heuristics (cheap, deterministic — run BEFORE any LLM call)
# --------------------------------------------------------------------------- #

_CODE_FENCE_RE = re.compile(r"```|~~~")
_MATH_PROOF_RE = re.compile(
    r"\bprove\b|\bproof\b|\btheorem\b|\blemma\b|\bderive\b|\bderivation\b|"
    r"\bintegral\b|\beigen|\bmatrix\b|\basymptotic\b|\bcomplexity\b|"
    r"\binduction\b|\bconvergence\b|\boptimi[sz]e\b",
    re.IGNORECASE,
)
# Architecture / design / migration / security-analysis keywords (R3 signal).
_ARCH_RE = re.compile(
    r"\barchitect\w*\b|\bdesign (a|an|the|our|my)\b|\bmigrat\w+\b|"
    r"\bscal(e|ing|able)\b|\bdistributed\b|\bmicroservice\w*\b|"
    r"\bzero[- ]downtime\b|\bhigh[- ]availab\w*\b|\bfault[- ]toleran\w*\b|"
    r"\bthreat model\w*\b|\bsecurity (analysis|review|audit|design)\b|"
    r"\bconcurren\w+\b|\brace condition\b|\bconsensus\b|\bsharding\b|"
    r"\btrade[- ]?offs?\b|\bend[- ]to[- ]end\b.*\b(system|pipeline|design)\b|"
    r"\brefactor\w*\b.*\b(architecture|system|codebase)\b|\bschema (design|migration)\b",
    re.IGNORECASE,
)

_GREETING_RE = re.compile(
    r"^\s*(hi|hey|hello|yo|sup|hiya|howdy|good (morning|afternoon|evening|night)|"
    r"how are you|how's it going|thanks|thank you|thx|ty|ok|okay|cool|nice|"
    r"great|lol|haha|bye|goodbye|please|yes|no|sure)\b",
    re.IGNORECASE,
)
# Simple atomic lookup: a short "what/who/when/where is X" one-liner.
_SIMPLE_LOOKUP_RE = re.compile(
    r"^\s*(what|who|when|where|which)\s+(is|are|was|were|'s)\b",
    re.IGNORECASE,
)


def is_r3_like(text: str) -> bool:
    """Heuristic R3 prefilter: long OR code-fenced OR math/proof markers OR
    architecture/design/migration/security-analysis keywords. Deliberately
    high-recall (the LLM prescreen and then Opus tighten precision)."""
    t = text.strip()
    if len(t) >= R3_MIN_CHARS:
        return True
    if _CODE_FENCE_RE.search(t):
        return True
    if _MATH_PROOF_RE.search(t):
        return True
    if _ARCH_RE.search(t):
        return True
    return False


def is_r0_like(text: str) -> bool:
    """Heuristic R0 prefilter: very short AND (a greeting/pleasantry OR a simple
    atomic lookup). Excludes anything with code / multi-line structure."""
    t = text.strip()
    if len(t) > R0_MAX_CHARS:
        return False
    if _CODE_FENCE_RE.search(t):
        return False
    if "\n" in t and len(t) > 60:
        return False
    if _GREETING_RE.search(t):
        return True
    if _SIMPLE_LOOKUP_RE.search(t):
        return True
    return False


def direction_for(text: str) -> str | None:
    """Assign a mining direction to a turn, or ``None`` if it matches neither
    heuristic. R3 is checked first (higher-signal); a turn that is somehow both
    long-and-greeting-ish goes to R3."""
    if is_r3_like(text):
        return R3_LIKE
    if is_r0_like(text):
        return R0_LIKE
    return None


# --------------------------------------------------------------------------- #
# Prescreen reply parsing
# --------------------------------------------------------------------------- #

_TRUE_RE = re.compile(r"\btrue\b", re.IGNORECASE)


def parse_prescreen(raw: str) -> tuple[bool, bool] | None:
    """Parse a prescreen reply into ``(self_contained, tier_match)`` or ``None``
    if unusable. Strict JSON first; a loose fallback reads the two boolean keys
    out of a fenced / prose-wrapped reply. Anything without both keys -> None
    (caller retries then drops — a candidate we cannot verify is not accepted)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and "self_contained" in obj and "tier_match" in obj:
            return bool(obj["self_contained"]), bool(obj["tier_match"])
    except (json.JSONDecodeError, ValueError):
        pass
    # Loose fallback: pull the value that follows each key name.
    sc_m = re.search(r'"?self_contained"?\s*[:=]\s*(true|false)', raw, re.IGNORECASE)
    tm_m = re.search(r'"?tier_match"?\s*[:=]\s*(true|false)', raw, re.IGNORECASE)
    if sc_m and tm_m:
        return (
            _TRUE_RE.fullmatch(sc_m.group(1)) is not None,
            _TRUE_RE.fullmatch(tm_m.group(1)) is not None,
        )
    return None


def accepted_by_prescreen(parsed: tuple[bool, bool] | None) -> bool:
    """A candidate is accepted only when BOTH self-contained AND tier-matching."""
    if parsed is None:
        return False
    self_contained, tier_match = parsed
    return self_contained and tier_match


# --------------------------------------------------------------------------- #
# Prescreen client protocol + real OpenRouter client (network — not in CI)
# --------------------------------------------------------------------------- #


class PrescreenClient(Protocol):
    def complete(self, text: str, direction: str) -> str: ...


class OpenRouterPrescreenClient:
    """Pinned deepseek-v4-flash two-part prescreen classifier over OpenRouter.

    Same wire shape as T5's ``OpenRouterClient`` (retry/backoff, usage
    accounting) but the system prompt embeds the per-direction tier description
    so one call decides self-containment AND tier plausibility."""

    def __init__(self, api_key: str) -> None:
        import httpx

        self._client = httpx.Client(timeout=30.0, limits=httpx.Limits(max_connections=64))
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._usage_lock = threading.Lock()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.reported_cost_usd = 0.0
        self.calls = 0

    def _system(self, direction: str) -> str:
        return f"{PRESCREEN_PROMPT_V1}\n\n{_TIER_DESCRIPTIONS[direction]}"

    def complete(self, text: str, direction: str) -> str:
        payload = {
            "model": PRESCREEN_MODEL,
            "temperature": PRESCREEN_TEMPERATURE,
            "max_tokens": PRESCREEN_MAX_TOKENS,
            "messages": [
                {"role": "system", "content": self._system(direction)},
                {"role": "user", "content": text[:4000]},
            ],
        }
        for attempt in range(4):
            try:
                resp = self._client.post(OPENROUTER_URL, headers=self._headers, json=payload)
                if resp.status_code == 429 or resp.status_code >= 500:
                    time.sleep(2 * (attempt + 1))
                    continue
                resp.raise_for_status()
                data = resp.json()
                usage = data.get("usage") or {}
                with self._usage_lock:
                    self.prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
                    self.completion_tokens += int(usage.get("completion_tokens", 0) or 0)
                    self.reported_cost_usd += float(usage.get("cost", 0.0) or 0.0)
                    self.calls += 1
                return str(data["choices"][0]["message"]["content"])
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2 * (attempt + 1))
        return ""

    def close(self) -> None:
        self._client.close()


# --------------------------------------------------------------------------- #
# Disk cache (idempotent / resumable) — keyed by turn id (per prompt version)
# --------------------------------------------------------------------------- #


def _load_cache(path: Path) -> dict[str, tuple[bool, bool]]:
    cache: dict[str, tuple[bool, bool]] = {}
    if path.is_file():
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    cache[str(rec["turn_id"])] = (
                        bool(rec["self_contained"]),
                        bool(rec["tier_match"]),
                    )
                except (json.JSONDecodeError, KeyError):
                    continue
    return cache


def _append_cache(
    path: Path,
    turn_id: str,
    direction: str,
    parsed: tuple[bool, bool],
    lock: threading.Lock,
) -> None:
    rec = {
        "turn_id": turn_id,
        "direction": direction,
        "self_contained": parsed[0],
        "tier_match": parsed[1],
    }
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
# Existing-corpus dedupe (turn ids + MinHash-LSH near-dup vs the T5 corpus)
# --------------------------------------------------------------------------- #


def load_existing_corpus_ids(path: Path) -> tuple[set[str], set[str]]:
    """Return (existing turn_ids, existing conversation_ids) from the T5 corpus.
    Empty sets when no prior corpus exists."""
    turn_ids: set[str] = set()
    conv_ids: set[str] = set()
    if not path.is_file():
        return turn_ids, conv_ids
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "turn_id" in row:
                turn_ids.add(str(row["turn_id"]))
            if "conversation_id" in row:
                conv_ids.add(str(row["conversation_id"]))
    return turn_ids, conv_ids


def build_existing_lsh(path: Path, *, threshold: float = 0.85) -> Any:
    """Build a MinHash-LSH index over the EXISTING corpus texts so mined
    candidates can be near-dup checked against what T5 already sampled. Returns
    ``None`` when there is no prior corpus (nothing to dedupe against)."""
    if not path.is_file():
        return None
    from datasketch import MinHash, MinHashLSH

    lsh = MinHashLSH(threshold=threshold, num_perm=MINHASH_PERMS)
    with path.open() as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            mh = MinHash(num_perm=MINHASH_PERMS)
            for sh in _shingles(row["text"]):
                mh.update(sh)
            lsh.insert(f"existing:{row.get('turn_id', i)}", mh)
    return lsh


def _minhash(text: str) -> Any:
    from datasketch import MinHash

    mh = MinHash(num_perm=MINHASH_PERMS)
    for sh in _shingles(text):
        mh.update(sh)
    return mh


def is_novel(
    turn: dict[str, Any],
    *,
    existing_turn_ids: set[str],
    existing_conv_ids: set[str],
    existing_lsh: Any,
    seen_lsh: Any,
) -> bool:
    """Whether ``turn`` is NEW relative to the T5 corpus AND to already-accepted
    mined candidates. Drops it if:

    - its turn_id or conversation_id is already in the T5 corpus (we mine NEW
      conversations only — a whole conversation already sampled is skipped); or
    - it near-duplicates an existing corpus text; or
    - it near-duplicates a candidate already accepted this run (``seen_lsh``).

    ``seen_lsh`` is mutated (the turn is inserted) when the turn is novel so the
    next call dedupes against it."""
    tid = str(turn["turn_id"])
    cid = str(turn["conversation_id"])
    if tid in existing_turn_ids or cid in existing_conv_ids:
        return False
    mh = _minhash(turn["text"])
    if existing_lsh is not None and existing_lsh.query(mh):
        return False
    if seen_lsh is not None and seen_lsh.query(mh):
        return False
    if seen_lsh is not None:
        seen_lsh.insert(f"mined:{tid}", mh)
    return True


# --------------------------------------------------------------------------- #
# Concurrent prescreen pass (network — not exercised in CI)
# --------------------------------------------------------------------------- #


def _targets_met(counts: dict[str, int], *, r3_target: int, r0_target: int) -> bool:
    return counts.get(R3_LIKE, 0) >= r3_target and counts.get(R0_LIKE, 0) >= r0_target


def run_prescreen(
    candidates: list[dict[str, Any]],
    client: PrescreenClient,
    cache: dict[str, tuple[bool, bool]],
    *,
    r3_target: int,
    r0_target: int,
    workers: int,
    cache_path: Path = CACHE_PATH,
) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
    """Prescreen ``candidates`` (each carries a ``direction``) concurrently until
    BOTH per-direction targets are met. Returns (accepted candidates in stable
    input order, number actually LLM-screened, per-direction accepted counts).

    A candidate whose direction target is already met is skipped without a model
    call (cost control). Cached verdicts cost no network call. The on-disk cache
    append is serialized. Mirrors T5's sliding-window drain so one slow request
    never stalls the pool."""
    cache_lock = threading.Lock()
    append_lock = threading.Lock()
    accept_counts: dict[str, int] = {d: 0 for d in DIRECTIONS}

    def _classify(cand: dict[str, Any]) -> tuple[str, tuple[bool, bool] | None]:
        tid = str(cand["turn_id"])
        if tid in cache:
            return tid, cache[tid]
        raw = client.complete(cand["text"], cand["direction"])
        parsed = parse_prescreen(raw)
        if parsed is not None:
            with cache_lock:
                cache[tid] = parsed
            _append_cache(cache_path, tid, cand["direction"], parsed, append_lock)
        return tid, parsed

    verdicts: dict[str, tuple[bool, bool] | None] = {}
    it = iter(candidates)
    inflight: dict[Any, dict[str, Any]] = {}
    window = max(workers * 3, 48)
    screened = 0
    stop = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            while not stop and len(inflight) < window:
                nxt = next(it, None)
                if nxt is None:
                    break
                # Skip candidates whose direction target is already satisfied.
                if accept_counts.get(nxt["direction"], 0) >= (
                    r3_target if nxt["direction"] == R3_LIKE else r0_target
                ):
                    continue
                inflight[pool.submit(_classify, nxt)] = nxt
            if not inflight:
                break
            done, _ = wait(set(inflight), return_when=FIRST_COMPLETED)
            for fut in done:
                cand = inflight.pop(fut)
                _, parsed = fut.result()
                screened += 1
                verdicts[str(cand["turn_id"])] = parsed
                if accepted_by_prescreen(parsed):
                    d = cand["direction"]
                    tgt = r3_target if d == R3_LIKE else r0_target
                    if accept_counts[d] < tgt:
                        accept_counts[d] += 1
                if screened % 200 == 0:
                    print(
                        f"[prescreen] screened={screened} accepted={accept_counts}",
                        flush=True,
                    )
            if _targets_met(accept_counts, r3_target=r3_target, r0_target=r0_target):
                stop = True
                break

    # Read accepted candidates back in stable input order, respecting targets.
    accepted: list[dict[str, Any]] = []
    kept: dict[str, int] = {d: 0 for d in DIRECTIONS}
    for cand in candidates:
        tid = str(cand["turn_id"])
        if tid not in verdicts:
            continue
        if not accepted_by_prescreen(verdicts[tid]):
            continue
        d = cand["direction"]
        tgt = r3_target if d == R3_LIKE else r0_target
        if kept[d] >= tgt:
            continue
        accepted.append(cand)
        kept[d] += 1
    print(f"[prescreen] final accepted={kept} screened={screened}", flush=True)
    return accepted, screened, kept


# --------------------------------------------------------------------------- #
# Orchestration (the real run)
# --------------------------------------------------------------------------- #


def run(
    *,
    r3_target: int,
    r0_target: int,
    screen_cap: int,
    dedup_threshold: float,
    client: PrescreenClient | None,
    prescreen_workers: int = 24,
) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    counts: Counter[str] = Counter()

    existing_turn_ids, existing_conv_ids = load_existing_corpus_ids(CORPUS_PATH)
    print(
        f"[mine] existing corpus: {len(existing_turn_ids)} turn_ids, "
        f"{len(existing_conv_ids)} conversation_ids",
        flush=True,
    )
    existing_lsh = build_existing_lsh(CORPUS_PATH, threshold=dedup_threshold)
    # A fresh LSH index over candidates accepted THIS run so we also dedupe
    # mined candidates against each other (not just against the T5 corpus).
    from datasketch import MinHashLSH

    seen_lsh = MinHashLSH(threshold=dedup_threshold, num_perm=MINHASH_PERMS)

    cache = _load_cache(CACHE_PATH)

    # Stages 1-5: stream + English + redaction + triviality + novelty + direction.
    candidates: list[dict[str, Any]] = []
    dir_counts: Counter[str] = Counter()
    for turn in sc._iter_user_turns(screen_cap):
        counts["screened"] += 1
        if not is_english(turn["text"], lang_meta=turn["language"]):
            continue
        counts["english"] += 1
        if not is_redaction_clean(turn["text"], redacted=turn["redacted"]):
            continue
        counts["redaction_clean"] += 1
        if not is_substantive(turn["text"]):
            continue
        counts["substantive"] += 1
        direction = direction_for(turn["text"])
        if direction is None:
            continue
        counts["direction_match"] += 1
        # Novelty last (MinHash is the most expensive per-turn cheap check).
        if not is_novel(
            turn,
            existing_turn_ids=existing_turn_ids,
            existing_conv_ids=existing_conv_ids,
            existing_lsh=existing_lsh,
            seen_lsh=seen_lsh,
        ):
            counts["dup_dropped"] += 1
            continue
        turn["direction"] = direction
        candidates.append(turn)
        dir_counts[direction] += 1
        if counts["screened"] % 10_000 == 0:
            print(
                f"[stream] screened={counts['screened']} candidates={len(candidates)} "
                f"{dict(dir_counts)}",
                flush=True,
            )
    counts["candidates"] = len(candidates)
    print(
        f"[stream] screened={counts['screened']} english={counts['english']} "
        f"substantive={counts['substantive']} candidates={len(candidates)} "
        f"{dict(dir_counts)}",
        flush=True,
    )

    # Stage 6: cheap-LLM prescreen (self-contained AND tier-plausible).
    if client is None:
        raise RuntimeError("no prescreen client provided for the real run")
    accepted, screened, kept = run_prescreen(
        candidates,
        client,
        cache,
        r3_target=r3_target,
        r0_target=r0_target,
        workers=prescreen_workers,
    )
    counts["prescreen_screened"] = screened
    counts["accepted"] = len(accepted)

    # Stage 7: assign split + category, write candidates. Split is the frozen T5
    # partition (assign_split) — recorded now, ENFORCED at label/merge time (only
    # train-assigned rows join the training corpus; §6.2).
    per_split: Counter[str] = Counter()
    with CANDIDATES_PATH.open("w") as fh:
        for cand in accepted:
            split = assign_split(cand["conversation_id"])
            row = {
                "turn_id": str(cand["turn_id"]),
                "conversation_id": str(cand["conversation_id"]),
                "text": cand["text"],
                "category": categorize(cand["text"]),
                "direction": cand["direction"],
                "split": split,
            }
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            per_split[split] += 1

    meta: dict[str, Any] = {
        "dataset_id": DATASET_ID,
        "dataset_revision": DATASET_REVISION,
        "prescreen_model": PRESCREEN_MODEL,
        "prescreen_temperature": PRESCREEN_TEMPERATURE,
        "prescreen_prompt_version": PRESCREEN_PROMPT_VERSION,
        "seed": SEED,
        "r3_target": r3_target,
        "r0_target": r0_target,
        "screen_cap": screen_cap,
        "dedup_threshold": dedup_threshold,
        "minhash_num_perm": MINHASH_PERMS,
        "r3_min_chars": R3_MIN_CHARS,
        "r0_max_chars": R0_MAX_CHARS,
        "stage_counts": {
            "screened": counts["screened"],
            "english": counts["english"],
            "redaction_clean": counts["redaction_clean"],
            "substantive": counts["substantive"],
            "direction_match": counts["direction_match"],
            "dup_dropped": counts["dup_dropped"],
            "candidates": counts["candidates"],
            "prescreen_screened": counts["prescreen_screened"],
            "accepted": counts["accepted"],
        },
        "accepted_by_direction": {d: kept.get(d, 0) for d in DIRECTIONS},
        "candidates_by_direction": {d: dir_counts.get(d, 0) for d in DIRECTIONS},
        "accepted_split_sizes": {s: per_split.get(s, 0) for s in ("train", "val", "test")},
        "candidate_files": (
            {CANDIDATES_PATH.name: _sha256(CANDIDATES_PATH)} if CANDIDATES_PATH.is_file() else {}
        ),
    }
    if isinstance(client, OpenRouterPrescreenClient):
        reported = round(client.reported_cost_usd, 6) if client.reported_cost_usd else 0.0
        per_call = (reported / client.calls) if client.calls else 0.0
        meta["prescreen_usage"] = {
            "llm_calls_this_run": client.calls,
            "prompt_tokens_this_run": client.prompt_tokens,
            "completion_tokens_this_run": client.completion_tokens,
            "reported_cost_this_run_usd": reported,
            "measured_cost_per_call_usd": round(per_call, 6),
        }

    META_PATH.write_text(json.dumps(meta, indent=2) + "\n")
    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--r3-target", type=int, default=DEFAULT_R3_TARGET)
    parser.add_argument("--r0-target", type=int, default=DEFAULT_R0_TARGET)
    parser.add_argument("--screen-cap", type=int, default=DEFAULT_SCREEN_CAP)
    parser.add_argument("--dedup-threshold", type=float, default=0.85)
    parser.add_argument("--prescreen-workers", type=int, default=24)
    args = parser.parse_args(argv)

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("ERROR: OPENROUTER_API_KEY is required for the real run", file=sys.stderr)
        return 2

    client = OpenRouterPrescreenClient(api_key)
    try:
        meta = run(
            r3_target=args.r3_target,
            r0_target=args.r0_target,
            screen_cap=args.screen_cap,
            dedup_threshold=args.dedup_threshold,
            client=client,
            prescreen_workers=args.prescreen_workers,
        )
    finally:
        client.close()

    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
