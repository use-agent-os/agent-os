"""Split the suite across CI jobs by recorded per-file durations.

``--shard=I/N`` keeps only the test files assigned to shard ``I`` of ``N``.
Assignment is a pure function of the collected file list and the recorded
timings in ``.github/ci-test-timings.json``, so every job that collects the
same tree computes the same partition and each file runs in exactly one shard.
A file with no recorded timing still lands somewhere, at the average cost;
stale timings only cost balance, never coverage.

``--shard-report=PATH`` writes the shard's per-file durations and how many
tests it kept out of how many it collected. ``scripts/ci_test_timings.py``
checks those reports add up to the whole suite and refits the timings file
from them.

Imported by ``tests/conftest.py``; not a test module.
"""

from __future__ import annotations

import json
import platform
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

TIMINGS_PATH = Path(__file__).resolve().parents[1] / ".github" / "ci-test-timings.json"

_COUNTS_KEY = pytest.StashKey[tuple[int, int]]()
_DURATIONS_KEY = pytest.StashKey[defaultdict[str, float]]()
_WORKER_COUNTS = "ci_shard_counts"


def parse_shard(value: str) -> tuple[int, int]:
    index_text, _, count_text = value.partition("/")
    try:
        index, count = int(index_text), int(count_text)
    except ValueError:
        raise pytest.UsageError(f"--shard expects I/N, got {value!r}") from None
    if count < 1 or not 1 <= index <= count:
        raise pytest.UsageError(f"--shard index must be within 1..N, got {value!r}")
    return index, count


def load_timings(path: Path = TIMINGS_PATH, system: str | None = None) -> dict[str, float]:
    """The recorded per-file seconds for this OS, or another OS's if it has none."""
    try:
        tables = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    table = tables.get(system or platform.system())
    if not table:
        table = next((t for t in tables.values() if t), {})
    return {str(name): float(seconds) for name, seconds in table.items()}


def assign_files(files: Iterable[str], count: int, timings: Mapping[str, float]) -> list[list[str]]:
    """Longest file first onto the least-loaded shard (ties go to the lower index)."""
    unique = sorted(set(files))
    known = [timings[name] for name in unique if name in timings]
    default = sum(known) / len(known) if known else 1.0

    def cost(name: str) -> float:
        return timings.get(name, default)

    loads = [0.0] * count
    shards: list[list[str]] = [[] for _ in range(count)]
    for name in sorted(unique, key=lambda name: (-cost(name), name)):
        target = min(range(count), key=lambda index: (loads[index], index))
        shards[target].append(name)
        loads[target] += cost(name)
    return shards


def _file_of(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("ci-shard")
    group.addoption(
        "--shard",
        default=None,
        metavar="I/N",
        help="run only the test files timing-balanced into shard I of N",
    )
    group.addoption(
        "--shard-report",
        default=None,
        metavar="PATH",
        help="write this run's per-file durations and selection counts as JSON",
    )


def pytest_configure(config: pytest.Config) -> None:
    shard = config.getoption("--shard")
    if shard:
        parse_shard(shard)
    durations: defaultdict[str, float] = defaultdict(float)
    config.stash[_DURATIONS_KEY] = durations
    # Under xdist the controller sees every worker's reports; a worker would
    # only see its own.
    if config.getoption("--shard-report") and not hasattr(config, "workerinput"):
        config.pluginmanager.register(_DurationRecorder(durations), "ci-shard-durations")


# Last, so the split sees what ``-m``/``-k`` left rather than the raw collection.
@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    total = len(items)
    shard = config.getoption("--shard")
    if shard:
        index, count = parse_shard(shard)
        mine = set(
            assign_files((_file_of(i.nodeid) for i in items), count, load_timings())[index - 1]
        )
        kept = [item for item in items if _file_of(item.nodeid) in mine]
        dropped = [item for item in items if _file_of(item.nodeid) not in mine]
        if dropped:
            config.hook.pytest_deselected(items=dropped)
        items[:] = kept
    counts = (total, len(items))
    config.stash[_COUNTS_KEY] = counts
    # An xdist worker collects; the controller that writes the report does not.
    workeroutput = getattr(config, "workeroutput", None)
    if workeroutput is not None:
        workeroutput[_WORKER_COUNTS] = list(counts)


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: object) -> None:
    counts = getattr(node, "workeroutput", {}).get(_WORKER_COUNTS)
    if counts is not None:
        node.config.stash[_COUNTS_KEY] = (int(counts[0]), int(counts[1]))


class _DurationRecorder:
    """Sums setup + call + teardown seconds per file, as the reports arrive."""

    def __init__(self, durations: defaultdict[str, float]) -> None:
        self._durations = durations

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self._durations[_file_of(report.nodeid)] += report.duration


def pytest_sessionfinish(session: pytest.Session) -> None:
    config = session.config
    path = config.getoption("--shard-report")
    if not path or hasattr(config, "workerinput"):
        return
    total, selected = config.stash.get(_COUNTS_KEY, (0, 0))
    report = {
        "os": platform.system(),
        "shard": config.getoption("--shard") or "1/1",
        "total": total,
        "selected": selected,
        "files": {
            name: round(seconds, 3)
            for name, seconds in sorted(config.stash[_DURATIONS_KEY].items())
        },
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
