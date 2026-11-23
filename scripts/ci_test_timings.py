#!/usr/bin/env python3
"""Check and refit the timing-balanced CI test shards.

Each CI shard runs ``pytest --shard=I/N --shard-report=PATH`` (see
``tests/ci_shard.py``). ``check`` reads those reports and fails unless, per
OS, the shards are exactly 1..N, every shard collected the same number of
tests, no test file ran in two shards, and the kept counts add up to the
collection, i.e. the shards partition the suite. ``fit`` folds the reports'
per-file durations into ``.github/ci-test-timings.json`` so the next split is
balanced on fresh numbers; OS tables the reports do not cover are kept. Give it
the reports of several runs: each shard runs on a runner of its own, whose
speed varies from run to run, so a file's timing is its median over them.

Standard library only, so it runs on a bare runner without the project env.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

TIMINGS_PATH = Path(__file__).resolve().parents[1] / ".github" / "ci-test-timings.json"


def _load_reports(paths: list[Path]) -> dict[str, list[dict[str, Any]]]:
    by_os: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in paths:
        report = json.loads(path.read_text(encoding="utf-8"))
        report["path"] = str(path)
        by_os[str(report["os"])].append(report)
    return dict(by_os)


def check_partition(reports: list[dict[str, Any]]) -> list[str]:
    """Problems that stop ``reports`` (one OS) from partitioning the suite."""
    problems: list[str] = []
    shards = sorted(str(report["shard"]) for report in reports)
    counts = {int(shard.partition("/")[2]) for shard in shards}
    if len(counts) != 1:
        return [f"shards disagree on N: {shards}"]
    (count,) = counts
    expected = [f"{index}/{count}" for index in range(1, count + 1)]
    if shards != sorted(expected):
        problems.append(f"expected shards {expected}, got {shards}")

    totals = {int(report["total"]) for report in reports}
    if len(totals) != 1:
        problems.append(f"shards collected different suites: totals {sorted(totals)}")
    selected = sum(int(report["selected"]) for report in reports)
    if len(totals) == 1 and selected != next(iter(totals)):
        problems.append(f"shards kept {selected} tests of {next(iter(totals))} collected")

    owner: dict[str, str] = {}
    for report in reports:
        for name in report["files"]:
            if name in owner:
                problems.append(f"{name} ran in shard {owner[name]} and {report['shard']}")
            owner[name] = str(report["shard"])
    return problems


def fit_timings(
    reports: dict[str, list[dict[str, Any]]], existing: dict[str, dict[str, float]]
) -> dict[str, dict[str, float]]:
    """Each reported OS's table, a file's median over the reports naming it."""
    tables = dict(existing)
    for system, system_reports in reports.items():
        samples: defaultdict[str, list[float]] = defaultdict(list)
        for report in system_reports:
            for name, seconds in report["files"].items():
                samples[str(name)].append(float(seconds))
        tables[system] = {
            name: round(statistics.median(values), 2) for name, values in sorted(samples.items())
        }
    return dict(sorted(tables.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="fail unless each OS's shards partition the suite")
    check.add_argument("reports", nargs="*", type=Path)
    check.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="OS",
        help="fail if no report covers this platform.system() name",
    )
    fit = sub.add_parser(
        "fit", help="refit the committed timings from shard reports (of one run or several)"
    )
    fit.add_argument("reports", nargs="+", type=Path)
    fit.add_argument("--out", type=Path, default=TIMINGS_PATH)
    args = parser.parse_args(argv)

    reports = _load_reports(args.reports)
    if args.command == "fit":
        try:
            existing = json.loads(args.out.read_text(encoding="utf-8"))
        except FileNotFoundError:
            existing = {}
        tables = fit_timings(reports, existing)
        args.out.write_text(json.dumps(tables, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {args.out}: " + ", ".join(f"{k} {len(v)} files" for k, v in tables.items()))
        return 0

    failed = False
    for system in args.require:
        if system not in reports:
            print(f"ERROR: no shard reports for {system}")
            failed = True
    for system, system_reports in sorted(reports.items()):
        problems = check_partition(system_reports)
        for problem in problems:
            print(f"ERROR: {system}: {problem}")
        if problems:
            failed = True
        else:
            total = system_reports[0]["total"]
            print(f"{system}: {len(system_reports)} shards partition {total} tests")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
