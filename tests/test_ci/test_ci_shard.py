"""The CI test shards split the suite by file and together run it exactly once.

``tests/ci_shard.py`` picks each shard's files; ``scripts/ci_test_timings.py``
checks the shards' reports and refits the timings from them.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType
from typing import Any

import ci_shard
import pytest

REPO = Path(__file__).resolve().parents[2]


def _load_timings_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ci_test_timings", REPO / "scripts" / "ci_test_timings.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


timings_script = _load_timings_script()


def _report(shard: str, total: int, selected: int, files: dict[str, float]) -> dict[str, Any]:
    return {"os": "Linux", "shard": shard, "total": total, "selected": selected, "files": files}


class TestAssignFiles:
    def test_every_file_lands_in_exactly_one_shard(self) -> None:
        files = [f"tests/test_{i}.py" for i in range(37)]
        timings = {name: float(i % 7) for i, name in enumerate(files[:30])}
        shards = ci_shard.assign_files(files + files[:5], 4, timings)

        assert len(shards) == 4
        flat = [name for shard in shards for name in shard]
        assert sorted(flat) == sorted(files)

    def test_the_split_depends_only_on_the_files_and_timings(self) -> None:
        files = [f"tests/test_{i}.py" for i in range(20)]
        timings = {name: float(i) for i, name in enumerate(files)}

        assert ci_shard.assign_files(files, 3, timings) == ci_shard.assign_files(
            list(reversed(files)), 3, timings
        )

    def test_slow_files_are_spread_across_shards(self) -> None:
        timings = {"a.py": 100.0, "b.py": 90.0, "c.py": 80.0, "d.py": 1.0, "e.py": 1.0}
        shards = ci_shard.assign_files(timings, 3, timings)

        assert [shard[0] for shard in shards] == ["a.py", "b.py", "c.py"]
        assert sorted(shards[2]) == ["c.py", "d.py", "e.py"]

    def test_a_file_without_a_timing_costs_the_average(self) -> None:
        timings = {"a.py": 10.0, "b.py": 2.0}
        # new.py costs 6: after a(10) and b(2), it joins b's shard.
        assert ci_shard.assign_files(["a.py", "b.py", "new.py"], 2, timings) == [
            ["a.py"],
            ["new.py", "b.py"],
        ]

    def test_more_shards_than_files_leaves_some_empty(self) -> None:
        assert ci_shard.assign_files(["a.py"], 3, {}) == [["a.py"], [], []]


@pytest.mark.parametrize("value", ["0/3", "4/3", "1/0", "x/3", "3", ""])
def test_a_bad_shard_spec_is_a_usage_error(value: str) -> None:
    with pytest.raises(pytest.UsageError):
        ci_shard.parse_shard(value)


def test_timings_fall_back_to_another_os_table(tmp_path: Path) -> None:
    path = tmp_path / "timings.json"
    path.write_text(json.dumps({"Linux": {"a.py": 1.5}}), encoding="utf-8")

    assert ci_shard.load_timings(path, "Windows") == {"a.py": 1.5}
    assert ci_shard.load_timings(tmp_path / "missing.json", "Linux") == {}


def test_the_committed_timings_cover_most_of_the_suite() -> None:
    """Stale timings only cost balance, but an empty table would cost all of it."""
    timings = ci_shard.load_timings(system="Linux")
    files = list((REPO / "tests").rglob("test_*.py"))

    assert len(timings) > len(files) // 2


class TestCheckPartition:
    def test_shards_that_add_up_to_the_suite_pass(self) -> None:
        reports = [
            _report("1/2", 5, 3, {"a.py": 1.0, "b.py": 1.0}),
            _report("2/2", 5, 2, {"c.py": 1.0}),
        ]
        assert timings_script.check_partition(reports) == []

    def test_a_missing_shard_fails(self) -> None:
        problems = timings_script.check_partition([_report("1/2", 5, 3, {"a.py": 1.0})])

        assert any("expected shards" in p for p in problems)
        assert any("kept 3 tests of 5" in p for p in problems)

    def test_a_file_in_two_shards_fails(self) -> None:
        reports = [_report("1/2", 4, 2, {"a.py": 1.0}), _report("2/2", 4, 2, {"a.py": 1.0})]

        assert timings_script.check_partition(reports) == ["a.py ran in shard 1/2 and 2/2"]

    def test_shards_of_different_collections_fail(self) -> None:
        reports = [_report("1/2", 5, 3, {"a.py": 1.0}), _report("2/2", 6, 2, {"b.py": 1.0})]

        assert any("different suites" in p for p in timings_script.check_partition(reports))

    def test_shards_that_disagree_on_n_fail(self) -> None:
        reports = [_report("1/2", 5, 3, {}), _report("2/3", 5, 2, {})]

        assert timings_script.check_partition(reports) == ["shards disagree on N: ['1/2', '2/3']"]

    def test_a_required_os_without_reports_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "r.json"
        path.write_text(json.dumps(_report("1/1", 1, 1, {"a.py": 1.0})), encoding="utf-8")

        assert timings_script.main(["check", "--require", "Linux", str(path)]) == 0
        assert timings_script.main(["check", "--require", "Windows", str(path)]) == 1
        assert "no shard reports for Windows" in capsys.readouterr().out


def test_fit_replaces_the_reported_os_and_keeps_the_others() -> None:
    existing = {"Linux": {"old.py": 9.0}, "Windows": {"w.py": 3.0}}
    reports = {"Linux": [_report("1/2", 2, 1, {"b.py": 1.234}), _report("2/2", 2, 1, {"a.py": 2})]}

    assert timings_script.fit_timings(reports, existing) == {
        "Linux": {"a.py": 2.0, "b.py": 1.23},
        "Windows": {"w.py": 3.0},
    }


# A small tree with a parametrized file, a skip, a marker-deselected test and
# a subdirectory: after ``-m "not slow"`` it holds 9 tests in 5 files.
_TREE = {
    "pytest.ini": """
        [pytest]
        markers =
            slow: deselected below
    """,
    "test_a.py": """
        import pytest

        @pytest.mark.parametrize("n", [1, 2, 3])
        def test_a(n):
            assert n
    """,
    "test_b.py": """
        import pytest

        def test_b():
            pass

        @pytest.mark.skip(reason="still counted as collected")
        def test_b_skipped():
            pass
    """,
    "sub/test_c.py": """
        import pytest

        def test_c():
            pass

        def test_c_again():
            pass

        @pytest.mark.slow
        def test_c_slow():
            pass
    """,
    "sub/test_d.py": "def test_d():\n    pass\n",
    "test_e.py": "def test_e():\n    pass\n",
}


def _run_shard(root: Path, shard: str, report: Path, *extra: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["PYTHONPATH"] = os.pathsep.join([str(REPO / "tests"), env.get("PYTHONPATH", "")])
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "ci_shard",
            "-p",
            "no:cacheprovider",
            "-m",
            "not slow",
            f"--shard={shard}",
            f"--shard-report={report}",
            *extra,
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_shards_partition_a_real_run_with_and_without_xdist(tmp_path: Path) -> None:
    root = tmp_path / "suite"
    for name, body in _TREE.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")

    for mode, extra in (("serial", ()), ("xdist", ("-n", "2"))):
        paths = [tmp_path / mode / f"shard-{i}.json" for i in (1, 2)]
        for index, path in enumerate(paths, start=1):
            _run_shard(root, f"{index}/2", path, *extra)
        reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]

        assert [r["total"] for r in reports] == [9, 9], mode
        assert sum(r["selected"] for r in reports) == 9, mode
        assert sorted(name for r in reports for name in r["files"]) == [
            "sub/test_c.py",
            "sub/test_d.py",
            "test_a.py",
            "test_b.py",
            "test_e.py",
        ], mode
        argv = ["check", "--require", reports[0]["os"], *map(str, paths)]
        assert timings_script.main(argv) == 0, mode
