#!/usr/bin/env python
"""Sample the Pilot router training corpus from WildChat-1M (spec §6.1/§6.3, T5).

**License gate:** see [`DATA.md`](DATA.md). Corpus = **WildChat-1M only**
(ODC-BY 1.0); LMSYS-Chat-1M is excluded by owner decision (2026-07-18). This
script contains no code path that downloads or references LMSYS.

Pipeline (each user turn evaluated independently — labels must be derivable
from the current message alone):

    stream WildChat  →  user turns  →  English  →  redaction-clean  →
    substantive (length)  →  MinHash near-dup dedupe  →
    LLM self-containment pre-filter  →  coarse category  →
    stratified ~8k  →  split BY conversation_id (70/15/15, frozen seed 42)

Outputs (under the git-ignored ``scripts/pilot_router/data/``):

- ``corpus.jsonl`` — one row per accepted turn:
  ``{turn_id, conversation_id, text, category, split}``. **Never committed.**
- ``corpus_meta.json`` — per-stage counts, per-category counts, split sizes,
  sha256(s), dataset revision, filter-model pin, seed. **Committed.**

The LLM self-containment check uses a pinned cheap model over OpenRouter
(``deepseek/deepseek-v4-flash``, temperature 0). Verdicts are cached on disk
keyed by turn id so a crash mid-pass does not restart from zero.

Dev-time only. Requires the ``pilot-train`` dependency group::

    OPENROUTER_API_KEY=... \\
      uv run --group pilot-train python scripts/pilot_router/sample_corpus.py \\
      --target 8000

A ``--screen-cap N`` smoke run validates the real HF + OpenRouter path
end-to-end on a tiny slice before the full run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Protocol

# --------------------------------------------------------------------------- #
# Pinned constants (reproducibility — mirrored in DATA.md)
# --------------------------------------------------------------------------- #

DATASET_ID = "allenai/WildChat-1M"
# Pinned snapshot (main @ this commit) recorded in the meta at run time; the
# HF API reports this sha for allenai/WildChat-1M.
DATASET_REVISION = "7d6490e462285cf85d91eabea0f9a954fbddcd1f"

FILTER_MODEL = "deepseek/deepseek-v4-flash"
FILTER_TEMPERATURE = 0.0
FILTER_MAX_TOKENS = 8
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SEED = 42
SPLIT_FRACTIONS = {"train": 0.70, "val": 0.15, "test": 0.15}
_BUCKETS = 10_000  # hash bucket resolution for the split
TARGET_TURNS = 8_000
MIN_CHARS = 12  # triviality floor

# Extension acceptance policy (owner-delegated, 2026-07-18). Screen up to
# ``SCREEN_CAP`` turns, but stop early once BOTH the total accepted target is
# met AND every non-rare category has reached its floor. ``tool_use`` is
# naturally rare in WildChat — take what we find down to a soft floor.
DEFAULT_SCREEN_CAP = 120_000
PER_CATEGORY_FLOOR = 400  # every category except tool_use
TOOL_USE_FLOOR = 60  # best-effort; accept fewer if 120k screened can't supply
# Once a category exceeds this share of the running accepted total, keep
# screening its turns but stop ACCEPTING from it (rebalance away from the
# dominant factual_qa bucket). The cheap regex category check runs BEFORE the
# LLM self-containment call, so capped-category turns cost no LLM spend.
CATEGORY_SHARE_CAP = 0.35

CATEGORIES = (
    "chitchat",
    "factual_qa",
    "writing",
    "coding",
    "math_reasoning",
    "tool_use",
)

# Versioned prompt text (DATA.md pins this id).
SELF_CONTAINMENT_PROMPT_V1 = (
    "You are a data-filtering classifier. Decide whether a single user message "
    "from a chat is INTERPRETABLE ON ITS OWN, i.e. a reader could act on it "
    "without seeing any earlier messages in the conversation.\n\n"
    "SELF-CONTAINED (true): concrete standalone requests such as "
    "'write a retry decorator with exponential backoff in Python', "
    "'what is the capital of France', 'summarize the theory of relativity'.\n"
    "REFERENTIAL (false): messages that depend on prior context, such as "
    "'now also add retry logic to that function', 'yes do that', "
    "'the second one', 'make it shorter', 'continue'.\n\n"
    "Reply with ONLY a JSON object: {\"self_contained\": true} or "
    "{\"self_contained\": false}. No other text."
)

DATA_DIR = Path(__file__).resolve().parent / "data"
CORPUS_PATH = DATA_DIR / "corpus.jsonl"
META_PATH = Path(__file__).resolve().parent / "corpus_meta.json"
CACHE_PATH = DATA_DIR / "self_containment_cache.jsonl"


# --------------------------------------------------------------------------- #
# Partition (frozen — spec §6.2). Pure function of conversation_id + seed.
# --------------------------------------------------------------------------- #


def _bucket(conversation_id: str) -> int:
    """Stable hash bucket in [0, _BUCKETS) for a conversation id.

    Uses blake2b (stable across processes/platforms, unlike ``hash()``) keyed
    with the fixed SEED so the mapping is frozen.
    """
    digest = hashlib.blake2b(
        conversation_id.encode("utf-8"), digest_size=8, key=str(SEED).encode("utf-8")
    ).digest()
    return int.from_bytes(digest, "big") % _BUCKETS


def _split_for_bucket(bucket: int) -> str:
    """Map a bucket to a split using the frozen 70/15/15 boundaries."""
    train_hi = int(SPLIT_FRACTIONS["train"] * _BUCKETS)  # 7000
    val_hi = train_hi + int(SPLIT_FRACTIONS["val"] * _BUCKETS)  # 8500
    if bucket < train_hi:
        return "train"
    if bucket < val_hi:
        return "val"
    return "test"


def assign_split(conversation_id: str) -> str:
    """Deterministic train/val/test assignment for a conversation id.

    Frozen: a pure function of (conversation_id, SEED). Later supplemental
    sampling cannot move an already-assigned conversation across the boundary.
    All turns of one conversation share one split.
    """
    return _split_for_bucket(_bucket(conversation_id))


def _assert_partition_stable(prior_corpus: Path) -> int:
    """Assert the frozen partition (spec §6.2): every conversation_id recorded
    in a previously-written corpus still maps to the SAME split under
    ``assign_split`` now. Returns the number of ids checked (0 if no prior
    corpus). Raises AssertionError on any drift."""
    if not prior_corpus.is_file():
        return 0
    checked = 0
    with prior_corpus.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                cid, prev = row["conversation_id"], row["split"]
            except (json.JSONDecodeError, KeyError):
                continue
            now = assign_split(cid)
            if now != prev:
                raise AssertionError(
                    f"partition drift: conversation {cid!r} was {prev!r}, now {now!r}"
                )
            checked += 1
    print(f"[partition] stable: {checked} prior conversation_ids keep their split")
    return checked


# --------------------------------------------------------------------------- #
# Turn filters
# --------------------------------------------------------------------------- #


def is_english(text: str, *, lang_meta: str | None) -> bool:
    """English check. Trust WildChat's per-message ``language`` metadata when
    present; fall back to ``langdetect`` only when it is missing."""
    if lang_meta:
        return lang_meta.strip().lower() == "english"
    try:
        from langdetect import DetectorFactory, detect

        DetectorFactory.seed = SEED
        return bool(detect(text) == "en")
    except Exception:
        # No detector or undetectable text -> conservatively not English.
        return False


_PLACEHOLDER_RE = re.compile(r"\[[A-Z][A-Z0-9_]{2,}\]")


def is_redaction_clean(text: str, *, redacted: bool) -> bool:
    """Drop turns WildChat flagged ``redacted`` or that still carry a
    redaction placeholder like ``[EMAIL]`` / ``[PHONE_NUMBER]``."""
    if redacted:
        return False
    return _PLACEHOLDER_RE.search(text) is None


def is_substantive(text: str) -> bool:
    """Drop empty / whitespace-only / trivially short anomalies."""
    return len(text.strip()) >= MIN_CHARS


# --------------------------------------------------------------------------- #
# Near-dup dedupe (MinHash + LSH)
# --------------------------------------------------------------------------- #

MINHASH_PERMS = 64
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _shingles(text: str, k: int = 4) -> set[bytes]:
    tokens = _TOKEN_RE.findall(text.lower())
    if len(tokens) < k:
        return {" ".join(tokens).encode("utf-8")} if tokens else {b""}
    return {" ".join(tokens[i : i + k]).encode("utf-8") for i in range(len(tokens) - k + 1)}


def dedupe_near_duplicates(
    rows: list[dict[str, Any]], *, threshold: float = 0.85
) -> list[dict[str, Any]]:
    """Collapse near-identical turns via MinHash-LSH; keep first-seen."""
    from datasketch import MinHash, MinHashLSH

    lsh = MinHashLSH(threshold=threshold, num_perm=MINHASH_PERMS)
    kept: list[dict[str, Any]] = []
    for row in rows:
        mh = MinHash(num_perm=MINHASH_PERMS)
        for sh in _shingles(row["text"]):
            mh.update(sh)
        if lsh.query(mh):
            continue  # near-duplicate of something already kept
        lsh.insert(str(row["turn_id"]), mh)
        kept.append(row)
    return kept


# --------------------------------------------------------------------------- #
# Coarse category heuristic
# --------------------------------------------------------------------------- #

_CODE_RE = re.compile(
    r"```|def |class |import |function|const |=>|</|SELECT |printf|console\.log|"
    r"public static|#include|std::|traceback|stack ?trace|npm |pip install|"
    r"\bpython\b|\bjavascript\b|\bjava\b|\bc\+\+\b|\bsql\b|\bregex\b|\bapi\b",
    re.IGNORECASE,
)
_MATH_RE = re.compile(
    r"\bintegral\b|\bderivative\b|\bequation\b|\bsolve\b|\bcalculate\b|"
    r"\bcompute\b|\bprobability\b|\btheorem\b|\bproof\b|\bmatrix\b|"
    r"step[- ]by[- ]step|\d+\s*[+\-*/^]\s*\d+",
    re.IGNORECASE,
)
_WRITING_RE = re.compile(
    r"\bwrite (a|me|an)\b.*\b(poem|story|essay|haiku|song|email|letter|"
    r"paragraph|article|blog|script|tweet)\b|\bcompose\b|\brewrite\b|"
    r"\bparaphrase\b|\bdraft\b",
    re.IGNORECASE,
)
_TOOL_RE = re.compile(
    r"\bsearch (the |)(web|internet|online)\b|\bgoogle\b|\blook up\b|"
    r"\bbrowse\b|\bfetch\b.*\burl\b|\bcurrent weather\b|\blatest news\b|"
    r"\brun (this |the |)command\b|\bcall (the |)api\b",
    re.IGNORECASE,
)
_QA_RE = re.compile(
    r"^\s*(what|who|when|where|which|why|how|is|are|does|do|can|could|"
    r"define|explain|describe|list)\b",
    re.IGNORECASE,
)
_CHITCHAT_RE = re.compile(
    r"^\s*(hi|hey|hello|yo|sup|how are you|how's it going|good morning|"
    r"good evening|thanks|thank you|lol|haha|nice|cool|ok|okay)\b",
    re.IGNORECASE,
)


def categorize(text: str) -> str:
    """Cheap deterministic coarse category. Order matters: the more specific /
    higher-signal patterns win before the generic Q&A / chitchat catch-alls."""
    t = text.strip()
    if _CODE_RE.search(t):
        return "coding"
    if _MATH_RE.search(t):
        return "math_reasoning"
    if _WRITING_RE.search(t):
        return "writing"
    if _TOOL_RE.search(t):
        return "tool_use"
    if _CHITCHAT_RE.search(t):
        return "chitchat"
    if _QA_RE.search(t):
        return "factual_qa"
    # Default bucket: short greetings-ish -> chitchat, else factual_qa.
    return "chitchat" if len(t) < 40 else "factual_qa"


# --------------------------------------------------------------------------- #
# Self-containment LLM filter (mockable client)
# --------------------------------------------------------------------------- #


class SelfContainmentClient(Protocol):
    def complete(self, text: str) -> str: ...


_YES_RE = re.compile(r"\byes\b|\btrue\b", re.IGNORECASE)
_NO_RE = re.compile(r"\bno\b|\bfalse\b", re.IGNORECASE)


def _parse_verdict(raw: str) -> bool:
    """Parse the model reply into a boolean. Tries strict JSON first, then a
    loose yes/no scan. Anything unparseable -> False (conservative: a turn we
    cannot verify as standalone is dropped)."""
    raw = (raw or "").strip()
    if not raw:
        return False
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and "self_contained" in obj:
            return bool(obj["self_contained"])
    except (json.JSONDecodeError, ValueError):
        pass
    # Loose fallback: explicit yes/true beats no/false; require a clear signal.
    has_yes = _YES_RE.search(raw) is not None
    has_no = _NO_RE.search(raw) is not None
    if has_yes and not has_no:
        return True
    if has_no and not has_yes:
        return False
    return False


def check_self_contained(
    text: str,
    turn_id: str,
    client: SelfContainmentClient,
    cache: dict[str, bool],
) -> bool:
    """Return whether ``text`` is interpretable standalone, using ``cache``
    keyed by turn id to avoid re-calling the model."""
    if turn_id in cache:
        return cache[turn_id]
    verdict = _parse_verdict(client.complete(text))
    cache[turn_id] = verdict
    return verdict


# --------------------------------------------------------------------------- #
# Stratified selection
# --------------------------------------------------------------------------- #


def stratified_select(
    rows: list[dict[str, Any]], *, target: int
) -> list[dict[str, Any]]:
    """Select up to ``target`` rows with balanced category coverage.

    Round-robin across categories (rows within a category kept in stable
    input order) so every category is represented as evenly as the supply
    allows. Deterministic. If supply < target, returns all rows."""
    if len(rows) <= target:
        return list(rows)
    by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_cat[row["category"]].append(row)
    cats = [c for c in CATEGORIES if by_cat[c]]
    selected: list[dict[str, Any]] = []
    idx = {c: 0 for c in cats}
    while len(selected) < target and cats:
        for c in list(cats):
            if len(selected) >= target:
                break
            pool = by_cat[c]
            if idx[c] < len(pool):
                selected.append(pool[idx[c]])
                idx[c] += 1
            else:
                cats.remove(c)
    return selected


# --------------------------------------------------------------------------- #
# Real OpenRouter client (network — not exercised in CI)
# --------------------------------------------------------------------------- #


class OpenRouterClient:
    """Pinned deepseek-v4-flash self-containment classifier over OpenRouter."""

    def __init__(self, api_key: str) -> None:
        import threading

        import httpx

        # A pooled client shared across worker threads (httpx.Client is
        # thread-safe for concurrent requests).
        self._client = httpx.Client(
            timeout=30.0, limits=httpx.Limits(max_connections=64)
        )
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._usage_lock = threading.Lock()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.reported_cost_usd = 0.0
        self.calls = 0

    def complete(self, text: str) -> str:
        payload = {
            "model": FILTER_MODEL,
            "temperature": FILTER_TEMPERATURE,
            "max_tokens": FILTER_MAX_TOKENS,
            "messages": [
                {"role": "system", "content": SELF_CONTAINMENT_PROMPT_V1},
                {"role": "user", "content": text[:4000]},
            ],
        }
        for attempt in range(4):
            try:
                resp = self._client.post(
                    OPENROUTER_URL, headers=self._headers, json=payload
                )
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
# Disk cache (idempotent / resumable)
# --------------------------------------------------------------------------- #


def _load_cache(path: Path) -> dict[str, bool]:
    cache: dict[str, bool] = {}
    if path.is_file():
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    cache[str(rec["turn_id"])] = bool(rec["self_contained"])
                except (json.JSONDecodeError, KeyError):
                    continue
    return cache


def _append_cache(path: Path, turn_id: str, verdict: bool) -> None:
    with path.open("a") as fh:
        fh.write(json.dumps({"turn_id": turn_id, "self_contained": verdict}) + "\n")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Streaming candidate extraction (network — not exercised in CI)
# --------------------------------------------------------------------------- #


def _iter_user_turns(screen_cap: int | None):
    """Yield candidate user turns from the WildChat stream as dicts:
    ``{turn_id, conversation_id, text, language, redacted}``."""
    from datasets import load_dataset

    ds = load_dataset(
        DATASET_ID, split="train", streaming=True, revision=DATASET_REVISION
    )
    seen = 0
    for conv in ds:
        cid = conv.get("conversation_hash") or conv.get("conversation_id") or ""
        messages = conv.get("conversation") or []
        for msg in messages:
            if (msg.get("role") or "").lower() != "user":
                continue
            content = msg.get("content") or ""
            if not content.strip():
                continue
            turn_id = str(msg.get("turn_identifier") or f"{cid}:{seen}")
            yield {
                "turn_id": turn_id,
                "conversation_id": str(cid),
                "text": content,
                "language": msg.get("language"),
                "redacted": bool(msg.get("redacted", False)),
            }
            seen += 1
            if screen_cap is not None and seen >= screen_cap:
                return


# --------------------------------------------------------------------------- #
# Concurrent LLM filter pass (network — not exercised in CI)
# --------------------------------------------------------------------------- #


def _run_llm_filter(
    deduped: list[dict[str, Any]],
    client: SelfContainmentClient,
    cache: dict[str, bool],
    *,
    accept_pool: int,
    workers: int,
) -> tuple[list[dict[str, Any]], int]:
    """Screen ``deduped`` turns through the self-containment LLM concurrently,
    stopping once ``accept_pool`` turns have been accepted. Returns
    (accepted turns in stable input order, number of turns actually screened).

    Cached verdicts are consumed without a network call; only uncached turns
    hit the pool. The on-disk cache append is serialized by a lock so the
    resumable log stays consistent."""
    import threading
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    cache_lock = threading.Lock()

    def _classify(turn: dict[str, Any]) -> tuple[str, bool]:
        tid = turn["turn_id"]
        if tid in cache:
            return tid, cache[tid]
        verdict = _parse_verdict(client.complete(turn["text"]))
        with cache_lock:
            cache[tid] = verdict
            _append_cache(CACHE_PATH, tid, verdict)
        return tid, verdict

    # Submit a sliding window of futures (bounded so we never enqueue the whole
    # corpus) and drain them as they complete — a single slow request cannot
    # stall the others. We stop submitting once ``accept_pool`` turns are
    # accepted; turns past that point are never sent to the model (cost/early
    # stop). Verdicts are collected by turn id, then accepted turns are read
    # back in stable input order.
    verdicts: dict[str, bool] = {}
    it = iter(deduped)
    inflight: set[Any] = set()
    accepted_count = 0
    done_calls = 0
    window = max(workers * 3, 48)
    stop_submitting = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            while not stop_submitting and len(inflight) < window:
                nxt = next(it, None)
                if nxt is None:
                    break
                inflight.add(pool.submit(_classify, nxt))
            if not inflight:
                break
            done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
            for fut in done:
                tid, verdict = fut.result()
                verdicts[tid] = verdict
                done_calls += 1
                if verdict:
                    accepted_count += 1
                if done_calls % 500 == 0:
                    print(f"[llm] screened={done_calls} accepted={accepted_count}")
            if accepted_count >= accept_pool:
                stop_submitting = True
                if not inflight:
                    break

    # Read accepted turns back in stable input order; early-stop at accept_pool.
    accepted: list[dict[str, Any]] = []
    screened = 0
    for turn in deduped:
        tid = turn["turn_id"]
        if tid not in verdicts:
            continue  # not screened (submission stopped before reaching it)
        screened += 1
        if verdicts[tid]:
            accepted.append(turn)
            if len(accepted) >= accept_pool:
                break
    return accepted, screened


def _quota_full(counts: dict[str, int], *, target: int) -> bool:
    """The compound stop condition for the stratified extension: total accepted
    at/over ``target`` AND every non-rare category at its floor (tool_use at its
    soft floor)."""
    total = sum(counts.values())
    if total < target:
        return False
    for cat in CATEGORIES:
        floor = TOOL_USE_FLOOR if cat == "tool_use" else PER_CATEGORY_FLOOR
        if counts.get(cat, 0) < floor:
            return False
    return True


def _category_capped(cat: str, counts: dict[str, int]) -> bool:
    """Whether we should stop ACCEPTING new turns from ``cat`` right now: its
    running accepted share exceeds ``CATEGORY_SHARE_CAP``. Only applies once its
    floor is met, so a category is never starved below its floor by the cap."""
    total = sum(counts.values())
    if total == 0:
        return False
    floor = TOOL_USE_FLOOR if cat == "tool_use" else PER_CATEGORY_FLOOR
    if counts.get(cat, 0) < floor:
        return False
    return counts.get(cat, 0) > CATEGORY_SHARE_CAP * total


def _run_llm_filter_stratified(
    deduped: list[dict[str, Any]],
    client: SelfContainmentClient,
    cache: dict[str, bool],
    *,
    target: int,
    workers: int,
) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
    """Category-aware self-containment filter for the extension run.

    Each turn is categorized with the cheap regex classifier BEFORE any LLM
    call. Turns whose category is currently share-capped
    (``_category_capped``) are skipped without a model call — no LLM spend on
    turns we would not keep. Uncapped turns are screened concurrently; accepted
    ones (self-contained) are collected until ``_quota_full`` (total ``target``
    + per-category floors). Returns (accepted turns in stable input order,
    number of turns actually LLM-screened, per-category accepted counts).

    Verdicts are disk-cached/resumable exactly as in ``_run_llm_filter``."""
    import threading
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    cache_lock = threading.Lock()

    def _classify(turn: dict[str, Any]) -> tuple[str, bool]:
        tid = turn["turn_id"]
        if tid in cache:
            return tid, cache[tid]
        verdict = _parse_verdict(client.complete(turn["text"]))
        with cache_lock:
            cache[tid] = verdict
            _append_cache(CACHE_PATH, tid, verdict)
        return tid, verdict

    # Pre-categorize; keep only what we might still accept as we go.
    for turn in deduped:
        turn["category"] = categorize(turn["text"])

    accepted: list[dict[str, Any]] = []
    accept_counts: dict[str, int] = {c: 0 for c in CATEGORIES}
    screened = 0
    it = iter(deduped)
    inflight: dict[Any, dict[str, Any]] = {}
    window = max(workers * 3, 48)
    stop = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            # Fill the window, skipping turns whose category is share-capped
            # right now (no LLM call for them).
            while not stop and len(inflight) < window:
                nxt = next(it, None)
                if nxt is None:
                    break
                if _category_capped(nxt["category"], accept_counts):
                    continue
                inflight[pool.submit(_classify, nxt)] = nxt
            if not inflight:
                break
            done, _ = wait(set(inflight), return_when=FIRST_COMPLETED)
            for fut in done:
                turn = inflight.pop(fut)
                _, verdict = fut.result()
                screened += 1
                cat = turn["category"]
                # Re-check the cap at accept time (it may have filled while the
                # call was in flight) so a category never overshoots its share.
                if verdict and not _category_capped(cat, accept_counts):
                    accepted.append(turn)
                    accept_counts[cat] += 1
                if screened % 500 == 0:
                    print(f"[llm] screened={screened} accepted={len(accepted)} {accept_counts}")
            if _quota_full(accept_counts, target=target):
                stop = True
                break
    print(f"[llm] final accepted={len(accepted)} {accept_counts}")
    return accepted, screened, accept_counts


# --------------------------------------------------------------------------- #
# Orchestration (the real run)
# --------------------------------------------------------------------------- #


def run(
    *,
    target: int,
    screen_cap: int | None,
    dedup_threshold: float,
    client: SelfContainmentClient | None,
    accept_pool_factor: float = 1.5,
    llm_workers: int = 24,
    stratified: bool = False,
) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = Counter()
    cache = _load_cache(CACHE_PATH)

    # Stages 1-3: stream + language + redaction + triviality.
    filtered: list[dict[str, Any]] = []
    for turn in _iter_user_turns(screen_cap):
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
        filtered.append(turn)
    print(
        f"[stream] screened={counts['screened']} english={counts['english']} "
        f"redaction_clean={counts['redaction_clean']} substantive={counts['substantive']}"
    )

    # Stage 4: near-dup dedupe.
    deduped = dedupe_near_duplicates(filtered, threshold=dedup_threshold)
    counts["deduped"] = len(deduped)
    print(f"[dedupe] {counts['substantive']} -> {counts['deduped']}")

    # Stage 5: LLM self-containment. Concurrent for throughput; verdicts are
    # cached on disk keyed by turn id (resumable). We only need enough accepted
    # turns to stratify to ``target``, so we stop early once the accepted pool
    # reaches ``accept_pool`` (target with headroom for stratification) — this
    # avoids paying for LLM calls on turns we would never select.
    if client is None:
        raise RuntimeError("no self-containment client provided for the real run")
    if stratified:
        # Extension mode: category-aware acceptance with per-category floors +
        # share cap, stopping on the compound quota. Categories are assigned in
        # the filter (before the LLM call) so capped categories cost no spend.
        accepted, screened, _ = _run_llm_filter_stratified(
            deduped, client, cache, target=target, workers=llm_workers
        )
        final = accepted  # already balanced by the quota controller
    else:
        accept_pool = int(target * accept_pool_factor)
        accepted, screened = _run_llm_filter(
            deduped, client, cache, accept_pool=accept_pool, workers=llm_workers
        )
        # Stage 6: coarse category.
        for turn in accepted:
            turn["category"] = categorize(turn["text"])
        # Stage 7: stratified selection to target.
        final = stratified_select(accepted, target=target)
    counts["llm_screened"] = screened  # turns actually LLM-screened (early-stopped)
    counts["self_contained"] = len(accepted)
    counts["final"] = len(final)
    print(f"[llm] self_contained={counts['self_contained']}/{screened} screened")

    # Stage 8: split by conversation_id + write corpus.
    # Partition stability check (spec §6.2): the split is a pure function of
    # (conversation_id, SEED), so any conversation that appeared in a previous
    # corpus MUST keep its exact split now. Cross-check against the prior corpus
    # if one is still on disk; this proves the extension moved nothing.
    _assert_partition_stable(CORPUS_PATH)

    per_cat: Counter[str] = Counter()
    split_conv: dict[str, set[str]] = {s: set() for s in SPLIT_FRACTIONS}
    split_turns: Counter[str] = Counter()
    with CORPUS_PATH.open("w") as fh:
        for turn in final:
            split = assign_split(turn["conversation_id"])
            row = {
                "turn_id": turn["turn_id"],
                "conversation_id": turn["conversation_id"],
                "text": turn["text"],
                "category": turn["category"],
                "split": split,
            }
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            per_cat[turn["category"]] += 1
            split_conv[split].add(turn["conversation_id"])
            split_turns[split] += 1

    meta: dict[str, Any] = {
        "dataset_id": DATASET_ID,
        "dataset_revision": DATASET_REVISION,
        "filter_model": FILTER_MODEL,
        "filter_temperature": FILTER_TEMPERATURE,
        "filter_prompt_version": "SELF_CONTAINMENT_PROMPT_V1",
        "seed": SEED,
        "split_fractions": SPLIT_FRACTIONS,
        "dedup_threshold": dedup_threshold,
        "minhash_num_perm": MINHASH_PERMS,
        "target_turns": target,
        "screen_cap": screen_cap,
        "stage_counts": {
            "screened": counts["screened"],
            "english": counts["english"],
            "redaction_clean": counts["redaction_clean"],
            "substantive": counts["substantive"],
            "deduped": counts["deduped"],
            "llm_screened": counts["llm_screened"],
            "self_contained": counts["self_contained"],
            "final": counts["final"],
        },
        "category_counts": {c: per_cat.get(c, 0) for c in CATEGORIES},
        "split_sizes": {
            s: {"conversations": len(split_conv[s]), "turns": split_turns[s]}
            for s in SPLIT_FRACTIONS
        },
        "corpus_files": {CORPUS_PATH.name: _sha256(CORPUS_PATH)},
    }
    if isinstance(client, OpenRouterClient):
        prompt_t = client.prompt_tokens
        completion_t = client.completion_tokens
        # OpenRouter reports a per-call ``cost`` in the usage block; we sum it
        # for the calls made *this* process. The true per-call cost derived from
        # this run then scales to the whole filter pass (total_calls = unique
        # deduped turns screened, incl. those served from cache in a resume run).
        reported = round(client.reported_cost_usd, 6) if client.reported_cost_usd else 0.0
        total_calls = counts["llm_screened"]
        per_call = (reported / client.calls) if client.calls else 0.0
        meta["filter_usage"] = {
            "llm_calls_this_run": client.calls,
            "llm_calls_total_pass": total_calls,
            "prompt_tokens_this_run": prompt_t,
            "completion_tokens_this_run": completion_t,
            "reported_cost_this_run_usd": reported,
            "measured_cost_per_call_usd": round(per_call, 6),
            "estimated_total_cost_usd": round(per_call * total_calls, 4),
        }

    META_PATH.write_text(json.dumps(meta, indent=2) + "\n")
    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=int, default=TARGET_TURNS)
    parser.add_argument(
        "--screen-cap",
        type=int,
        default=None,
        help="cap the number of user turns pulled from the stream (smoke runs)",
    )
    parser.add_argument("--dedup-threshold", type=float, default=0.85)
    parser.add_argument(
        "--accept-pool-factor",
        type=float,
        default=1.5,
        help="LLM-screen until accepted >= target*factor, then stop (cost control)",
    )
    parser.add_argument("--llm-workers", type=int, default=24)
    parser.add_argument(
        "--stratified",
        action="store_true",
        help=(
            "category-aware acceptance: per-category floors + share cap, stop on "
            "the compound quota (total target + floors). Screens up to --screen-cap."
        ),
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("ERROR: OPENROUTER_API_KEY is required for the real run", file=sys.stderr)
        return 2

    client = OpenRouterClient(api_key)
    try:
        meta = run(
            target=args.target,
            screen_cap=args.screen_cap,
            dedup_threshold=args.dedup_threshold,
            client=client,
            accept_pool_factor=args.accept_pool_factor,
            llm_workers=args.llm_workers,
            stratified=args.stratified,
        )
    finally:
        client.close()

    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
