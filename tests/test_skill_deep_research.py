"""deep-research skill unit tests."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "src" / "agentos" / "skills" / "bundled" / "deep-research" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import compile  # type: ignore[import-not-found]  # noqa: E402
import iterate  # type: ignore[import-not-found]  # noqa: E402
import plan  # type: ignore[import-not-found]  # noqa: E402


def test_record_evidence_advances_plan_rounds(tmp_path: Path) -> None:
    """record_evidence advances plan.rounds both programmatically and with round_num."""
    p = plan.Plan(
        question="What is AgentOS?",
        depth="overview",
        created_at="2026-09-15",
        subquestions=[
            plan.SubQuestion(
                id="sq-1",
                question="What is it?",
                target_sources=1,
            )
        ],
    )
    assert p.rounds == 0

    # 1. Programmatic call without round_num increments rounds by 1
    evidence = [
        {
            "subquestion_id": "sq-1",
            "url": "https://example.com",
            "title": "Example",
            "excerpt": "AgentOS runtime",
            "relevance": 1.0,
            "fetched_at": "2026-09-15",
        }
    ]
    added = iterate.record_evidence(p, evidence)
    assert added == 1
    assert p.rounds == 1

    # 2. Call with explicit round_num
    iterate.record_evidence(p, [], round_num=3)
    assert p.rounds == 3

    # 3. Report rendering displays updated rounds count
    rendered = compile.render(p)
    assert "Rounds: 3." in rendered
    assert "This report was assembled across 3 research rounds." in rendered
