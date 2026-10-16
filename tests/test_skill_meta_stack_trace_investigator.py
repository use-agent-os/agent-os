"""``meta-stack-trace-investigator`` and the ``stack-trace-*-probe`` family.

The five probes (``stack-trace-{generic,go,js,python,rust}-probe``) each set
``disable-model-invocation: true`` and describe themselves as "Internal
helper for meta-stack-trace-investigator" -- but no such skill existed
anywhere in the bundled catalog, so nothing could ever call ``skill_view``
on them. This orchestrator is that caller. These tests pin two things: the
orchestrator is actually offered (unlike its children) and actually wired to
every probe it claims to dispatch to, and -- as a general regression guard --
every bundled skill's "Internal helper for X" claim resolves to a real,
loaded skill, so a probe family can't go orphaned like this again.
"""

from __future__ import annotations

import re
from pathlib import Path

from agentos.skills.loader import SkillLoader

BUNDLED_DIR = Path(__file__).resolve().parent.parent / "src" / "agentos" / "skills" / "bundled"

_PROBE_NAMES = (
    "stack-trace-generic-probe",
    "stack-trace-go-probe",
    "stack-trace-js-probe",
    "stack-trace-python-probe",
    "stack-trace-rust-probe",
)

_INTERNAL_HELPER_RE = re.compile(r"Internal helper for ([\w-]+)")


def _load_bundled():
    return SkillLoader(bundled_dir=BUNDLED_DIR).load_all()


def test_the_orchestrator_is_offered_to_the_model_unlike_its_probes() -> None:
    skills = {s.name: s for s in _load_bundled()}

    orchestrator = skills["meta-stack-trace-investigator"]
    assert orchestrator.user_invocable is True
    assert orchestrator.disable_model_invocation is False

    for probe_name in _PROBE_NAMES:
        probe = skills[probe_name]
        assert probe.user_invocable is False
        assert probe.disable_model_invocation is True


def test_the_orchestrator_body_names_every_probe_it_dispatches_to() -> None:
    content = (BUNDLED_DIR / "meta-stack-trace-investigator" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    for probe_name in _PROBE_NAMES:
        assert probe_name in content


def test_every_declared_internal_helper_resolves_to_a_loaded_skill() -> None:
    """The defect this skill fixes, generalized: a skill's own description can
    claim an orchestrator that was never built, and nothing short of reading
    every SKILL.md by hand would notice. Any future "Internal helper for X"
    must name a skill that is actually in the catalog, not a promise."""
    skills_by_name = {s.name: s for s in _load_bundled()}
    claimed_orchestrators: set[str] = set()

    for skill in skills_by_name.values():
        for match in _INTERNAL_HELPER_RE.finditer(skill.description or ""):
            claimed_orchestrators.add(match.group(1))

    assert claimed_orchestrators, "expected at least the stack-trace-*-probe family to match"
    missing = claimed_orchestrators - set(skills_by_name)
    assert not missing, f"declared as an internal helper for a skill that doesn't exist: {missing}"
