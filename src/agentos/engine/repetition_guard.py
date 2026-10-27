"""Spot a streamed model response that has degenerated into repeating itself.

A model occasionally gets stuck emitting the same line, paragraph or short
phrase over and over until it hits ``max_tokens``. This module is the pure
detection half of the guard: it holds no I/O, imports nothing from the rest of
the engine, and is fed the text deltas of one assistant message (one watch for
the visible text, a separate one for the reasoning text).

Cost is bounded on purpose. Nothing is checked until the text passes
``first_check_chars``; after that a check runs each time the text has grown by
as much again as it had (capped at ``tail_chars``), so a response of ``n``
characters is checked roughly ``log2(tail / first) + n / tail`` times. Each
check looks only at the last ``tail_chars`` characters, and the watch never
buffers more than about twice that.

A check trips on two signals, both required to cover at least
``min_coverage`` of the inspected tail:

* an exactly periodic run: a ``window_chars`` anchor sampled from the tail
  reappears ``p`` characters later and the text keeps matching itself at
  offset ``p`` for at least ``min_repeats`` periods. A unit that is one line
  is reported as ``repeated_lines``, a unit shorter than the window with no
  line break as ``short_period``, anything longer (a paragraph, a block) as
  ``repeated_window``;
* one stripped line that repeats at least ``min_repeats`` times, not always
  back to back, in a tail whose lines are mostly not distinct
  (``repeated_lines``).

The defaults lean towards missing a loop rather than cutting a legitimate long
answer: generated code with shared boilerplate, a long table or a numbered list
differ from row to row, so they are never exactly periodic over half the tail
and never dominated by one line.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

RepetitionSignal = Literal["short_period", "repeated_window", "repeated_lines"]
RepetitionChannel = Literal["text", "reasoning"]

# Anchors sampled per check for the periodic-run search, and how many later
# occurrences of each anchor are tried as a candidate period.
_ANCHOR_SAMPLES = 32
_MATCHES_PER_ANCHOR = 4
_LINE_RE = re.compile(r"[^\r\n]+")


@dataclass(frozen=True)
class RepetitionGuardSettings:
    """Thresholds for the repetition guard (``[llm.repetition_guard]``)."""

    enabled: bool = True
    first_check_chars: int = 16_000
    tail_chars: int = 64_000
    window_chars: int = 60
    min_repeats: int = 5
    min_coverage: float = 0.5

    def normalized(self) -> RepetitionGuardSettings:
        """Return a copy with every threshold clamped to a workable value."""

        window = max(8, int(self.window_chars))
        first = max(window * 4, int(self.first_check_chars))
        return RepetitionGuardSettings(
            enabled=bool(self.enabled),
            first_check_chars=first,
            tail_chars=max(first, int(self.tail_chars)),
            window_chars=window,
            min_repeats=max(2, int(self.min_repeats)),
            min_coverage=min(1.0, max(0.05, float(self.min_coverage))),
        )


def repetition_guard_settings_from(source: Any) -> RepetitionGuardSettings:
    """Build settings from a config section (any object with matching attributes).

    ``None`` or a missing attribute falls back to the default for that field.
    """

    defaults = RepetitionGuardSettings()
    if source is None:
        return defaults
    values = {
        name: getattr(source, name, getattr(defaults, name))
        for name in RepetitionGuardSettings.__dataclass_fields__
    }
    return RepetitionGuardSettings(**values).normalized()


@dataclass(frozen=True)
class RepetitionTrip:
    """Where and how a watched text was found to be looping.

    Offsets are absolute positions in the text fed to the watch. The repeated
    unit first appears at ``loop_start``; ``keep_chars`` is the end of that
    first copy, so ``text[:keep_chars]`` is the text up to the repetition.
    ``exact`` is true when the text repeats itself character for character
    with period ``period``; false for a dominant line with variations between
    its copies.
    """

    signal: RepetitionSignal
    period: int
    repeats: int
    coverage_chars: int
    loop_start: int
    keep_chars: int
    checked_chars: int
    channel: RepetitionChannel = "text"
    exact: bool = True


@dataclass(frozen=True)
class _Finding:
    signal: RepetitionSignal
    period: int
    repeats: int
    coverage_chars: int
    loop_start: int
    keep_chars: int
    exact: bool = True


def find_repetition(
    text: str,
    settings: RepetitionGuardSettings | None = None,
) -> _Finding | None:
    """Analyse one window of text; return the strongest loop found, if any.

    Offsets in the result are relative to ``text``. Callers normally use
    :class:`RepetitionWatch`, which picks the window and the schedule.
    """

    cfg = (settings or RepetitionGuardSettings()).normalized()
    if not text:
        return None
    needed = max(1, int(len(text) * cfg.min_coverage))
    periodic = _find_periodic_run(text, cfg, needed)
    if periodic is not None:
        return periodic
    return _find_repeated_line(text, cfg, needed)


def _find_periodic_run(
    text: str,
    cfg: RepetitionGuardSettings,
    needed: int,
) -> _Finding | None:
    window = cfg.window_chars
    length = len(text)
    if length < window * 2 or needed < window:
        return None
    last_start = length - window
    step = max(1, last_start // (_ANCHOR_SAMPLES - 1))
    best: _Finding | None = None
    seen_runs: list[tuple[int, int, int]] = []
    for start in range(0, last_start + 1, step):
        if any(lo <= start and start + window <= hi for lo, hi, _ in seen_runs):
            # Already inside a run measured from an earlier anchor.
            continue
        anchor = text[start : start + window]
        match = start
        for _ in range(_MATCHES_PER_ANCHOR):
            match = text.find(anchor, match + 1)
            if match < 0:
                break
            period = match - start
            left = _extend_left(text, start, period)
            right = _extend_right(text, start + window, period)
            covered_end = right + period
            coverage = covered_end - left
            seen_runs.append((left, covered_end, period))
            repeats = coverage // period
            if repeats < cfg.min_repeats or coverage < needed:
                continue
            finding = _Finding(
                signal=_periodic_signal(text[left : left + period], window),
                period=period,
                repeats=repeats,
                coverage_chars=coverage,
                loop_start=left,
                keep_chars=left + period,
            )
            if best is None or finding.coverage_chars > best.coverage_chars:
                best = finding
            break
    return best


def _periodic_signal(unit: str, window: int) -> RepetitionSignal:
    if unit.count("\n") == 1 and unit.strip():
        return "repeated_lines"
    if len(unit) < window:
        return "short_period"
    return "repeated_window"


def _extend_right(text: str, end: int, period: int) -> int:
    """Largest ``r >= end`` with ``text[k] == text[k + period]`` for ``end <= k < r``."""

    limit = len(text) - period
    step = 64
    growing = True
    while step >= 1:
        nxt = min(end + step, limit)
        if nxt > end and text[end:nxt] == text[end + period : nxt + period]:
            end = nxt
            if growing:
                step *= 2
        else:
            growing = False
            step //= 2
    return end


def _extend_left(text: str, start: int, period: int) -> int:
    """Smallest ``l <= start`` with ``text[k] == text[k + period]`` for ``l <= k < start``."""

    step = 64
    growing = True
    while step >= 1:
        nxt = max(start - step, 0)
        if nxt < start and text[nxt:start] == text[nxt + period : start + period]:
            start = nxt
            if growing:
                step *= 2
        else:
            growing = False
            step //= 2
    return start


def _find_repeated_line(
    text: str,
    cfg: RepetitionGuardSettings,
    needed: int,
) -> _Finding | None:
    spans: list[tuple[str, int, int]] = []
    for match in _LINE_RE.finditer(text):
        line = match.group(0).strip()
        if line:
            spans.append((line, match.start(), match.end()))
    total = len(spans)
    if total < cfg.min_repeats:
        return None
    counts = Counter(line for line, _, _ in spans)
    # Mostly distinct lines (table rows, list items, code) are not a loop even
    # when a few of them repeat.
    if len(counts) * 2 > total:
        return None
    candidates = [item for item in counts.items() if item[1] >= cfg.min_repeats]
    if not candidates:
        return None
    line, count = max(candidates, key=lambda item: len(item[0]) * item[1])
    coverage = len(line) * count
    if coverage < needed:
        return None
    positions = [(start, end) for value, start, end in spans if value == line]
    first_start, first_end = _dense_cluster_head(positions)
    gaps = [b[0] - a[0] for a, b in zip(positions, positions[1:], strict=False)]
    period = sorted(gaps)[len(gaps) // 2] if gaps else len(line)
    return _Finding(
        signal="repeated_lines",
        period=max(1, period),
        repeats=count,
        coverage_chars=coverage,
        loop_start=first_start,
        keep_chars=first_end,
        exact=False,
    )


def _dense_cluster_head(positions: list[tuple[int, int]]) -> tuple[int, int]:
    """Return the occurrence that opens the final tight run of a repeated line.

    An early, legitimate use of the line followed by unrelated text should not
    pull the cut point back, so walk back from the last occurrence while the
    gaps stay close to the typical gap.
    """

    if len(positions) < 2:
        return positions[0]
    gaps = [b[0] - a[0] for a, b in zip(positions, positions[1:], strict=False)]
    typical = sorted(gaps)[len(gaps) // 2]
    limit = max(typical * 4, 1)
    index = len(positions) - 1
    while index > 0 and positions[index][0] - positions[index - 1][0] <= limit:
        index -= 1
    return positions[index]


class RepetitionWatch:
    """Incremental watch over the text of one assistant message channel."""

    def __init__(
        self,
        settings: RepetitionGuardSettings | None = None,
        *,
        channel: RepetitionChannel = "text",
    ) -> None:
        self._settings = (settings or RepetitionGuardSettings()).normalized()
        self._channel: RepetitionChannel = channel
        self._parts: list[str] = []
        self._buffered = 0
        self._total = 0
        self._next_check = self._settings.first_check_chars
        self.checks = 0
        self.trip: RepetitionTrip | None = None

    @property
    def total_chars(self) -> int:
        return self._total

    @property
    def buffered_chars(self) -> int:
        return self._buffered

    def feed(self, delta: str) -> RepetitionTrip | None:
        """Add a delta; return the trip the first time the text is found looping."""

        if self.trip is not None:
            return self.trip
        if not delta or not self._settings.enabled:
            self._total += len(delta)
            return None
        self._parts.append(delta)
        self._buffered += len(delta)
        self._total += len(delta)
        if self._total < self._next_check:
            return None
        return self._check()

    def _check(self) -> RepetitionTrip | None:
        cfg = self._settings
        tail = "".join(self._parts)
        if len(tail) > cfg.tail_chars:
            tail = tail[-cfg.tail_chars :]
        self._parts = [tail]
        self._buffered = len(tail)
        self._next_check = self._total + min(self._total, cfg.tail_chars)
        self.checks += 1
        finding = find_repetition(tail, cfg)
        if finding is None:
            return None
        base = self._total - len(tail)
        self.trip = RepetitionTrip(
            signal=finding.signal,
            period=finding.period,
            repeats=finding.repeats,
            coverage_chars=finding.coverage_chars,
            loop_start=base + finding.loop_start,
            keep_chars=base + finding.keep_chars,
            checked_chars=self._total,
            channel=self._channel,
            exact=finding.exact,
        )
        return self.trip


class StreamRepetitionGuard:
    """Text and reasoning watches for one provider call."""

    def __init__(self, settings: RepetitionGuardSettings | None = None) -> None:
        self.settings = (settings or RepetitionGuardSettings()).normalized()
        self.text = RepetitionWatch(self.settings, channel="text")
        self.reasoning = RepetitionWatch(self.settings, channel="reasoning")

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    @property
    def trip(self) -> RepetitionTrip | None:
        return self.text.trip or self.reasoning.trip

    def feed_text(self, delta: str) -> RepetitionTrip | None:
        return self.text.feed(delta)

    def feed_reasoning(self, delta: str) -> RepetitionTrip | None:
        return self.reasoning.feed(delta)


def kept_text(text: str, trip: RepetitionTrip) -> str:
    """Cut ``text`` (the full watched text) back to its first repeated copy.

    The watch only sees its tail, so a loop that began before the tail is
    traced back here over the full text before cutting.
    """

    keep = min(len(text), max(0, trip.keep_chars))
    if trip.exact and trip.loop_start > 0 and trip.period > 0:
        start = min(trip.loop_start, len(text) - trip.period)
        if start > 0:
            start = _extend_left(text, start, trip.period)
            keep = min(keep, start + trip.period)
    return text[:keep]
