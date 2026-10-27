"""Pure-logic tests for the degenerate-output repetition guard."""

from __future__ import annotations

import math
import random
from types import SimpleNamespace

import pytest

from agentos.engine.repetition_guard import (
    RepetitionGuardSettings,
    RepetitionTrip,
    RepetitionWatch,
    StreamRepetitionGuard,
    find_repetition,
    kept_text,
    repetition_guard_settings_from,
)

_WORDS = (
    "request cache model router token budget window stream parser schema "
    "session gateway policy sandbox ledger wallet signal channel worker queue"
).split()


_REGIONS = ("us-east", "eu-west", "ap-south")


def _sentence(rng: random.Random) -> str:
    words = [rng.choice(_WORDS) for _ in range(rng.randint(7, 15))]
    return " ".join(words).capitalize() + "."


def _prose(chars: int, seed: int = 7) -> str:
    rng = random.Random(seed)
    paragraphs: list[str] = []
    size = 0
    while size < chars:
        paragraph = " ".join(_sentence(rng) for _ in range(rng.randint(3, 7)))
        paragraphs.append(paragraph)
        size += len(paragraph) + 2
    return "\n\n".join(paragraphs)[:chars]


def _stream(text: str, *, chunk: int = 23, settings: RepetitionGuardSettings | None = None):
    watch = RepetitionWatch(settings)
    for start in range(0, len(text), chunk):
        trip = watch.feed(text[start : start + chunk])
        if trip is not None:
            return watch, trip
    return watch, None


# ---------------------------------------------------------------------------
# Long legitimate output must never trip
# ---------------------------------------------------------------------------


def _markdown_table(rows: int) -> str:
    rng = random.Random(3)
    header = "| # | Service | Region | p50 ms | p99 ms | Status | Notes |\n"
    header += "|---|---|---|---:|---:|---|---|\n"
    body = "".join(
        f"| {i} | svc-{rng.choice(_WORDS)}-{i % 17} | {rng.choice(_REGIONS)}"
        f" | {rng.randint(5, 90)} | {rng.randint(100, 900)} | "
        f"{rng.choice(['ok', 'ok', 'ok', 'degraded'])} | {_sentence(rng)} |\n"
        for i in range(1, rows + 1)
    )
    return header + body


def _boilerplate_tests(count: int) -> str:
    blocks = [
        f"""

@pytest.mark.parametrize("payload", [None, {{}}, {{"id": {i}}}])
def test_endpoint_{i:03d}_accepts_payload(client, payload):
    response = client.post("/api/v1/items/{i}", json=payload)
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.headers["content-type"] == "application/json"
"""
        for i in range(count)
    ]
    return "import pytest\n" + "".join(blocks)


def _boilerplate_classes(count: int) -> str:
    return "".join(
        f'''

class Handler{i}(BaseHandler):
    """Handle the event_{i} notification."""

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.name = "handler_{i}"

    def handle(self, event: Event) -> Result:
        return self.dispatch(event)
'''
        for i in range(count)
    )


def _numbered_list(items: int) -> str:
    return "".join(
        f"{i}. Review the configuration of item {i} and confirm the defaults are correct.\n"
        for i in range(1, items + 1)
    )


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("markdown_table", _markdown_table(1_500)),
        ("boilerplate_tests", _boilerplate_tests(700)),
        ("boilerplate_classes", _boilerplate_classes(500)),
        ("numbered_list_300", _numbered_list(300)),
        ("numbered_list_3000", _numbered_list(3_000)),
        ("prose", _prose(150_000)),
    ],
)
def test_long_legitimate_output_does_not_trip(name: str, text: str) -> None:
    assert len(text) > 20_000, name
    watch, trip = _stream(text)

    assert trip is None, (name, trip)
    assert watch.checks >= 1


def test_text_below_first_check_is_never_inspected() -> None:
    watch, trip = _stream("again and again " * 900)

    assert len("again and again " * 900) < 16_000
    assert trip is None
    assert watch.checks == 0


# ---------------------------------------------------------------------------
# Real loops trip, and the cut keeps one copy
# ---------------------------------------------------------------------------


def test_looped_line_trips_and_keeps_the_first_copy() -> None:
    lead = _prose(5_000)
    line = "I will now check the configuration file again to be sure.\n"
    text = lead + "\n" + line * 2_000

    _, trip = _stream(text)

    assert trip is not None
    assert trip.signal == "repeated_lines"
    assert trip.period == len(line)
    assert trip.repeats >= 5
    kept = kept_text(text[: trip.checked_chars], trip)
    assert kept.startswith(lead)
    assert kept.count("check the configuration file again") == 1


def test_looped_paragraph_trips() -> None:
    lead = _prose(8_000)
    paragraph = (
        "Let me reconsider the approach. The function reads the file, parses each row, "
        "and then writes the summary.\nI should double-check the parser before moving on.\n\n"
    )
    text = lead + paragraph * 400

    _, trip = _stream(text)

    assert trip is not None
    assert trip.signal == "repeated_window"
    assert trip.period == len(paragraph)
    kept = kept_text(text[: trip.checked_chars], trip)
    assert kept.startswith(lead)
    assert kept.count("Let me reconsider the approach.") == 1


def test_short_period_loop_trips() -> None:
    lead = _prose(3_000)
    text = lead + " the" * 10_000

    _, trip = _stream(text)

    assert trip is not None
    assert trip.signal == "short_period"
    assert trip.period == 4
    kept = kept_text(text[: trip.checked_chars], trip)
    assert kept.startswith(lead)
    assert len(kept) <= len(lead) + 4


def test_single_character_run_trips() -> None:
    text = _prose(2_000) + "\n" * 20_000

    _, trip = _stream(text)

    assert trip is not None
    assert trip.signal == "short_period"
    assert trip.period == 1


def test_dominant_line_with_small_variations_trips() -> None:
    line = "Checking the deployment status once more before reporting back to the user."
    text = _prose(2_000) + "".join(f"{line}\n{line}\n{line}\n(attempt {i})\n" for i in range(400))

    _, trip = _stream(text)

    assert trip is not None
    assert trip.signal == "repeated_lines"
    kept = kept_text(text[: trip.checked_chars], trip)
    assert kept.endswith(line)


def test_repeated_line_cut_ignores_an_early_legitimate_use() -> None:
    line = "Running the full test suite again now to confirm the result."
    early = _prose(3_000) + "\n" + line + "\n" + _prose(4_000, seed=11) + "\n"
    loop = "".join(f"{line}\n{line}\nstep {i}\n" for i in range(600))
    text = early + loop

    _, trip = _stream(text)

    assert trip is not None
    assert trip.signal == "repeated_lines"
    kept = kept_text(text[: trip.checked_chars], trip)
    # The early mention and the prose after it survive the cut.
    assert kept.startswith(early)


def test_loop_that_began_before_the_tail_is_traced_back() -> None:
    lead = _prose(4_000)
    unit = "The answer is still being computed, please hold on for a moment longer.\n"
    text = lead + "\n" + unit * 2_000
    settings = RepetitionGuardSettings(first_check_chars=1_000, tail_chars=8_000)
    trip = RepetitionWatch(settings).feed(text)

    assert trip is not None
    # The whole text arrived at once, so the inspected tail starts mid-loop...
    assert trip.loop_start > len(lead) + len(unit)
    # ...but the cut walks back to the first copy over the full text.
    kept = kept_text(text, trip)
    assert kept.startswith(lead)
    assert kept.count("still being computed") == 1


# ---------------------------------------------------------------------------
# Bounded cost
# ---------------------------------------------------------------------------


def test_check_schedule_is_bounded() -> None:
    settings = RepetitionGuardSettings()
    watch = RepetitionWatch(settings)
    text = _prose(1_200_000, seed=5)
    max_buffered = 0
    for start in range(0, len(text), 97):
        assert watch.feed(text[start : start + 97]) is None
        max_buffered = max(max_buffered, watch.buffered_chars)

    total = len(text)
    doubling = math.ceil(math.log2(settings.tail_chars / settings.first_check_chars))
    bound = doubling + 1 + math.ceil(total / settings.tail_chars)
    assert watch.total_chars == total
    assert 1 <= watch.checks <= bound
    assert max_buffered <= 2 * settings.tail_chars + 97


def test_checks_run_on_a_growing_schedule() -> None:
    settings = RepetitionGuardSettings(first_check_chars=1_000, tail_chars=4_000)
    watch = RepetitionWatch(settings)
    check_points: list[int] = []
    text = _prose(20_000, seed=9)
    for index, char in enumerate(text, start=1):
        before = watch.checks
        watch.feed(char)
        if watch.checks != before:
            check_points.append(index)

    assert check_points[:4] == [1_000, 2_000, 4_000, 8_000]
    assert check_points[4:] == [12_000, 16_000, 20_000]


def test_watch_stops_inspecting_after_a_trip() -> None:
    watch, trip = _stream("x" * 40_000)
    assert trip is not None
    checks = watch.checks

    assert watch.feed("more text") is trip
    assert watch.checks == checks


# ---------------------------------------------------------------------------
# Settings and channels
# ---------------------------------------------------------------------------


def test_disabled_guard_never_trips() -> None:
    settings = RepetitionGuardSettings(enabled=False)
    watch, trip = _stream("loop " * 20_000, settings=settings)

    assert trip is None
    assert watch.checks == 0
    assert watch.buffered_chars == 0


def test_settings_from_config_section_and_defaults() -> None:
    assert repetition_guard_settings_from(None) == RepetitionGuardSettings()
    section = SimpleNamespace(
        enabled=False,
        first_check_chars=20_000,
        tail_chars=10,  # below first_check_chars: raised to it
        window_chars=80,
        min_repeats=8,
        min_coverage=0.7,
    )
    settings = repetition_guard_settings_from(section)
    assert settings == RepetitionGuardSettings(
        enabled=False,
        first_check_chars=20_000,
        tail_chars=20_000,
        window_chars=80,
        min_repeats=8,
        min_coverage=0.7,
    )
    partial = repetition_guard_settings_from(SimpleNamespace(min_repeats=9))
    assert partial.min_repeats == 9
    assert partial.first_check_chars == RepetitionGuardSettings().first_check_chars


def test_stream_guard_watches_reasoning_separately() -> None:
    guard = StreamRepetitionGuard()
    guard.feed_text(_prose(10_000))
    trip = None
    for _ in range(4_000):
        trip = guard.feed_reasoning("Wait, let me re-check that. ") or trip
        if trip is not None:
            break

    assert trip is not None
    assert trip.channel == "reasoning"
    assert guard.trip is trip
    assert guard.text.trip is None


def test_find_repetition_on_short_or_empty_text() -> None:
    assert find_repetition("") is None
    assert find_repetition("short") is None


def test_kept_text_for_an_inexact_loop_uses_the_recorded_cut() -> None:
    trip = RepetitionTrip(
        signal="repeated_lines",
        period=10,
        repeats=6,
        coverage_chars=60,
        loop_start=4,
        keep_chars=9,
        checked_chars=70,
        exact=False,
    )
    assert kept_text("abcd12345 and the rest", trip) == "abcd12345"
