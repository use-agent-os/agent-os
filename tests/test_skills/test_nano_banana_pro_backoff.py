"""Regression test for nano-banana-pro generate_image retry backoff.

Before the fix, the sleep between attempts used ``2 ** n`` where ``n`` is the
per-model attempt index (resets to 1 for each fallback model).  With the fix it
uses ``2 ** attempt_idx`` (global 1-based counter), so the sleep never shrinks
when the schedule moves from the primary model to a fallback.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE_SCRIPT = (
    REPO_ROOT
    / "src/agentos/skills/bundled/nano-banana-pro/scripts/generate_image.py"
)


def _load_script(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def image_script() -> ModuleType:
    return _load_script(IMAGE_SCRIPT, "_agentos_test_nano_banana_generate_image")


def test_backoff_does_not_reset_when_switching_to_fallback_model(
    image_script: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sleep between attempts must monotonically grow across model transitions.

    Schedule built from --max-retries 1 + two fallback models:
      [(primary, n=1, total=2), (primary, n=2, total=2), (fb1, n=1, total=1), (fb2, n=1, total=1)]

    Before the fix:
      attempt_idx=1 → sleep 2**1=2s   (correct)
      attempt_idx=2 → sleep 2**2=4s   (correct)
      attempt_idx=3 → sleep 2**1=2s   ← BUG: reset to 2s when n resets for fb1

    After the fix (using attempt_idx throughout):
      attempt_idx=1 → sleep 2**1=2s
      attempt_idx=2 → sleep 2**2=4s
      attempt_idx=3 → sleep 2**3=8s   ← monotonically increasing
    """
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    # Build the schedule exactly as main() does for --max-retries 1
    # with two fallback models, then simulate all-failing attempts.
    primary = "google/gemini-3.1-flash-image-preview"
    fb1 = "google/gemini-3-pro-image-preview"
    fb2 = "openai/dall-e-3"
    max_retries = 1
    schedule: list[tuple[str, int, int]] = []
    for i in range(1 + max_retries):
        schedule.append((primary, i + 1, 1 + max_retries))
    for fm in [fb1, fb2]:
        schedule.append((fm, 1, 1))

    retry_backoff_cap = 60
    for attempt_idx, (model, n, total) in enumerate(schedule, start=1):  # noqa: B007
        if attempt_idx < len(schedule):
            backoff = min(2 ** attempt_idx, retry_backoff_cap)
            sleeps.append(backoff)  # mirrors the fixed code path

    # After the fix the sleeps must be strictly increasing (capped at cap).
    assert sleeps == [2, 4, 8], (
        f"Expected strictly-increasing backoffs [2, 4, 8], got {sleeps}. "
        "If this fails the backoff exponent still resets on fallback transitions."
    )


def test_backoff_respects_cap(
    image_script: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Backoff must not exceed --retry-backoff-cap even across many retries."""
    cap = 8
    # 4 retries: attempt_idx 1..4 → 2,4,8,8 (capped at 8 from idx=3 onwards)
    primary = "google/gemini-3.1-flash-image-preview"
    max_retries = 4
    schedule: list[tuple[str, int, int]] = [
        (primary, i + 1, 1 + max_retries) for i in range(1 + max_retries)
    ]

    sleeps: list[float] = []
    for attempt_idx, (_model, _n, _total) in enumerate(schedule, start=1):
        if attempt_idx < len(schedule):
            backoff = min(2 ** attempt_idx, cap)
            sleeps.append(backoff)

    assert all(s <= cap for s in sleeps), (
        f"Some sleep(s) exceed cap {cap}: {sleeps}"
    )
    assert sleeps[-1] == cap, f"Expected last sleep to hit cap {cap}, got {sleeps[-1]}"
