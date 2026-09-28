#!/usr/bin/env python3
"""Fetch a compact weather summary for meta-skill DAGs."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Bundled scripts run under AgentOS's own interpreter; the path insert only
# matters in a source checkout where the package is not installed (#2804).
_SRC_ROOT = str(Path(__file__).resolve().parents[5])
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)
from agentos.skill_stdio import configure_utf8_stdio  # noqa: E402


def _extract_location(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return "London"
    for line in text.splitlines():
        match = re.match(r"\s*DESTINATION:\s*(.*?)\s*$", line, flags=re.I)
        if match:
            # A DESTINATION field is present, so it -- not some other field
            # in the contract -- is the caller's answer for "where"; a blank
            # value means no destination was resolved, same as no text at
            # all, not "fall through to whatever line comes first".
            return match.group(1).strip() or "London"
    first = text.splitlines()[0].strip()
    return first[:120] or "London"


def _seasonal_hint(query: str, location: str) -> str:
    lowered = f"{query} {location}".lower()
    has_june = bool(re.search(r"\bjune\b", lowered))
    has_tokyo = bool(re.search(r"\btokyo\b", lowered))
    if has_tokyo and has_june:
        return (
            "Tokyo in late June is usually tsuyu rainy season: humid, warm, "
            "frequent showers, and occasional heavy rain. Treat outdoor plans "
            "as weather-dependent and keep indoor backups."
        )
    if has_june:
        return (
            "Requested dates appear outside the reliable short forecast window; "
            "use current forecast only as near-term context and verify seasonal "
            "normals before booking."
        )
    return (
        "Short-range forecast only; verify dates again near departure for "
        "weather-sensitive bookings."
    )


def _fetch_wttr_json(location: str, timeout: float) -> dict[str, Any]:
    # safe="" rather than the default "/": the location is one path segment,
    # and the entrypoint defaults it to the user's own message, so an ordinary
    # "Dallas/Fort Worth" or a "15/09" date would otherwise split the path and
    # address somewhere else entirely.
    encoded = urllib.parse.quote(location, safe="")
    url = f"https://wttr.in/{encoded}?format=j1"
    req = urllib.request.Request(  # noqa: S310 - fixed trusted weather endpoint
        url,
        headers={"User-Agent": "AgentOS-weather-skill/0.1"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _pick_current(payload: dict[str, Any]) -> dict[str, str]:
    current = (payload.get("current_condition") or [{}])[0]
    desc = (current.get("weatherDesc") or [{}])[0].get("value", "")
    return {
        "condition": desc,
        "temperature_c": str(current.get("temp_C", "")),
        "feels_like_c": str(current.get("FeelsLikeC", "")),
        "humidity_pct": str(current.get("humidity", "")),
        "precip_mm": str(current.get("precipMM", "")),
        "wind_kmph": str(current.get("windspeedKmph", "")),
    }


def _pick_forecast(payload: dict[str, Any], days: int) -> list[dict[str, str]]:
    forecast: list[dict[str, str]] = []
    for item in (payload.get("weather") or [])[:days]:
        hourly = item.get("hourly") or []
        rain_chances = [
            int(h.get("chanceofrain", 0))
            for h in hourly
            if str(h.get("chanceofrain", "")).isdigit()
        ]
        forecast.append(
            {
                "date": str(item.get("date", "")),
                "min_c": str(item.get("mintempC", "")),
                "max_c": str(item.get("maxtempC", "")),
                "rain_chance_max_pct": str(max(rain_chances) if rain_chances else ""),
                "rain_hours_over_50pct": str(sum(1 for value in rain_chances if value >= 50)),
            }
        )
    return forecast


def _dump(payload: Any) -> str:
    """The exact bytes ``main`` prints, so a size check is on what is emitted.

    ``separators=(",", ":")`` matches :func:`_summarize`'s measurement. The old
    pair disagreed -- the budget was measured on the compact form and the
    output printed with the default ``", "`` / ``": "`` -- so even a result that
    fit the measured budget came out over it on stdout.
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _summarize(result: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Fit ``result`` into ``max_chars`` of JSON, or say plainly that it cannot.

    ``--max-chars`` is the caller's budget: ``SKILL.md`` sells this entrypoint as
    "a bounded JSON forecast" and ``{{ with.max_chars }}`` is how a meta-skill
    sets the bound per invocation. The old body measured the JSON and applied
    one fixed cut (``forecast[:2]``) without ever re-measuring, so 660 chars came
    back against a budget of 50 -- flagged ``truncated: true``, as if the budget
    had been kept.

    ``truncated`` now means "something was actually dropped", never "a trim was
    attempted", and the output is at or under ``max_chars`` whenever anything can
    fit at all. Parts are dropped least load-bearing first: forecast days (a
    caller can just ask for fewer), then accumulated errors, then the prose hint,
    then the optional current readings, then the meta fields. ``location`` -- the
    one thing the caller asked about -- is the last to go.
    """

    def _size(payload: Any) -> int:
        return len(_dump(payload))

    if _size(result) <= max_chars:
        return result

    candidate = dict(result)
    candidate["truncated"] = True
    candidate["forecast"] = list(result.get("forecast") or [])
    candidate["errors"] = list(result.get("errors") or [])
    candidate["current"] = dict(result.get("current") or {})
    candidate["seasonal_hint"] = result.get("seasonal_hint", "")

    def _fits() -> bool:
        return _size(candidate) <= max_chars

    # Least load-bearing first: failed fetches nobody asked for, then the prose
    # hint (a caller can ask the model to reason about the season itself), then
    # forecast days (drop one day at a time, newest kept), then the optional
    # current readings. ``location`` -- the one thing the caller asked about --
    # is the last to go.
    while candidate["errors"] and not _fits():
        candidate["errors"].pop()
    if candidate["seasonal_hint"] and not _fits():
        candidate["seasonal_hint"] = ""
    while candidate["forecast"] and not _fits():
        candidate["forecast"].pop()
    for key in (
        "precip_mm",
        "wind_kmph",
        "humidity_pct",
        "feels_like_c",
        "condition",
        "temperature_c",
    ):
        if _fits():
            break
        candidate["current"].pop(key, None)

    # Drop what has been emptied outright: a ``"forecast": []`` left behind is
    # not data, it is bytes the budget could spend on ``location``. This is what
    # let the old shape sit at 95 chars against a budget of 50.
    for key in ("forecast", "errors", "current"):
        if not candidate.get(key):
            candidate.pop(key, None)
    if not candidate.get("seasonal_hint"):
        candidate.pop("seasonal_hint", None)
    for key in ("forecast_window", "source"):
        if _fits():
            break
        candidate.pop(key, None)
    if _fits():
        return candidate

    # Even the barest shape is over budget (``--max-chars`` in the tens). Build
    # the smallest honest marker additively so the output honours the cap rather
    # than coming back many times over with a flag claiming otherwise. ``truncated``
    # is the contract and is added first; ``location`` -- the one thing the caller
    # asked about -- is kept whole if it fits and shortened only if it must be;
    # ``dropped`` is a bonus marker and the first to be forfeited.
    fallback: dict[str, Any] = {}
    if _size({"truncated": True}) <= max_chars:
        fallback["truncated"] = True
    location = str(result.get("location", ""))
    shortened = location
    while shortened and _size({**fallback, "location": shortened}) > max_chars:
        shortened = shortened[:-1]
    if _size({**fallback, "location": shortened}) <= max_chars:
        fallback["location"] = shortened
    if _size({**fallback, "dropped": True}) <= max_chars:
        fallback["dropped"] = True
    return fallback


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--location", required=True)
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--max-chars", type=int, default=2500)
    args = parser.parse_args(argv)

    raw_location = args.location
    location = _extract_location(raw_location)
    days = max(1, min(args.days, 5))
    result: dict[str, Any] = {
        "location": location,
        "source": "wttr.in",
        "forecast_window": "short_range_current_service",
        "seasonal_hint": _seasonal_hint(raw_location, location),
        "current": {},
        "forecast": [],
        "errors": [],
    }
    try:
        payload = _fetch_wttr_json(location, args.timeout)
        result["current"] = _pick_current(payload)
        result["forecast"] = _pick_forecast(payload, days)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 - keep meta DAG resilient
        result["errors"].append(f"{type(exc).__name__}: {exc}")

    sys.stdout.write(_dump(_summarize(result, args.max_chars)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
