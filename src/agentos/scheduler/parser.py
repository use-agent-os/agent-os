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


_NAME_TOKEN_SPLIT_RE = re.compile(r"([-/])")


def _substitute_names(part: str, names: dict[str, int], field_name: str) -> str:
    """Replace month / day-of-week names in one comma-free field token.

    POSIX: month and day-of-week names are case-insensitive ("case doesn't
    matter"), so the token is lowercased before lookup and Mon-Fri / JAN / jan
    all resolve. Substitution is per *token* rather than a whole-string
    replace, because the numeric value a name maps to depends on where it sits:
    Sunday is both 0 and 7, and only the position tells them apart.
    """

    tokens = _NAME_TOKEN_SPLIT_RE.split(part.lower())
    values = [names.get(token) for token in tokens]

    # POSIX lets Sunday be written 0 or 7, and cron/croniter read a trailing
    # SUN as 7 — "SAT-SUN" is 6-7, not the reversed 6-0 that _parse_field would
    # reject outright. Only the *upper* bound flips, and only when the range
    # does not already start at Sunday, so "SUN-WED" stays 0-3 and "SUN-SUN"
    # stays the single day it names.
    if field_name == "day_of_week" and len(tokens) >= 3 and tokens[1] == "-":
        if tokens[2] == "sun" and tokens[0] not in ("sun", "0", "7"):
            values[2] = 7

    return "".join(
        str(value) if value is not None else token
        for token, value in zip(tokens, values, strict=True)
    )


def _parse_field(token: str, field_name: str, names: dict[str, int] | None = None) -> CronField:
    lo, hi = _FIELD_RANGES[field_name]
    values: set[int] = set()

    for part in token.split(","):
        part = part.strip()
        if names:
            part = _substitute_names(part, names, field_name)

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
                if start == end:
                    # POSIX/croniter: a range whose two endpoints resolve to
                    # the same field position ("SUN-SUN", "3-3", "0-SUN",
                    # "SUN-0") spans the entire field, not just that one
                    # value -- matches croniter's own
                    # `elif low == high: whole cycle` rule exactly, and
                    # applies to every field (month "3-3" is all 12 months,
                    # hour "5-5" is all 24 hours), not just day_of_week.
                    start, end = lo, hi
            else:
                start = _to_int(range_part, field_name, lo, hi)
                end = hi
                if field_name == "day_of_week":
                    # Day-of-week's true cardinality is 7 distinct values
                    # (0-6); 7 is only ever an input alias for Sunday (0),
                    # never a real 8th slot -- matching croniter's own
                    # RANGES[DOW] = (0, 6). A bare "N/M" (no explicit second
                    # bound) must step across that true 7-value field:
                    # alias a literal 7 start to 0, and use 6 (not the
                    # alias-inclusive `hi`) as the implicit end.
                    if start == hi:
                        start = lo
                    end = hi - 1
                    if start == end:
                        # Matches croniter: when the (aliased) start lands
                        # exactly on the field's true max, "N/M" means "step
                        # across the whole field", not "just N" -- e.g.
                        # "6/2" is the entire {0, 2, 4, 6}, not just {6}.
                        start = lo

            values.update(range(start, end + 1, step))
        elif "-" in part:
            start_str, end_str = part.split("-", 1)
            start = _to_int(start_str, field_name, lo, hi)
            end = _to_int(end_str, field_name, lo, hi)
            if start > end:
                raise CronParseError(f"Range start > end in field '{field_name}'")
            if start == end:
                # See the with-step dash branch above for why: matches
                # croniter's `elif low == high: whole cycle` exactly.
                start, end = lo, hi
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
        raise CronParseError(
            f"ISO-8601 timestamp must include a timezone offset: {raw!r}"
        )
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
