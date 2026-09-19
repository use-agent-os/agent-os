"""``edit_file`` must not do its matching work on the caller's event loop.

The fuzzy matcher is the CPU-bound part of an edit: on a miss it slides the
pattern's window down the whole file, twice — once looking for a match, once
building the "closest match" hint. Run inline that blocks whatever loop called
the tool, so two guards keep a failed edit cheap: the match runs in a worker
thread, and the sweeping strategies decline outright on inputs too big to
sweep.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agentos.tools.builtin import filesystem as fs
from agentos.tools.fuzzy_match import _MAX_SWEEP_CELLS, FuzzyMatchError, FuzzyMatchResult
from agentos.tools.fuzzy_match import fuzzy_find_and_replace as real_fuzzy_find_and_replace
from agentos.tools.types import CallerKind, SafeToolError, ToolContext, current_tool_context


def _original_async(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    """Unwrap the @tool and @sandboxed decorators to reach the implementation."""

    return fn.__wrapped__.__wrapped__  # type: ignore[attr-defined, no-any-return]


edit_file = _original_async(fs.edit_file)


@contextmanager
def tool_context(workspace: Path) -> Iterator[None]:
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            channel_kind="cli",
            channel_id="cli:test",
            workspace_dir=str(workspace),
        )
    )
    try:
        yield
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_matching_runs_off_the_calling_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n", encoding="utf-8")

    seen: list[threading.Thread] = []

    def _record(content: str, old_text: str, new_text: str) -> FuzzyMatchResult:
        seen.append(threading.current_thread())
        return FuzzyMatchResult(
            updated="value = 2\n",
            match_count=1,
            strategy="exact",
            spans=((0, 9),),
        )

    monkeypatch.setattr(fs, "fuzzy_find_and_replace", _record)

    caller = threading.current_thread()
    with tool_context(tmp_path):
        await edit_file(str(target), "value = 1", "value = 2")

    assert seen, "the matcher was never called"
    assert seen[0] is not caller, "the matcher ran on the calling (event loop) thread"
    assert target.read_text(encoding="utf-8") == "value = 2\n"


def _sweep_corpus(line_count: int) -> str:
    return "".join(f"v_{index} = z({index})\n" for index in range(line_count))


# Twelve lines keeps the pattern short enough that ``SequenceMatcher`` scores it
# without its autojunk heuristic, so the hint below is a real one.
_PATTERN_LINES = 12
_PATTERN = "".join(f"qb_{index}: w({index})\n" for index in range(_PATTERN_LINES))


def test_hint_survives_below_the_sweep_bound() -> None:
    content = _sweep_corpus(_MAX_SWEEP_CELLS // _PATTERN_LINES - 500)

    with pytest.raises(FuzzyMatchError) as excinfo:
        real_fuzzy_find_and_replace(content, _PATTERN, "x = 1\n")

    # Under the bound nothing changes: the sweep still runs and still reports
    # the region that came closest.
    assert excinfo.value.hint.startswith("line 1 (")


def test_sweep_bound_drops_the_hint_on_oversized_input() -> None:
    content = _sweep_corpus(_MAX_SWEEP_CELLS // _PATTERN_LINES + 500)

    with pytest.raises(FuzzyMatchError) as excinfo:
        real_fuzzy_find_and_replace(content, _PATTERN, "x = 1\n")

    assert str(excinfo.value) == "old_text not found"
    assert excinfo.value.hint == ""


@pytest.mark.asyncio
async def test_oversized_miss_fails_fast_without_a_hint(tmp_path: Path) -> None:
    # 6_000 x 40 = 240_000 cells, comfortably past the bound: the resemblance
    # strategies and the hint sweep must all decline rather than grind.
    target = tmp_path / "big.py"
    target.write_text(
        "".join(f"alpha_{index} = compute_value({index}, flag=True)\n" for index in range(6_000)),
        encoding="utf-8",
    )
    old_text = "".join(f"zulu_{index} = derive_other({index}, flag=False)\n" for index in range(40))

    started = time.perf_counter()
    with tool_context(tmp_path):
        with pytest.raises(SafeToolError) as excinfo:
            await edit_file(str(target), old_text, "x = 1\n")
    elapsed = time.perf_counter() - started

    message = str(excinfo.value)
    assert "old_text not found" in message
    assert "Closest match" not in message
    # Coarse on purpose: the claim is "returns promptly", not a microbenchmark.
    # Unbounded this same input runs for minutes.
    assert elapsed < 5.0, f"oversized miss took {elapsed:.1f}s"
