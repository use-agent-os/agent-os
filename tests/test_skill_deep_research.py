"""deep-research scripts — validation of record and plan files."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "src" / "agentos" / "skills" / "bundled" / "deep-research" / "scripts"


def _import_scripts():
    sys.path.insert(0, str(SCRIPTS))
    try:
        import compile as compile_script  # type: ignore[import-not-found]
        import iterate  # type: ignore[import-not-found]
        import plan as plan_script  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    return plan_script, iterate, compile_script


def _make_sample_plan(tmp_path: Path) -> Path:
    plan_script, _, _ = _import_scripts()
    plan = plan_script.Plan(
        question="What is the impact of AI on programming?",
        depth="overview",
        created_at="2026-01-01T00:00:00Z",
        subquestions=plan_script.make_subquestions("test", "overview"),
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    return plan_path


@pytest.mark.parametrize(
    "record_text",
    [
        '{"subquestion_id": "sq-001", "url": "https://example.com"}',
        '"evidence"',
        "42",
        "null",
        "true",
    ],
)
def test_iterate_refuses_non_list_record_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    record_text: str,
) -> None:
    _, iterate, _ = _import_scripts()
    plan_path = _make_sample_plan(tmp_path)
    initial_plan = plan_path.read_text(encoding="utf-8")

    record_path = tmp_path / "evidence.json"
    record_path.write_text(record_text, encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        ["iterate.py", "--plan", str(plan_path), "--round", "1", "--record", str(record_path)],
    )

    assert iterate.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "must be a JSON list of evidence items" in captured.err
    assert (
        plan_path.read_text(encoding="utf-8") == initial_plan
    ), "refused record must not mutate plan"


@pytest.mark.parametrize(
    "record_bytes",
    [
        b'[{"subquestion_id": "sq-001",',
        '[{"subquestion_id": "sq-001"}]'.encode("utf-16"),
    ],
)
def test_iterate_refuses_invalid_json_record_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    record_bytes: bytes,
) -> None:
    _, iterate, _ = _import_scripts()
    plan_path = _make_sample_plan(tmp_path)
    initial_plan = plan_path.read_text(encoding="utf-8")

    record_path = tmp_path / "evidence.json"
    record_path.write_bytes(record_bytes)

    monkeypatch.setattr(
        sys,
        "argv",
        ["iterate.py", "--plan", str(plan_path), "--round", "1", "--record", str(record_path)],
    )

    assert iterate.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "is not valid JSON" in captured.err
    assert plan_path.read_text(encoding="utf-8") == initial_plan


def test_iterate_refuses_invalid_plan_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, iterate, _ = _import_scripts()
    plan_path = tmp_path / "corrupt_plan.json"
    plan_path.write_text('{"bad": "schema"}', encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        ["iterate.py", "--plan", str(plan_path), "--round", "1", "--print-fetches"],
    )

    assert iterate.main() == 2
    captured = capsys.readouterr()
    assert "is not valid JSON or plan schema" in captured.err


def test_compile_refuses_invalid_plan_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, compile_script = _import_scripts()
    plan_path = tmp_path / "corrupt_plan.json"
    plan_path.write_text('{"bad": "schema"}', encoding="utf-8")
    out_path = tmp_path / "report.md"

    monkeypatch.setattr(
        sys,
        "argv",
        ["compile.py", "--plan", str(plan_path), "--out", str(out_path)],
    )

    assert compile_script.main() == 2
    captured = capsys.readouterr()
    assert "is not valid JSON or plan schema" in captured.err
    assert not out_path.exists()
