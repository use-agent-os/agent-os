"""CI guard: the decision-log aggregate library must stay importable.

`observability.decision_log_aggregate` is consumed by
`skills/bundled/history-explorer/scripts/explore.py` — a subprocess
entrypoint that imports via the `sys.path.insert` bootstrap.

If somebody renames or relocates the module, this test fails loudly
so the duplicated definitions don't drift back into the bundled
script.
"""

from __future__ import annotations


def test_module_importable_with_expected_public_api() -> None:
    from agentos.observability import decision_log_aggregate as agg

    for name in (
        "parse_log_line",
        "within_window",
        "aggregate_co_occurrences",
    ):
        assert hasattr(agg, name), f"public API drifted: missing {name!r}"

    assert name in agg.__all__  # last name from the loop is enough as smoke


def test_history_explorer_script_imports_from_aggregate_module() -> None:
    """The bundled script must not redefine the lifted functions."""

    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "agentos"
        / "skills"
        / "bundled"
        / "history-explorer"
        / "scripts"
        / "explore.py"
    )
    text = script.read_text(encoding="utf-8")
    assert "from agentos.observability.decision_log_aggregate import" in text
    assert "def aggregate_co_occurrences" not in text, (
        "explore.py must import aggregate_co_occurrences, not redefine it"
    )
