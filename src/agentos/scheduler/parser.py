"""Standard 5-field cron expression parser."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_tz(tz: str) -> None:
    """Raise ValueError if ``tz`` is set but not a valid IANA timezone name."""
    if not tz:
        return
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown timezone: {tz!r}") from exc


class CronParseError(ValueError):
    pass


_FIELD_RANGES = {
    "minute": (0, 59),
    "hour": (0, 23),
    "day_of_month": (1, 31),
    "month": (1, 12),
    "day_of_week": (0, 7),  # 0=Sunday, 7=Sunday too (POSIX permits 0 and 7)
}

_MONTH_NAMES = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_DOW_NAMES = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}

_PRESETS: dict[str, str] = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


@dataclass(frozen=True)
class CronField:
    values: frozenset[int]
    #: Whether the field was written as a bare ``*``. Expanding ``*`` to the
    #: full value set makes it indistinguishable from an explicit ``0-6`` /
    #: ``1-31`` at match time, and the POSIX day-of-month/day-of-week OR rule
    #: turns on exactly that distinction — so the parser has to carry it.
    is_wildcard: bool = False

    def matches(self, value: int) -> bool:
        return value in self.values


@dataclass(frozen=True)
class CronExpression:
    minute: CronField
    hour: CronField
    day_of_month: CronField
    month: CronField
    day_of_week: CronField
    raw: str

    def matches(self, dt: datetime) -> bool:
        if not (
            self.minute.matches(dt.minute)
            and self.hour.matches(dt.hour)
            and self.month.matches(dt.month)
        ):
            return False

        day_of_month_ok = self.day_of_month.matches(dt.day)
        day_of_week_ok = self.day_of_week.matches((dt.weekday() + 1) % 7)  # Mon=0 → Sun=0

        # POSIX day-of-month / day-of-week rule: when both fields are
        # restricted (neither is a bare ``*``) the job fires when *either*
        # matches, which is what cron, croniter and every scheduler users will
        # compare against do. ``0 0 1,15 * 5`` means "the 1st, the 15th, or any
        # Friday" — ANDing the two would restrict it to a 1st or 15th that also
        # happens to be a Friday, which is almost never true.
        if self.day_of_month.is_wildcard or self.day_of_week.is_wildcard:
            return day_of_month_ok and day_of_week_ok
        return day_of_month_ok or day_of_week_ok


def _parse_field(token: str, field_name: str, names: dict[str, int] | None = None) -> CronField:
    lo, hi = _FIELD_RANGES[field_name]
    values: set[int] = set()

    for part in token.split(","):
        part = part.strip()
        if names:
            # POSIX: month and day-of-week names are case-insensitive ("case
            # doesn't matter"). Lowercase the token before substituting so
            # Mon-Fri / JAN / jan all resolve; digits and the -, /, * ,
            # separators are unaffected by lower().
            part = part.lower()
            sub_parts = part.replace("/", "§").replace("-", "¶")
            if field_name == "day_of_week":
                # In day-of-week ranges, Sunday as the upper bound (e.g. SAT-SUN,
                # WED-SUN, MON-SUN) represents 7 (hi) rather than 0 (lo),
                # so the range does not invert into start > 0 and reject.
                sub_parts = re.sub(r"¶sun\b", "¶7", sub_parts)
            for name, val in names.items():
                sub_parts = sub_parts.replace(name, str(val))
            part = sub_parts.replace("§", "/").replace("¶", "-")

        if "/" in part:
            range_part, step_str = part.split("/", 1)
            try:
                step = int(step_str)
            except ValueError:
                raise CronParseError(f"Invalid step '{step_str}' in field '{field_name}'")
            if step <= 0:
                raise CronParseError(f"Step must be > 0 in field '{field_name}'")

            if range_part == "*":
                start, end = lo, hi
            elif "-" in range_part:
                start_str, end_str = range_part.split("-", 1)
                start = _to_int(start_str, field_name, lo, hi)
                end = _to_int(end_str, field_name, lo, hi)
                # Same rule the plain-range branch applies. Without it a
                # reversed range silently yields an empty step sequence, so the
                # expression parses, stores, and then matches no instant at all.
                if start > end:
                    raise CronParseError(f"Range start > end in field '{field_name}'")
            else:
                start = _to_int(range_part, field_name, lo, hi)
                end = hi

            values.update(range(start, end + 1, step))

        elif "-" in part:
            start_str, end_str = part.split("-", 1)
            start = _to_int(start_str, field_name, lo, hi)
            end = _to_int(end_str, field_name, lo, hi)
            if start > end:
                raise CronParseError(f"Range start > end in field '{field_name}'")
            values.update(range(start, end + 1))

        elif part == "*":
            values.update(range(lo, hi + 1))

        else:
            values.add(_to_int(part, field_name, lo, hi))

    if field_name == "day_of_week" and 7 in values:
        # POSIX: day-of-week may use either 0 or 7 to mean Sunday. Our matcher
        # normalizes Python's Monday==0 to Sunday==0 via (weekday+1) % 7, so a
        # literal 7 must collide onto 0 or a plain "0 0 * * 7" schedule would
        # never fire (and "SAT-SUN" would be a dead range).
        values.remove(7)
        values.add(0)

    # Only an exact ``*`` is a wildcard: ``*/2`` and ``0-6`` both name a
    # specific set of days and count as restricted, matching croniter.
    return CronField(frozenset(values), is_wildcard=token.strip() == "*")


def _to_int(s: str, field_name: str, lo: int, hi: int) -> int:
    try:
        v = int(s)
    except ValueError:
        raise CronParseError(f"Invalid value '{s}' in field '{field_name}'")
    if not (lo <= v <= hi):
        raise CronParseError(f"Value {v} out of range [{lo}, {hi}] for field '{field_name}'")
    return v


def parse_iso_at(raw: str) -> datetime:
    """Parse a tz-aware ISO-8601 timestamp; raise CronParseError otherwise."""
    if not isinstance(raw, str):
        raise CronParseError(f"Expected ISO-8601 string, got {type(raw).__name__}")
    text = raw.strip()
    if not text:
        raise CronParseError("ISO-8601 timestamp must not be empty")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CronParseError(f"Invalid ISO-8601 timestamp: {raw!r}") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise CronParseError(f"ISO-8601 timestamp must include a timezone offset: {raw!r}")
    return dt


def parse_cron(expr: str) -> CronExpression:
    """Parse a standard 5-field cron expression or @preset shorthand."""
    expr = expr.strip()

    # Handle @presets
    if expr.startswith("@"):
        if expr not in _PRESETS:
            raise CronParseError(f"Unknown preset '{expr}'")
        expr = _PRESETS[expr]

    fields = expr.split()
    if len(fields) != 5:
        raise CronParseError(f"Expected 5 fields, got {len(fields)}: '{expr}'")

    minute_tok, hour_tok, dom_tok, month_tok, dow_tok = fields

    return CronExpression(
        minute=_parse_field(minute_tok, "minute"),
        hour=_parse_field(hour_tok, "hour"),
        day_of_month=_parse_field(dom_tok, "day_of_month"),
        month=_parse_field(month_tok, "month", _MONTH_NAMES),
        day_of_week=_parse_field(dow_tok, "day_of_week", _DOW_NAMES),
        raw=expr,
    )
