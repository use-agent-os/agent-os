"""Golden-set CI regression guard for the Pilot router (spec §6.4).

Asserts the Pilot router's **final-decision** accuracy on the committed golden
set (``data/pilot_golden.jsonl``) does not regress below the owner-amended
floor of 0.55 (see ``GOLDEN_ACCURACY_FLOOR``; the original 0.80 is now the
aspirational target under the 2026-07-19 relative-to-incumbent gate). Each row
is replayed through the REAL ``apply_agentos_router`` step (guards enabled), so
the test scores exactly what production would route — not the raw classifier
argmax.

The golden rows are self-authored, rubric-anchored exemplars (see the file's
``_meta`` header); the header line is skipped here.

**Skip-if-no-artifacts.** The production Pilot artifact
(``models/pilot_v1/``) is not shipped until after the T9 gate passes, and the
T7 staging artifact (``scripts/pilot_router/artifacts/pilot_v1/``) is gitignored.
So this test SKIPS when no loadable Pilot artifact is found, and activates
automatically once one is present — matching the labeling-side skip pattern.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("numpy", reason="pilot runtime (numpy) not installed")
pytest.importorskip("onnxruntime", reason="pilot runtime (onnxruntime) not installed")

GOLDEN_PATH = Path(__file__).parent / "data" / "pilot_golden.jsonl"

# Owner-amended 2026-07-19 (relative-to-incumbent ship gate). The shipped
# pilot-v1 bundle measures 0.584 golden final-decision accuracy and beats the
# v4 incumbent on 11/12 gate axes, so this is a REGRESSION GUARD at 0.55 — not
# the original 0.80 absolute floor. 0.80 remains the aspirational target; a
# future uplift round should raise this back toward it. Do NOT lower this
# threshold further without owner approval.
GOLDEN_ACCURACY_FLOOR = 0.55
GOLDEN_ACCURACY_TARGET = 0.80  # aspirational; tracked as a future improvement


def _load_golden() -> list[dict]:
    rows: list[dict] = []
    with GOLDEN_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if "_meta" in rec:  # header line
                continue
            rows.append(rec)
    return rows


def _resolve_pilot_artifact() -> Path | None:
    """Return the loadable SHIPPED Pilot artifact dir, or None (→ skip).

    Only the shipped production bundle (``models/pilot_v1/``) activates this
    gate. That bundle does not exist until the artifact is promoted AFTER a
    passing T9 gate, so the test skips until then — the brief's
    "skip-if-no-real-artifacts for now; activates when artifacts ship". The T7
    staging artifact under ``scripts/pilot_router/artifacts/`` is deliberately
    NOT consulted: it is a pre-gate staging copy, not a shipped artifact, and
    the T9 measurement (recorded in ``eval_report.md``) already scored it.
    """
    from agentos.agentos_router.pilot.model import PilotModel
    from agentos.agentos_router.pilot.strategy import default_artifact_dir

    candidate = default_artifact_dir()
    try:
        if candidate.exists() and PilotModel(candidate).available:
            return candidate
    except Exception:
        return None
    return None


def test_golden_set_is_well_formed() -> None:
    rows = _load_golden()
    assert 60 <= len(rows) <= 100, f"golden set should have 60-100 rows, got {len(rows)}"
    classes = {r["gold_class"] for r in rows}
    assert classes == {"R0", "R1", "R2", "R3"}, f"all four classes required, got {classes}"
    # Code-heavy coverage (spec risk #3): several rows must carry code.
    code_rows = [
        r for r in rows if "def " in r["text"] or "```" in r["text"] or "SELECT " in r["text"]
    ]
    assert len(code_rows) >= 5, "golden set must include code-heavy turns"
    assert all(r["gold_class"] in {"R0", "R1", "R2", "R3"} for r in rows)
    assert len({r["id"] for r in rows}) == len(rows), "row ids must be unique"


@pytest.mark.asyncio
async def test_pilot_meets_golden_accuracy_floor() -> None:
    artifact = _resolve_pilot_artifact()
    if artifact is None:
        pytest.skip("no loadable Pilot artifact (shipped bundle absent, staging gitignored)")

    from agentos.agentos_router.pilot import PilotStrategy
    from agentos.engine.steps import agentos_router as step
    from agentos.gateway.config import GatewayConfig

    strategy = PilotStrategy(
        artifact_dir=str(artifact),
        safety_net_threshold=0.5,
        confidence_threshold=0.5,
    )
    assert strategy._available

    rows = _load_golden()
    tier_to_class = {"c0": "R0", "c1": "R1", "c2": "R2", "c3": "R3"}

    # Route each golden row through the FULL step in an isolated session (no
    # cross-row history — golden rows are independent single-turn exemplars).
    step._history_store.clear()
    step._strategy = None
    step._strategy_key = None
    original = step._get_strategy
    step._get_strategy = lambda _config, _llm_cfg=None: strategy  # type: ignore[assignment]
    try:
        correct = 0
        for i, row in enumerate(rows):
            from agentos.engine.pipeline import TurnContext

            config = GatewayConfig()
            config.agentos_router.enabled = True
            config.agentos_router.rollout_phase = "full"
            ctx = TurnContext(
                message=row["text"],
                session_key=f"golden-{i}",
                config=config,
                provider=None,
                model=config.llm.model,
                tool_defs=[],
                system_prompt="system",
            )
            routed = await step.apply_agentos_router(ctx)
            extra = routed.metadata.get("routing_extra") or {}
            pred = extra.get("final_route_class") or tier_to_class.get(
                str(routed.metadata.get("routed_tier")), "R1"
            )
            if pred == row["gold_class"]:
                correct += 1
    finally:
        step._get_strategy = original
        step._history_store.clear()
        step._strategy = None
        step._strategy_key = None

    accuracy = correct / len(rows)
    assert accuracy >= GOLDEN_ACCURACY_FLOOR, (
        f"Pilot golden-set accuracy {accuracy:.3f} regressed below the "
        f"owner-amended floor {GOLDEN_ACCURACY_FLOOR} "
        f"(aspirational target {GOLDEN_ACCURACY_TARGET}) "
        f"({correct}/{len(rows)} correct)"
    )
