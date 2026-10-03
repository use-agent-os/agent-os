"""``ai-video-script``'s N_SHOTS default and range must agree with themselves.

The skill stated its default shot count as both 3 (description, body) and 5
(the `with.N_SHOTS` bullet), and its own output-format spec capped N_SHOTS at
`<int 3-5>` while the very next section instructs "for any N_SHOTS between 1
and 10" and documents semantics for every count from 1 through 10. Both
contradictions could send the model either the wrong default or a spuriously
narrow accepted range for the exact same field.
"""

from __future__ import annotations

from pathlib import Path

SKILL_MD = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "ai-video-script"
    / "SKILL.md"
)


def test_n_shots_default_is_stated_consistently() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")

    assert "3 shots by default" in text
    assert "The default emits 3 shots" in text
    assert "N_SHOTS` override (3 default" in text
    # The stale, contradicting default must not reappear.
    assert "5 default" not in text


def test_n_shots_range_is_stated_consistently() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")

    assert "1-10 allowed" in text
    assert "N_SHOTS: <int 1-10>" in text
    assert "between 1 and 10" in text
    # The stale, narrower range must not reappear in the output-format spec.
    assert "<int 3-5>" not in text
