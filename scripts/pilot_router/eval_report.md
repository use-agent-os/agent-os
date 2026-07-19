# Pilot-v1 vs v4_phase3 — T9 evaluation gate report

**VERDICT: STOP** (one or more gate rows FAIL)

Honest measurement (spec §6.6): every §6.4 gate row is filled for both routers exactly as measured on the held-out test split, replayed through the full `apply_agentos_router` step. A failed gate is a valid, final STOP outcome — no tuning, no threshold nudging, no re-runs.

- Test turns scored: **983** (per router)
- Boundary-set turns (report-only): **147**
- Pilot artifact: `scripts/pilot_router/artifacts/pilot_v1`
- Pilot thresholds: safety_net 0.5 / confidence 0.5 (defaults)
- Fitted temperature (in manifest): 4.357346773147382

## §6.4 gate table

| Metric | Pilot | v4_phase3 | Gate | Verdict |
|---|---|---|---|---|
| Accuracy (4-class, final decision) | 0.6297 | 0.2604 | Pilot >= v4 AND >= 0.70 | **FAIL** |
| Under-routing rate (pred < gold) | 0.1658 | 0.6460 | Pilot <= v4 | PASS |
| Severity-weighted under-routing | 0.1740 | 0.8169 | Pilot <= v4 | PASS |
| R2 recall (Pilot) | 0.7085 | 0.0660 | Pilot >= 0.60 absolute | PASS |
| R3 recall (Pilot) | 0.2424 | 0.1212 | Pilot >= 0.60 absolute | **FAIL** |
| Over-routing rate (pred > gold) | 0.2045 | 0.0936 | Pilot <= v4 + 5pp | **FAIL** |
| Accuracy-delta 95% bootstrap CI | delta=0.3693 CI[0.3266, 0.4120] | - | CI lower bound > -1pp | PASS |

## Report-only metrics

| Metric | Pilot | v4_phase3 |
|---|---|---|
| Macro-F1 | 0.4832 | 0.1965 |
| Recall R0 | 0.2083 | 0.5694 |
| Recall R1 | 0.6446 | 0.4412 |
| Recall R2 | 0.7085 | 0.0660 |
| Recall R3 | 0.2424 | 0.1212 |
| ECE (15-bin, calibrated) | 0.0451 | 0.4473 |
| NLL | 0.8195 | 2.2649 |
| Boundary-set accuracy | 0.4762 | 0.2517 |

## Confusion matrices (gold rows × pred cols)

### Pilot

| gold\pred | R0 | R1 | R2 | R3 | recall |
|---|---|---|---|---|---|
| **R0** | 15 | 37 | 20 | 0 | 0.208 |
| **R1** | 6 | 263 | 134 | 5 | 0.645 |
| **R2** | 3 | 129 | 333 | 5 | 0.709 |
| **R3** | 0 | 5 | 20 | 8 | 0.242 |

### v4_phase3

| gold\pred | R0 | R1 | R2 | R3 | recall |
|---|---|---|---|---|---|
| **R0** | 41 | 25 | 3 | 3 | 0.569 |
| **R1** | 199 | 180 | 7 | 22 | 0.441 |
| **R2** | 147 | 260 | 31 | 32 | 0.066 |
| **R3** | 0 | 21 | 8 | 4 | 0.121 |

## Statistical validity

Paired bootstrap (10000 resamples, seed 42) on `acc(Pilot) - acc(v4)`: point delta **0.3693**, 95% CI **[0.3266, 0.4120]**. Gate (CI lower bound > -1pp): PASS.

## Quality-oracle subset (report-only)

- Status: ok
- Turns judged: 32
- Tier→model: `{'R0': 'deepseek-v4-flash', 'R1': 'minimax-m3', 'R2': 'glm-5.2', 'R3': 'claude-opus-4.8'}`
- All four tier models were accepted by OpenCAP (no substitutions or skips).
- Cheapest-acceptable-tier agreement with Pilot prediction: 21.88%
- Rows where a cheaper tier than Pilot's pick already sufficed: 9
- Rows where NEITHER tried tier was judged acceptable: 16 (the pinned opus judge grades strictly; some reasoning-model answers also returned empty content under the answer token cap and parse as unacceptable — so this is a conservative lower bound, report-only).
- By gold difficulty (turns with ≥1 acceptable tier / total):
    - R0: 6/8
    - R1: 6/8
    - R2: 4/8
    - R3: 0/8

## Golden set (report-only here; CI floor >= 0.80)

- `tests/test_agentos_router/data/pilot_golden.jsonl`: Pilot final-decision accuracy **0.5325** over 77 rows (floor 0.8): **FAIL**
- Golden per-class recall: R0=0.4667, R1=0.9524, R2=0.5833, R3=0.0000
- The CI test `test_pilot_golden.py` enforces the 0.80 floor and activates when a shipped `models/pilot_v1/` bundle is present (skips until then).

## Provenance

- git sha: `4e39e0239b99bf3043548858e9e633e54f337c0c`
- generated: 2026-07-18T19:11:19Z
- Replay: full `apply_agentos_router` step, guards enabled, history per conversation in `turn_id` order; scored on the engine's FINAL (post-guard) tier.
- Raw per-turn rows: `scripts/pilot_router/data/eval_raw_rows.jsonl` (gitignored).
