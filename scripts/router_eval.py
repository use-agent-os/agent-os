#!/usr/bin/env python3
"""Offline routing eval: run a router strategy over the labeled dataset.

Runs every case in tests/data/router_eval/cases.jsonl and every session
script in tests/data/router_eval/sessions.jsonl through the REAL
``apply_agentos_router`` path (same TurnContext/GatewayConfig construction as
scripts/smoke_llm_judge_router.py) and reports:

- accuracy (predicted route class == gold class)
- under_route_rate = P(pred_rank < gold_rank)   <- primary KPI
- over_route_rate  = P(pred_rank > gold_rank)
- per-class confusion matrix
- per-lang / per-tag slices
- sessions: downgrade_within_window_rate (follow-up turns whose applied tier
  drops below the session's previously established tier)
- with --repeat N: class-agreement rate across N runs of each case

Metrics are computed for two views:
- "classification": the strategy's raw route class (``routing_extra.route_class``)
- "applied": the tier actually applied after deterministic guards
  (``routed_tier`` mapped back to a class rank)

Usage:
    uv run python scripts/router_eval.py --name judge_eval
    uv run python scripts/router_eval.py --strategy llm_judge --repeat 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import traceback
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agentos.engine.pipeline import TurnContext  # noqa: E402
from agentos.engine.steps import agentos_router as router_mod  # noqa: E402
from agentos.env import load_env  # noqa: E402
from agentos.gateway.config import GatewayConfig  # noqa: E402

DATA_DIR = REPO_ROOT / "tests" / "data" / "router_eval"
REPORTS_DIR = DATA_DIR / "reports"
ROUTE_CLASSES = ["R0", "R1", "R2", "R3"]
TIERS = {
    "c0": {
        "provider": "openrouter",
        "model": "eval-c0-model",
        "description": "short text and trivial follow-ups",
    },
    "c1": {
        "provider": "openrouter",
        "model": "eval-c1-model",
        "description": "normal coding and agent tasks",
    },
    "c2": {
        "provider": "openrouter",
        "model": "eval-c2-model",
        "description": "structured multi-step work",
    },
    "c3": {
        "provider": "openrouter",
        "model": "eval-c3-model",
        "description": "deep reasoning and hard recovery turns",
    },
}
TIER_ORDER = list(TIERS)


def _class_rank(route_class: str | None) -> int:
    return ROUTE_CLASSES.index(route_class) if route_class in ROUTE_CLASSES else -1


def _tier_rank(tier: str | None) -> int:
    return TIER_ORDER.index(tier) if tier in TIER_ORDER else -1


def _reset_router_state(*, reset_strategy: bool = False) -> None:
    router_mod._history_store.clear()
    if reset_strategy:
        router_mod._strategy = None
        router_mod._strategy_key = None


def _router_config(strategy: str) -> GatewayConfig:
    config = GatewayConfig()
    config.agentos_router = config.agentos_router.model_copy(
        update={
            "enabled": True,
            "strategy": strategy,
            "rollout_phase": "full",
            "tiers": TIERS,
            "default_tier": "c1",
            "confidence_threshold": 0.5,
            "kv_cache_anti_downgrade_enabled": True,
            "kv_cache_anti_downgrade_window_seconds": 600,
            "complaint_upgrade_enabled": True,
            "complaint_upgrade_steps": 1,
        }
    )
    return config


async def _route_turn(config: GatewayConfig, message: str, session_key: str) -> dict[str, Any]:
    ctx = TurnContext(
        message=message,
        raw_message=message,
        session_key=session_key,
        config=config,
        provider=None,
        model="eval-default-model",
        tool_defs=[],
        system_prompt="",
        attachments=[],
        metadata={},
    )
    out = await router_mod.apply_agentos_router(ctx)
    extra = out.metadata.get("routing_extra") or {}
    return {
        "route_class": extra.get("route_class"),
        "final_route_class": extra.get("final_route_class"),
        "final_tier": out.metadata.get("routed_tier"),
        "base_tier": extra.get("base_tier"),
        "confidence": out.metadata.get("routing_confidence"),
        "source": out.metadata.get("routing_source"),
        "applied": out.metadata.get("routing_applied"),
        "anti_downgrade_applied": extra.get("anti_downgrade_applied"),
        "complaint_upgrade_applied": extra.get("complaint_upgrade_applied"),
        "confidence_gate_applied": extra.get("confidence_gate_applied"),
    }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _directional_metrics(pairs: list[tuple[int, int]]) -> dict[str, Any]:
    """pairs: (gold_rank, pred_rank) with valid ranks only."""
    total = len(pairs)
    if total == 0:
        return {"count": 0, "accuracy": None, "under_route_rate": None, "over_route_rate": None}
    exact = sum(1 for gold, pred in pairs if pred == gold)
    under = sum(1 for gold, pred in pairs if pred < gold)
    over = sum(1 for gold, pred in pairs if pred > gold)
    return {
        "count": total,
        "accuracy": round(exact / total, 4),
        "under_route_rate": round(under / total, 4),
        "over_route_rate": round(over / total, 4),
    }


def _confusion(rows: list[dict[str, Any]], pred_key: str) -> dict[str, dict[str, int]]:
    matrix: dict[str, dict[str, int]] = {
        gold: {pred: 0 for pred in [*ROUTE_CLASSES, "invalid"]} for gold in ROUTE_CLASSES
    }
    for row in rows:
        pred = row.get(pred_key)
        matrix[row["gold_class"]][pred if pred in ROUTE_CLASSES else "invalid"] += 1
    return matrix


def _case_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    class_pairs = [
        (_class_rank(row["gold_class"]), _class_rank(row["pred_class"]))
        for row in rows
        if _class_rank(row["pred_class"]) >= 0
    ]
    applied_pairs = [
        (_class_rank(row["gold_class"]), _tier_rank(row["final_tier"]))
        for row in rows
        if _tier_rank(row["final_tier"]) >= 0
    ]
    return {
        "classification": _directional_metrics(class_pairs),
        "applied": _directional_metrics(applied_pairs),
        "errors": sum(1 for row in rows if row.get("error")),
    }


def _slices(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[f"lang:{row['lang']}"].append(row)
        for tag in row["tags"]:
            groups[f"tag:{tag}"].append(row)
    return {name: _case_metrics(group) for name, group in sorted(groups.items())}


async def _run_cases(
    config: GatewayConfig, cases: list[dict[str, Any]], *, run_index: int
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        _reset_router_state()
        session_key = f"router-eval:{run_index}:{case['id']}"
        row: dict[str, Any] = {
            "id": case["id"],
            "gold_class": case["gold_class"],
            "lang": case["lang"],
            "tags": case["tags"],
            "message_chars": len(case["message"]),
        }
        try:
            result = await _route_turn(config, case["message"], session_key)
        except Exception as exc:  # noqa: BLE001 - recorded verbatim in the report
            row.update({"pred_class": None, "final_tier": None, "error": repr(exc)})
            rows.append(row)
            continue
        row.update(
            {
                "pred_class": result["route_class"],
                "final_class": result["final_route_class"],
                "final_tier": result["final_tier"],
                "base_tier": result["base_tier"],
                "confidence": result["confidence"],
                "source": result["source"],
                "confidence_gate_applied": result["confidence_gate_applied"],
                "error": None,
            }
        )
        rows.append(row)
    return rows


async def _run_sessions(
    config: GatewayConfig, sessions: list[dict[str, Any]]
) -> dict[str, Any]:
    session_rows: list[dict[str, Any]] = []
    followup_turns = 0
    downgrades = 0
    turn_rows: list[dict[str, Any]] = []
    for session in sessions:
        _reset_router_state()
        session_key = f"router-eval-session:{session['id']}"
        turns: list[dict[str, Any]] = []
        max_prior_tier_rank = -1
        session_downgrades = 0
        for index, turn in enumerate(session["turns"]):
            try:
                result = await _route_turn(config, turn["message"], session_key)
            except Exception as exc:  # noqa: BLE001 - recorded verbatim in the report
                turns.append({"index": index, "error": repr(exc)})
                continue
            tier_rank = _tier_rank(result["final_tier"])
            downgraded = False
            if index > 0:
                followup_turns += 1
                downgraded = 0 <= tier_rank < max_prior_tier_rank
                if downgraded:
                    downgrades += 1
                    session_downgrades += 1
            max_prior_tier_rank = max(max_prior_tier_rank, tier_rank)
            turn_row = {
                "index": index,
                "gold_class": turn["gold_class"],
                "pred_class": result["route_class"],
                "final_tier": result["final_tier"],
                "anti_downgrade_applied": result["anti_downgrade_applied"],
                "complaint_upgrade_applied": result["complaint_upgrade_applied"],
                "downgraded_within_window": downgraded,
                "error": None,
            }
            turns.append(turn_row)
            turn_rows.append({"gold_class": turn["gold_class"], **turn_row})
        session_rows.append(
            {
                "id": session["id"],
                "lang": session.get("lang"),
                "turn_count": len(session["turns"]),
                "downgrades": session_downgrades,
                "turns": turns,
            }
        )

    class_pairs = [
        (_class_rank(row["gold_class"]), _class_rank(row["pred_class"]))
        for row in turn_rows
        if _class_rank(row.get("pred_class")) >= 0
    ]
    return {
        "session_count": len(sessions),
        "turn_count": sum(len(s["turns"]) for s in sessions),
        "followup_turn_count": followup_turns,
        "downgrade_count": downgrades,
        "downgrade_within_window_rate": (
            round(downgrades / followup_turns, 4) if followup_turns else None
        ),
        "sessions_with_downgrade": sum(1 for s in session_rows if s["downgrades"] > 0),
        "turn_classification": _directional_metrics(class_pairs),
        "sessions": session_rows,
    }


def _agreement(runs: list[list[dict[str, Any]]]) -> dict[str, Any]:
    by_id: dict[str, list[str | None]] = defaultdict(list)
    for rows in runs:
        for row in rows:
            by_id[row["id"]].append(row.get("pred_class"))
    disagreements = sorted(
        case_id for case_id, preds in by_id.items() if len(set(preds)) > 1
    )
    total = len(by_id)
    return {
        "repeats": len(runs),
        "case_count": total,
        "class_agreement_rate": (
            round((total - len(disagreements)) / total, 4) if total else None
        ),
        "disagreeing_case_ids": disagreements,
    }


async def _run_eval(args: argparse.Namespace) -> dict[str, Any]:
    cases = _load_jsonl(Path(args.cases))
    sessions = _load_jsonl(Path(args.sessions))
    config = _router_config(args.strategy)

    report: dict[str, Any] = {
        "name": args.name,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "strategy": args.strategy,
        "dataset": {
            "cases_path": str(Path(args.cases).relative_to(REPO_ROOT)),
            "sessions_path": str(Path(args.sessions).relative_to(REPO_ROOT)),
            "case_count": len(cases),
            "session_count": len(sessions),
            "cases_by_gold": dict(Counter(c["gold_class"] for c in cases)),
            "cases_by_lang": dict(Counter(c["lang"] for c in cases)),
        },
        "router_config": {
            "strategy": args.strategy,
            "default_tier": "c1",
            "confidence_threshold": 0.5,
            "kv_cache_anti_downgrade_window_seconds": 600,
            "complaint_upgrade_steps": 1,
            "tiers": TIER_ORDER,
        },
        "runtime_error": None,
    }

    _reset_router_state(reset_strategy=True)
    try:
        probe = await _route_turn(config, "hello", "router-eval:probe")
    except Exception:  # noqa: BLE001 - recorded verbatim in the report
        report["runtime_error"] = traceback.format_exc()
        return report
    if probe["source"] != args.strategy or probe["applied"] is not True:
        report["runtime_error"] = (
            "strategy did not engage: "
            f"routing_source={probe['source']!r} routing_applied={probe['applied']!r} "
            f"(expected source={args.strategy!r}) — probe result: {probe!r}"
        )
        return report

    runs: list[list[dict[str, Any]]] = []
    for run_index in range(max(1, args.repeat)):
        print(f"run {run_index + 1}/{max(1, args.repeat)}: {len(cases)} cases", file=sys.stderr)
        runs.append(await _run_cases(config, cases, run_index=run_index))
    rows = runs[0]

    print(f"sessions: {len(sessions)}", file=sys.stderr)
    session_report = await _run_sessions(config, sessions)

    report["metrics"] = _case_metrics(rows)
    report["confusion_classification"] = _confusion(rows, "pred_class")
    report["confusion_applied_final_class"] = _confusion(rows, "final_class")
    report["slices"] = _slices(rows)
    report["sessions"] = session_report
    if args.repeat > 1:
        report["repeat_consistency"] = _agreement(runs)
    report["cases"] = [
        {key: value for key, value in row.items() if key != "tags"} for row in rows
    ]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default=None, help="report name (default: <strategy>)")
    parser.add_argument("--strategy", default="llm_judge", help="router strategy to evaluate")
    parser.add_argument(
        "--cases", default=str(DATA_DIR / "cases.jsonl"), help="single-turn cases jsonl"
    )
    parser.add_argument(
        "--sessions", default=str(DATA_DIR / "sessions.jsonl"), help="session scripts jsonl"
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="run single-turn cases N times and report class-agreement rate",
    )
    parser.add_argument(
        "--out", default=None, help="report path (default: reports/<name>.json)"
    )
    args = parser.parse_args()
    if args.name is None:
        args.name = args.strategy

    logging.disable(logging.INFO)  # silence router debug logs during the sweep
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
    load_env(REPO_ROOT)
    report = asyncio.run(_run_eval(args))

    out_path = Path(args.out) if args.out else REPORTS_DIR / f"{args.name}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"report written to {out_path}", file=sys.stderr)

    summary = {
        "name": report["name"],
        "strategy": report["strategy"],
        "runtime_error": bool(report.get("runtime_error")),
        "metrics": report.get("metrics"),
        "downgrade_within_window_rate": (report.get("sessions") or {}).get(
            "downgrade_within_window_rate"
        ),
        "repeat_consistency": (report.get("repeat_consistency") or {}).get(
            "class_agreement_rate"
        ),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not report.get("runtime_error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
