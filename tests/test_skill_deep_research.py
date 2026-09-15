"""deep-research iterate.py: record_evidence must advance plan.rounds (#2268).

``record_evidence`` ended its own body with ``plan.rounds = max(plan.rounds,
plan.rounds + 0)`` -- algebraically a no-op for every value of
``plan.rounds``. The one existing caller, ``main()``'s ``--record`` branch,
happened to work anyway, because ``main()`` separately sets
``plan.rounds = max(plan.rounds, args.round_num)`` a few lines earlier, before
``record_evidence`` ever runs -- confirmed by running the issue's own CLI
repro against the unpatched code, which does *not* reproduce a stuck
counter. The real, narrower bug is that ``record_evidence`` is exported as a
reusable function (this file's own docstring frames stage 2 as "print fetch
list and record evidence", and it's the only place ``plan.rounds`` needs to
change to reflect one more completed round) but was not self-sufficient: any
caller that invokes it without going through ``main()``'s CLI parsing -- the
literal scenario the issue names -- saw the counter never move.

The fix adds an explicit, optional ``round_num`` parameter. It defaults to
``None``, not ``0``: ``Plan.rounds`` itself defaults to 0, and the CLI's
own ``--round`` never sends 0 (its default is 1), so 0 is a real, reachable
round number, not just a placeholder for "caller didn't say" -- collapsing
"missing" into "zero" here would be the same class of bug this function's
dead line already was once. With no round_num, the round is inferred as one
past whatever's already recorded (the only call pattern this skill
documents: one ``record_evidence`` per research round). ``max`` keeps the
counter monotonic regardless of how it's called.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "src" / "agentos" / "skills" / "bundled" / "deep-research" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import compile as compile_mod  # type: ignore[import-not-found]  # noqa: E402
import iterate  # type: ignore[import-not-found]  # noqa: E402
import plan as plan_mod  # type: ignore[import-not-found]  # noqa: E402


def _plan(target_sources: int = 2) -> plan_mod.Plan:
    return plan_mod.Plan(
        question="What is AgentOS?",
        depth="overview",
        created_at="2026-09-15",
        subquestions=[
            plan_mod.SubQuestion(id="sq-1", question="What is it?", target_sources=target_sources)
        ],
    )


def _evidence(sq_id: str = "sq-1") -> list[dict[str, object]]:
    return [
        {
            "subquestion_id": sq_id,
            "url": "https://example.com",
            "title": "Example",
            "excerpt": "AgentOS runtime",
            "relevance": 1.0,
            "fetched_at": "2026-09-15",
        }
    ]


# --- record_evidence: the core bug ------------------------------------------


def test_bare_call_with_no_round_num_advances_rounds_by_one() -> None:
    """Fails without the fix: plan.rounds stays 0 forever."""
    p = _plan()
    assert p.rounds == 0

    added = iterate.record_evidence(p, _evidence())

    assert added == 1
    assert p.rounds == 1


def test_repeated_bare_calls_keep_advancing() -> None:
    """Fails without the fix: every call is a no-op, so this stays at 0."""
    p = _plan()

    iterate.record_evidence(p, _evidence())
    iterate.record_evidence(p, _evidence())

    assert p.rounds == 2


def test_explicit_round_num_sets_the_round_directly() -> None:
    p = _plan()

    iterate.record_evidence(p, [], round_num=3)

    assert p.rounds == 3


def test_explicit_round_num_zero_is_not_collapsed_into_unspecified() -> None:
    """The bug this test guards against is specific to a 0-as-sentinel
    design: if round_num=0 meant "not specified" instead of "really zero",
    this call would auto-increment to 3 instead of staying at 2 -- silently
    overriding a caller's explicit round number with a guess.
    """
    p = _plan()
    iterate.record_evidence(p, [], round_num=2)

    iterate.record_evidence(p, [], round_num=0)

    assert p.rounds == 2


def test_round_num_never_moves_the_counter_backward() -> None:
    """Boundary this fix must not overshoot: an out-of-order or repeated
    round number must not un-advance plan.rounds."""
    p = _plan()
    iterate.record_evidence(p, [], round_num=10)

    iterate.record_evidence(p, [], round_num=3)

    assert p.rounds == 10


def test_unmatched_subquestion_id_is_not_counted_but_still_advances_round() -> None:
    """Guard: passes either way by design -- round advancement must not be
    gated on evidence actually matching a subquestion."""
    p = _plan()

    added = iterate.record_evidence(p, _evidence(sq_id="sq-does-not-exist"))

    assert added == 0
    assert p.rounds == 1


def test_plan_done_flips_when_coverage_reaches_target() -> None:
    """Guard: passes either way by design -- unrelated to round tracking,
    pinned so this fix doesn't regress it."""
    p = _plan(target_sources=1)

    iterate.record_evidence(p, _evidence())

    assert p.done is True


# --- the CLI path (the issue's own literal repro) ---------------------------


def test_cli_record_across_two_rounds_matches_the_issues_own_repro(
    tmp_path: Path,
) -> None:
    """Regression guard for the exact reproduction steps in #2268 -- these
    already passed before this fix (main() sets plan.rounds separately,
    before record_evidence runs), so this pins that main() and the now-fixed
    record_evidence agree rather than double-applying or conflicting."""
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(_plan().model_dump_json(indent=2), encoding="utf-8")
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text("[]", encoding="utf-8")

    for round_num in (1, 2):
        loaded = iterate.load_plan(plan_path)
        evidence = iterate.json.loads(evidence_path.read_text())
        iterate.record_evidence(loaded, evidence, round_num=round_num)
        iterate.save_plan(loaded, plan_path)

    final = iterate.load_plan(plan_path)
    assert final.rounds == 2


# --- real artifact: the compiled report -------------------------------------


def test_compiled_report_reflects_the_advanced_round_count() -> None:
    p = _plan()
    iterate.record_evidence(p, _evidence())
    iterate.record_evidence(p, _evidence())
    iterate.record_evidence(p, [], round_num=3)

    rendered = compile_mod.render(p)

    assert "Rounds: 3." in rendered
    assert "This report was assembled across 3 research rounds." in rendered
