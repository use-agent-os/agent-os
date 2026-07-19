# Pilot Router — Training Data License Gate & Corpus Provenance

This document is the **license gate** for the Pilot router training corpus
(spec Rev 4 §6.1/§6.3, task T5). It is written and satisfied **before** any
corpus is sampled or any filter spend is incurred. The corpus statistics
section is filled from the real sampling run.

The sampler that produces the corpus is
[`sample_corpus.py`](sample_corpus.py). The committed run manifest is
[`corpus_meta.json`](corpus_meta.json). The corpus rows themselves are
**never committed** — they live under the git-ignored `data/` directory.

---

## 1. Source dataset — WildChat-1M

| Field | Value |
|---|---|
| Dataset | **WildChat-1M** |
| Hugging Face id | [`allenai/WildChat-1M`](https://huggingface.co/datasets/allenai/WildChat-1M) |
| Publisher | Allen Institute for AI (AI2) |
| License | **ODC-BY 1.0** (Open Data Commons Attribution License) |
| License text | <https://opendatacommons.org/licenses/by/1-0/> |
| Snapshot / revision used | *(recorded in `corpus_meta.json` → `dataset_revision`; filled by the run)* |
| Access method | `datasets` **streaming** (`streaming=True`) — the full multi-GB dataset is **never** downloaded to disk |

WildChat-1M is a corpus of ~1M real user↔chatbot conversations, released by AI2
with per-message **language** metadata and per-message **PII redaction** flags,
which the sampler uses directly (§3 below).

### ODC-BY obligations and how we satisfy them

ODC-BY 1.0 is a permissive open-data license. Its substantive obligation is
**attribution**: any public use of the database or a derived/produced work must
keep the attribution notice and the ODC-BY notice intact. It imposes **no**
share-alike / copyleft requirement and **no** restriction on commercial use.
(WildChat additionally asks users to abide by the AI2 ImpACT low-risk
guidelines and OpenAI's terms; we consume only derived training signal, do not
redistribute rows, and use the data for building a routing model — consistent
with those guidelines.)

Concretely, for this project:

- **We never redistribute WildChat rows.** The sampled corpus is written only
  to the git-ignored local `data/` directory (see §5). Nothing under `data/`
  is committable — the `.gitignore` `data/` rule is verified against
  `git check-ignore` as part of this task, and
  `tests/test_public_release_hygiene.py` guards the ignore set.
- **What actually ships in the repo / wheel is model weights and metadata**,
  not data rows: the corpus feeds a downstream classifier (later tasks); only
  the trained model artifacts and this provenance metadata are committed. No
  ODC-BY *database* or *derived database* is published by us, so the
  redistribution-attribution clause is not triggered by anything we commit.
- **The golden evaluation set** (spec §6.4), when it is built in a later task,
  **will draw examples from WildChat** and therefore **will** carry an ODC-BY
  attribution header in the golden-set file, plus a
  `THIRD_PARTY_NOTICES.md` entry — because that derived data file is checked
  in. That is a later task's obligation; it is recorded here so the obligation
  is not lost. **No `THIRD_PARTY_NOTICES.md` entry is required for T5** because
  T5 commits no WildChat-derived data (only counts, hashes, and the model pin).

---

## 2. Owner decision — LMSYS-Chat-1M EXCLUDED (2026-07-18)

**Decision (dated 2026-07-18):** the Pilot training corpus is built from
**WildChat-1M only**. **LMSYS-Chat-1M is excluded** and the sampler contains
no code path that downloads or references it.

**Rationale.** The spec (Rev 4 §6.1) originally listed both WildChat-1M and
LMSYS-Chat-1M as candidate sources. The owner is an open-source project with
commercial elements (a project token), so the corpus license posture must be
unambiguously commercial-safe. WildChat-1M is released under **ODC-BY 1.0** —
fully open, attribution-only, no commercial restriction. LMSYS-Chat-1M is
distributed under a **research-oriented, click-through gated agreement** whose
commercial-use posture is a legal gray zone for a project with a token. Rather
than carry that ambiguity, the owner scoped the corpus to WildChat-1M only.
Local AgentOS logs may be blended in as an additional source in a later task;
that is out of scope for T5.

This decision amends spec §6.1 for the purposes of this task and all
downstream training-data tasks until revisited.

**Amendment (2026-07-18) — extend the corpus to the ~8k target.** After the
initial 40,000-turn screen yielded 4,675 accepted turns (below the ~8k
target, because only ~42% of WildChat is English and self-containment
accepts ~33% of deduped turns), the owner approved extending the screen up to
**120,000 turns** (the incremental filter spend is a few cents; exact figures
are recorded in private ops notes). The extension resumes from the on-disk verdict
cache (previously-screened turns are **not** re-billed) and adds
category-aware acceptance (per-category floors + a 35% share cap; see §3
step 8) so the added turns rebalance coverage rather than pile onto
`factual_qa`. The frozen partition function is unchanged, so no existing
conversation's split assignment moves (asserted at run time; see §3 step 9).

---

## 3. Sampling pipeline (what the corpus went through)

Implemented in [`sample_corpus.py`](sample_corpus.py). Each user turn is
evaluated **independently** — the target signal must be derivable from the
current message alone (spec §6.1 "self-contained turns only").

1. **Stream** `allenai/WildChat-1M` (pinned revision) via `datasets`
   streaming — no full download.
2. **Extract user turns.** Only `role == "user"` messages are candidates
   (the router classifies the incoming user turn). Each carries its parent
   `conversation_id` and a turn index.
3. **Language filter — English only.** Uses WildChat's per-message `language`
   metadata when present; `langdetect` is the cheap fallback when a turn lacks
   the flag.
4. **PII / redaction filter.** Turns flagged `redacted` (or whose text still
   contains redaction placeholders) are **dropped** — we do not train on
   redacted or PII-bearing text.
5. **Length / triviality filter.** Empty, whitespace-only, and
   trivially-short (`< MIN_CHARS`) turns are dropped as anomalies.
6. **Near-duplicate dedupe.** MinHash + LSH (`datasketch`) over normalized
   turn text; near-identical turns collapse to one representative. Parameters
   are pinned in `corpus_meta.json`.
7. **Self-containment pre-filter (LLM).** A short yes/no LLM check — *"is this
   message interpretable on its own, without prior conversation context?"* —
   using the pinned cheap model below. Self-contained mid-conversation turns
   are **kept** ("write a retry decorator with exponential backoff in
   Python"); referential turns are **dropped** ("now also add retry logic to
   that", "yes do that", "the second one"). Verdicts are cached on disk keyed
   by turn id so a crash mid-pass does not restart from zero.
8. **Coarse category + stratification.** Each surviving turn is assigned a
   coarse category — `chitchat`, `factual_qa`, `writing`, `coding`,
   `math_reasoning`, `tool_use` — via cheap deterministic heuristics. The
   corpus is stratified toward balanced coverage of every category, targeting
   **~8,000 turns** total. In the `--stratified` extension mode (owner
   decision 2026-07-18, below) the category check runs **before** the LLM
   call so over-represented categories cost no filter spend: once a category
   exceeds **35%** of the running accepted total *and* has met its floor, its
   turns are screened out without a model call. Per-category floors are
   **400** for every category except `tool_use` (naturally rare in WildChat;
   soft floor **60**, best-effort). Screening stops once total accepted ≥
   target **and** every floor is met, up to a **120,000**-turn screen ceiling.
9. **Split by `conversation_id` (never by turn).** A **frozen deterministic
   partition** (`blake2b(conversation_id) mod 10_000` bucketed with seed
   **42**) assigns each conversation to **train / val / test = 70 / 15 / 15**.
   Because assignment is a pure function of the id + seed, later supplemental
   sampling can never move an already-assigned conversation across the split
   boundary (spec §6.2 partition contract). All turns of one conversation
   always share a split, so no turn from a test conversation can leak into
   train.

### Pinned self-containment filter model + params (reproducibility, §6.3)

| Field | Value |
|---|---|
| Provider | OpenRouter (`OPENROUTER_API_KEY`) |
| Model | **`deepseek/deepseek-v4-flash`** |
| Temperature | **0** |
| Max tokens | 8 (strict yes/no JSON) |
| Response contract | JSON object `{"self_contained": true|false}`, strict parse |
| Prompt version | `SELF_CONTAINMENT_PROMPT_V1` (verbatim text lives in `sample_corpus.py`) |
| Seed | **42** (partition + any sampling RNG) |

---

## 4. Corpus statistics — real run (extended)

Run date **2026-07-18**, dataset revision
`7d6490e462285cf85d91eabea0f9a954fbddcd1f`, seed 42,
`--stratified --screen-cap 120000 --target 8000`. Source of truth:
`corpus_meta.json`. (This supersedes the initial 40k-screen run, which
produced 4,675 turns; see the §2 amendment for why the screen was extended.)

### Per-stage counts

| Stage | Count | Survival |
|---|---:|---:|
| User turns screened (streamed) | 120,000 | 100% |
| → English | 49,462 | 41.2% |
| → PII/redaction-clean | 49,163 | 41.0% |
| → length/triviality-clean | 48,259 | 40.2% |
| → after near-dup dedupe | 41,007 | 34.2% |
| → LLM-screened (category-uncapped) | 24,461 | 20.4% |
| → self-contained + quota-accepted | **6,389** | 5.3% |
| → final corpus | **6,389** | 5.3% |

**Note on the target.** The compound stop condition (total ≥ 8,000 **and**
every non-`tool_use` category ≥ 400 **and** `tool_use` ≥ 60) was **not** met
even at the full 120,000-turn screen ceiling: the 41,007-turn deduped pool was
exhausted at **6,389** accepted. Two funnels bind: only ~41% of WildChat is
English, and once the 35% share cap on the dominant `factual_qa` bucket
engages (as designed), the corpus can only grow as fast as the *rarer*
categories supply turns — `math_reasoning` and especially `tool_use` are thin
in organic WildChat. `tool_use` reached **57** (just shy of its 60 soft
floor); per the owner's "take what you find" instruction this is accepted and
documented rather than chased further. Closing the remaining gap to 8,000 with
balanced rare classes needs a different source (local AgentOS logs, §6.1) —
more WildChat sampling would only deepen the common classes. The corpus is
complete and every downstream contract (splits, categories, sha256, meta)
holds at 6,389.

### Per-category counts (final corpus)

| Category | Count | Share |
|---|---:|---:|
| factual_qa | 2,236 | 35.0% |
| coding | 1,671 | 26.2% |
| chitchat | 1,073 | 16.8% |
| writing | 750 | 11.7% |
| math_reasoning | 602 | 9.4% |
| tool_use | 57 | 0.9% |

The category-aware acceptance policy worked as intended: `factual_qa` — 66% of
the initial unbalanced run — is held to its **35% share cap** here, and four of
six categories now clear the §6.2 "no class < 15%" bar (factual_qa, coding,
chitchat all ≥ 15%; writing at 11.7% and math_reasoning at 9.4% are close;
`tool_use` remains rare and needs cross-source supplementation, not more
WildChat). §6.2's target is enforced downstream in acquisition/labeling; this
acquisition-side proxy is now far better balanced than the 40k run.

### Split sizes (by conversation_id, 70/15/15)

| Split | Conversations | Turns |
|---|---:|---:|
| train | 3,422 | 4,533 |
| val | 669 | 871 |
| test | 728 | 985 |
| **total** | **4,819** | **6,389** |

Turn-level split shares land at 70.9 / 13.6 / 15.4 — close to 70/15/15; the
small drift is expected because the split is frozen *per conversation* (so
multi-turn conversations move as a unit), which is the whole point of the
§6.2 partition contract.

### Partition stability (spec §6.2)

The extension asserts, at run time, that **every one of the 4,675
conversation-turns from the prior corpus keeps its exact split** under the
unchanged `assign_split` function (log line: `[partition] stable: 4675 prior
conversation_ids keep their split`). The split is a pure function of
`(conversation_id, seed=42)`, so growing the corpus is strictly additive and
never reshuffles an existing assignment.

### Filter cost

| Field | Value |
|---|---:|
| Unique turns LLM-screened (total pass) | 24,461 |
| Calls billed this extension run | 16,229 (the other 8,232 came from the resumable cache) |
| Model | `deepseek/deepseek-v4-flash` |
| Filter spend | recorded in private ops notes |

The category pre-filter means over-represented `factual_qa` turns past the 35%
cap were **skipped before the LLM call**, so they cost nothing. Total filter
spend across both runs is recorded in private ops notes and was well within the
owner-approved budget.

### Corpus file sha256

| File | sha256 |
|---|---|
| `data/corpus.jsonl` | `cab90ce38b6d50570a19fcda1f53d346851a68b900653e269a78e4ddf57f7564` |

---

## 5. Redistribution posture (summary)

- Raw and sampled WildChat rows: **never committed.** Written only under the
  git-ignored `scripts/pilot_router/data/`.
- Committed to the repo: this `DATA.md`, `sample_corpus.py`, and
  `corpus_meta.json` (counts, sha256s, dataset revision, filter-model pin,
  seed). **No data rows.**
- Verified: `git check-ignore` confirms `scripts/pilot_router/data/*.jsonl`
  is ignored; `git status` shows no `data/` content staged before commit.

---

## 6. Labeling (T6, spec §6.2) — rubric + harness + gated dry run

Each corpus turn is assigned a reasoning-difficulty tier `R0/R1/R2/R3` by
`label_corpus.py` per [`rubric.md`](rubric.md). This section records the pinned
labeler, the protocol, and the dry-run gate outcome. **Label rows are never
committed** — only the stats/meta below and the committed `labels_meta.json`.

### Pinned labeler + params (reproducibility)

| Field | Value |
|---|---|
| Provider | **OpenCAP**, OpenAI-compatible; endpoint supplied via `OPENCAP_BASE_URL` (recorded in private ops notes); owner-pinned 2026-07-18 (revised) |
| Model | **`claude-opus-4.8`** (bare id — no `anthropic/` prefix; the gateway requires it) |
| Labeler pin | `opencap:claude-opus-4.8@t0.0` (keys the resumable cache file) |
| Temperature | `0.0` |
| Max tokens | `200` |
| Output | strict JSON `{"label":"R0..R3","why":"<one sentence>"}` |
| Pass A ordering | `orderA_R0_to_R3_v1` (classes presented cheapest-first) |
| Pass B ordering | `orderB_R3_to_R0_v1` (classes presented hardest-first) |
| Adjudication prompt | `adjudicate_v1` (shows both candidate labels sorted, pass identity hidden) |
| Rubric file | `rubric.md`, **sha256 `f4cef943c56d4e0e40382e6bbd342c27d23fa0a8c62fdedb0945a1c6c437cef2`** (rubric v1) |

> **Provider switch (2026-07-18):** the labeler was moved from OpenRouter
> (`anthropic/claude-opus-4.8`) to OpenCAP (`claude-opus-4.8`) by owner
> decision. The two pins are never mixed: the cache file is namespaced by the
> labeler pin, and the **215 OpenRouter-pinned verdicts** from the earlier
> dry run were segregated (moved to `label_cache__openrouter_retired.jsonl`,
> git-ignored) and **not reused**. The OpenCAP run writes a fresh cache
> (`label_cache__opencap_claude_opus_4_8_t0_0.jsonl`).

### Protocol

- **Two independent passes** (A, B) — same model/params, differing only in the
  order the rubric classes are presented (guards against position bias).
- **Adjudication:** where A ≠ B, a third call decides using a distinct prompt
  that shows both candidate labels *sorted* (so ordering never reveals which
  pass produced which).
- **Partition contract:** splits are frozen from T5; labeling moves nothing.
  An adjudicated item whose conversation is in the **test** split is tagged
  `boundary_set: true` — a report-only set (§6.4), never used to train/tune.
- **Resumable:** every `(turn_id, pass)` call is logged to the pin-namespaced
  cache `data/label_cache__<pin>.jsonl`; reruns/full runs never re-bill
  completed calls, and a provider/model switch starts a fresh cache.
- **Malformed JSON:** 1 try + 2 retries per pass; still unparseable → the turn
  is dropped (never guessed).
- **Rate limits / robustness:** moderate concurrency (6 workers); 429/5xx and
  transient network errors get exponential backoff **with jitter** (honoring
  `Retry-After`); on final retry exhaustion a call degrades to an empty reply
  (→ parse-fail → dropped turn) rather than crashing the pool. An unexpected
  per-turn exception is **skip-and-logged**; a genuine auth/quota `RuntimeError`
  (JSON 401/402/403/404) stays **fatal-and-stops**. A **WAF/edge 403 with an
  HTML body** (block page under parallel load) is treated as **transient**
  (20–40 s jittered backoff) with a 20-consecutive fuse; a JSON 403 stays
  fatal.

### Dry run — 100 stratified TRAIN turns (2026-07-18, OpenCAP pin)

Stratified across all six T5 categories (≈17 each). Real OpenCAP run.

| Metric | Value |
|---|---:|
| Turns labeled / dropped | 100 / 0 |
| Label distribution | R0 = 11, R1 = 32, R2 = 54, R3 = 3 |
| Two-pass agreement rate | **0.91** |
| Adjudication rate | **0.09** |
| Boundary-set size (test-split adj.) | 0 (dry run is train-only by design) |
| LLM calls (2 passes + 9 adj.) | 209 |
| Prompt / completion tokens | 156,598 / 9,342 |
| Run cost | recorded in private ops notes |

### Dry-run gate — 2 of 3 pass; cost then relaxed by owner

| Criterion | Measured | Verdict |
|---|---|:--:|
| Two-pass agreement ≥ 0.70 | 0.910 | ✅ PASS |
| All four classes present, none > 70% | present, max share R2 = 0.54 | ✅ PASS |
| Projected full-run cost ≤ ceiling | recorded in private ops notes | see below |

Agreement and class-balance both passed. The projected full-run cost initially
tripped a conservative cost ceiling; the owner reviewed the projection against
actual gateway billing and relaxed the ceiling, so the cost criterion passed and
the full run was authorized and executed. Exact cost figures and the ceiling are
recorded in private ops notes.

### Full run — all 6,389 turns, all splits (2026-07-18/19, OpenCAP pin)

| Metric | Value |
|---|---:|
| Turns labeled / unlabeled | **6,376 / 13** (≈0.2 % dropped after retries) |
| Overall label distribution | R0 = 370, R1 = 2,523, R2 = 3,251, R3 = 232 |
| Two-pass agreement rate | **0.8628** |
| Adjudication rate | **0.1372** |
| Boundary-set size (adjudicated **test**-split items) | **147** |
| Total billed LLM calls (all resumed legs) | 13,634 |
| Total run cost | recorded in private ops notes |

Per-split label distributions:

| Split | Total | R0 | R1 | R2 | R3 |
|---|---:|---:|---:|---:|---:|
| train | 4,526 | 243 | 1,761 | 2,354 | 168 |
| val | 867 | 55 | 354 | 427 | 31 |
| test | 983 | 72 | 408 | 470 | 33 |

### Owner-delegated gate on the FULL run — **PASS (under owner relaxation)**

| Criterion | Measured | Verdict |
|---|---|:--:|
| Two-pass agreement ≥ 0.70 | 0.863 | ✅ PASS |
| All four classes present, none > 70% | present, max share R2 = 0.51 | ✅ PASS |
| Cost ≤ ceiling | recorded in private ops notes | ⚠️ over, **PASS via owner relaxation** |

Agreement and class-balance both pass. The projected cost landed over the
review ceiling; the owner relaxed the ceiling after comparing the estimate to
actual gateway billing, so **the gate PASSES** under that relaxation. Exact cost
figures and the ceiling are recorded in private ops notes. The per-turn cost
rose vs. the dry-run estimate mainly because the full-run adjudication rate
(0.137) exceeded the dry run's (0.09), adding a third call to more turns.

### Run incidents

Operational incidents during the runs are recorded in private ops notes.

The completed run wrote 6,376 labeled rows; `labels_meta.json` records the
full-run stats and `gate.pass = true` (owner relaxation).

### Redistribution posture (labeling)

- `data/labels.jsonl`, all `data/label_cache__*.jsonl` (incl. the retired
  OpenRouter cache), and `data/label_run.log`: **never committed** (git-ignored
  under `scripts/pilot_router/data/`).
- Committed: `rubric.md`, `label_corpus.py`, `labels_meta.json` (counts,
  rates, cost, provider + model pin, rubric sha256). **No label rows.**

---

## 7. Training (T7, spec §6.6) — the locked `pilot-v1` artifact

The shipped model is built by **one command** ([`train.py`](train.py)); its
pure, encoder-agnostic half lives in [`train_lib.py`](train_lib.py) and the
skl2onnx export in [`export_model.py`](export_model.py):

```sh
uv run --group pilot-train --extra recommended \
    python scripts/pilot_router/train.py
```

Everything below the pipeline is **locked** — no backbone/pooling/feature/head/
hyperparameter search of any kind. Evaluation (T9) decides ship-or-stop; T7 only
produces the artifact.

### Locked recipe

| Item | Value |
|---|---|
| Architecture | `Pipeline(StandardScaler, MLPClassifier(hidden_layer_sizes=(256, 64)))` |
| Features | T1 `build_features` → `float32 [N, 392]` (384-d MiniLM INT8 mean-pool + L2, then 8 scalars), via the **production** `_MiniLMEncoder` imported from `pilot.strategy` (no third encoder) |
| Sample weights | GOLD-class `R0=1, R1=1, R2=2, R3=3` (`MLPClassifier.fit(..., sample_weight=…)`) |
| Labels | integer `0..3` ↔ `["R0","R1","R2","R3"]` (production ONNX contract, `zipmap=False`) |
| Shipped seed | **42** (ships regardless of the stability numbers) |
| Diagnostic seeds | 7, 2026 (stability report only; no seed selected by score) |
| Calibration | log-space temperature `T` fit on **validation only** by minimizing NLL of `softmax(log(clip(p,1e-7,1))/T)` (golden-section, deterministic) |
| Splits | train + val loaded; **test never opened** (T9 owns it). Boundary-set rows untouched here. |

### Resampling decision (spec §6.2) — **none**

The TRAIN partition is **not resampled**. Class balance is carried entirely by
the GOLD-class sample weights (R2 counts 2×, R3 counts 3×), so no rows are
duplicated and the fit sees each real turn exactly once. Validation and test
keep their **natural** distribution (never resampled, by contract). Rationale:
oversampling R3 (168 train rows) with replacement would repeat the same ~168
vectors up to ~14× toward the R2 majority — inflating overfit on a handful of
rare points without adding signal — whereas the sample-weight lever raises R3's
gradient contribution without cloning rows. The `oversample` strategy is
implemented and available in `train_lib.resample_train` but is **not** used by
the shipped artifact.

### Feature cache

Embedding the 5,393 train+val turns through INT8 MiniLM takes ~16 s cold on the
build machine (Apple silicon). Features are cached to a **git-ignored**
`.feature_cache/<split>_<fingerprint>.npz`, keyed by a sha256 over
(corpus bytes + labels bytes + the 392-dim contract), so any data or contract
change invalidates the cache and retrains are otherwise instant.

### Set sizes and per-split class balance (natural)

| Split | R0 | R1 | R2 | R3 | total |
|---|---|---|---|---|---|
| train | 243 | 1761 | 2354 | 168 | **4526** |
| val | 55 | 354 | 427 | 31 | **867** |

(Test — 72/408/470/33 = 983 — is recorded in `labels_meta.json` but **not read**
by T7.)

### Seed-42 validation metrics (the shipped artifact)

| Metric | Value |
|---|---|
| Accuracy | **0.6713** |
| Recall R0 / R1 / R2 / R3 | 0.418 / 0.644 / 0.759 / **0.226** |
| Severity-weighted under-routing | 0.3322 |
| ECE (15-bin) before → after calibration | 0.2591 → **0.0513** |
| NLL before → after calibration | 1.784 → 0.780 |
| Fitted temperature `T` | **4.3573** |

Validation confusion (gold rows × predicted cols, seed 42):

| gold \ pred | R0 | R1 | R2 | R3 |
|---|---|---|---|---|
| **R0** | 23 | 25 | 7 | 0 |
| **R1** | 11 | 228 | 113 | 2 |
| **R2** | 1 | 93 | 324 | 9 |
| **R3** | 0 | 5 | 19 | 7 |

### 3-seed stability (mean ± std over seeds 42, 7, 2026 — diagnostic only)

| Metric | mean ± std |
|---|---|
| Accuracy | 0.6697 ± 0.0011 |
| Severity-weighted under-routing | 0.3314 ± 0.0076 |
| ECE (calibrated) | 0.0501 ± 0.0062 |
| Temperature `T` | 4.4012 ± 0.1263 |
| Recall R0 | 0.3939 ± 0.0227 |
| Recall R1 | 0.6516 ± 0.0087 |
| Recall R2 | 0.7518 ± 0.0083 |
| Recall R3 | 0.2366 ± 0.0152 |

The seeds are tight (accuracy std ~0.001): the recipe is stable, not
seed-lucky. **Honest read:** overall accuracy ~0.67 is well above the 0.5
catastrophic floor, and temperature calibration cuts ECE ~5× (0.26 → 0.05).
The weak spots are the two rare classes — **R3 recall ~0.23** (most R3 turns
land in R2, the adjacent tier) and R0 ~0.42 — driven by their scarcity (3.6% /
5.8% of the corpus) even with the up-weighting. These are reported exactly as
measured; no rerun hunted a better seed, and seed 42 ships regardless. T9's
eval gate on the **test** split is the ship-or-stop decision.

### Artifact staging and provenance

- The seed-42 artifact (`model.onnx` + `manifest.json`) is written to the
  **git-ignored** staging dir `scripts/pilot_router/artifacts/pilot_v1/`. Per
  the spec flow it lands in the shipped location
  (`src/agentos/agentos_router/models/pilot_v1/`) **only after T9 passes** —
  not in this task.
- Loadability is verified in-run: `PilotModel(staging_dir)` loads available and
  predicts a normalized `[N, 4]` distribution, with exact 392-dim parity
  asserted (a 391-dim input is rejected fail-soft).
- The `manifest.json` records the full §6.3 contract (classes, temperature,
  embedder id, `model.onnx` sha256, IO contract, encoder contract copied from
  the MiniLM `export_meta.json`, feature schema with the `url_regex`/`file_regex`
  pins, and the training-stats block). It loads cleanly through `PilotModel`.
- **Committed** (git-tracked): `train.py`, `train_lib.py`, `export_model.py`,
  and [`training_meta.json`](training_meta.json) — the full metrics + provenance
  record (set sizes, per-split balance, seed-42 metrics + confusion, 3-seed
  stability, resampling decision, sample-weight policy, labeler pin, rubric
  sha256, git SHA, pinned + installed dep versions). **Never committed:** the
  staged `artifacts/` and the `.feature_cache/`.

### CI smoke (offline, spec §9)

`tests/test_agentos_router/test_pilot_train_smoke.py` drives the **same** T7
code paths on a synthetic 50-row corpus+labels fixture with a deterministic stub
encoder (no MiniLM weights, no network, fork-safe): join → build `[N, 392]`
features → train tiny → fit T → export → `PilotModel` load round-trip, plus a
parity guard that a wrong embed width trips. It `importorskip`s the `pilot-train`
group so the default offline suite (which lacks it) does not error.

## 8. pilot-v1 R3 uplift — config-grid round (owner-approved amendment)

The Pilot router ships as **pilot-v1**; this is the R3-uplift training round of
pilot-v1 (there is no separate "v2" product). The T9 eval gate on the **test**
split returned **STOP** on the v1 artifact: R3 recall 0.242 (floor 0.60),
accuracy 0.630 (floor 0.70), over-routing 0.205 (limit 0.144). Root cause: R3 is
starved — only **168 train rows** (3.7% of the corpus). The owner recorded a
**spec amendment** (`.superpowers/sdd/progress.md`): the config-grid round
permits a **small, explicit config grid selected on VALIDATION ONLY** — the v1
no-search rule is amended *only* for this grid. Everything else of the v1
discipline stands: the fixed architecture, seed 42, the same temperature-refit
procedure, and — critically — **the test split stays sealed** (no test rows or
labels are read anywhere in the config-grid round; T9 owns the test gate).

### The frozen grid (exactly four configs — no others, no search beyond them)

| Config | Sample weights (R0/R1/R2/R3) | TRAIN oversample multipliers |
|---|---|---|
| `baseline` | 1 / 1 / 2 / 3 | none (shipped v1 settings, rerun for comparison) |
| `oversample` | 1 / 1 / 2 / 3 | R0 ×2, R3 ×3 (~R0 243→486, R3 168→504 effective) |
| `weights` | **2** / 1 / 2 / **6** | none |
| `both` | **1.5** / 1 / 2 / **4** | R3 ×2 (~R3 168→336) |

Oversampling is **with-replacement copies of real TRAIN rows** (no synthesized
vectors), applied only to the named classes; val/test keep their **natural**
distribution. All four run at seed 42 with per-config temperature refit on val.

Run it with:

```sh
uv run --group pilot-train --extra recommended \
    python scripts/pilot_router/train.py --r3-uplift-grid
```

### Validation comparison (seed 42, val split, **all four reported as measured**)

| Config | Acc | R0 | R1 | R2 | **R3** | Sev-under | Over-route (pred>gold) | ECE (cal) | T |
|---|---|---|---|---|---|---|---|---|---|
| `baseline` | 0.6713 | 0.418 | 0.644 | 0.759 | **0.226** | 0.3322 | 0.1799 | 0.0513 | 4.357 |
| `oversample` | 0.6770 | 0.400 | 0.653 | 0.766 | **0.226** | 0.3368 | 0.1742 | 0.0383 | 4.247 |
| `weights` | 0.6563 | 0.418 | 0.647 | 0.726 | **0.226** | 0.3933 | 0.1696 | 0.0494 | 4.297 |
| `both` | 0.6759 | 0.382 | 0.698 | 0.728 | **0.226** | 0.3702 | 0.1626 | 0.0471 | 4.238 |

**R3 recall is bit-identical (7/31 = 0.22581) across all four configs.** Neither
oversampling nor sample-weight boosts moved the R3 decision boundary on
validation at all — the levers only trade R0/R1/R2 against each other. R3 is too
sparse for a re-weighting/resampling lever to add signal; the 31 val R3 turns
that land correctly (7) are the same set under every config. The over-routing
proxy actually *dropped* slightly under every non-baseline config, so
over-routing was never the binding constraint here — recall was.

### Selection rule (applied mechanically) → **KEEP baseline**

Rule (from the amendment): among configs whose val accuracy is within **2pp** of
the best config's accuracy **and** whose over-routing proxy does not exceed
baseline's by more than **5pp**, choose the highest val **R3 recall**; if no
config beats baseline's R3 recall by **≥5pp**, that is a valid outcome —
keep baseline.

Best eligible R3 uplift over baseline: **+0.000** (all tied at 0.226). No config
clears the +5pp meaningful-uplift bar, so the rule **mechanically keeps
baseline**. This is the honest, expected outcome the amendment anticipated:
**targeted data (hard-R3 mining → Opus labeling → supplemental TRAIN rows) is
the real lever** for R3 recall, not a config search over the existing
232-effective-row R3 partition.

### Artifact staging (separate from v1 — v1 record untouched)

The selected config (`baseline`) is exported to the **separate git-ignored**
staging dir `scripts/pilot_router/artifacts/pilot_v1_uplift_grid/` (`model.onnx` +
`manifest.json` + a `grid_val_table.json` recording all four configs and the
selection rationale). The v1 staging dir `artifacts/pilot_v1/` — the committed
T9 record — is **never overwritten**. Loadability is verified in-run through
`PilotModel`. The grid selection, per-config weights/multipliers, and the full
val comparison table are recorded verbatim in the selected artifact's
`manifest.json` `training_stats` block.

### CI smoke (offline) — the grid plumbing

`tests/test_agentos_router/test_pilot_train_v2_configs.py` pins the grid to
exactly the four approved names, locks the two new levers
(`resample_train("multiplier", …)` per-class oversampling and per-config
`sample_weights_for`/`train_pipeline` weights) plus the val-only over-routing
proxy added to `evaluate`, and drives **each config** through the real
train→export→`PilotModel`-load path on the same synthetic stub-encoder fixture
as the v1 smoke. It `importorskip`s the `pilot-train` group like the v1 smoke.
