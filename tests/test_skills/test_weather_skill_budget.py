"""``weather_fetch._summarize`` must honour ``--max-chars``, or say that it cannot.

``--max-chars`` is the caller's output budget: ``SKILL.md`` sells the entrypoint
as "a bounded JSON forecast" for meta-skill DAGs (``skill_exec`` into a sub-agent
context), and ``{{ with.max_chars | default(2500) }}`` is how a caller tunes it
per invocation. The old body measured the JSON and applied exactly one fixed cut
(``forecast[:2]``) without ever re-measuring, so the output came back **13x over
budget** (660 chars against a budget of 50) carrying ``"truncated": true`` —
which reported that a trim was *attempted*, not that the budget was kept.

A second, smaller fault sat between the two halves of the contract: the budget
was measured on ``json.dumps(..., separators=(",", ":"))`` while ``main`` printed
with the default ``", "`` / ``": "`` separators, so even a result that fit the
measured budget came out over it on stdout. ``_dump`` is the single serialiser
both now use.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "weather"
    / "scripts"
    / "weather_fetch.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("weather_fetch_budget", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _result(n_forecast: int = 5, n_errors: int = 0) -> dict:
    """The shape ``main`` builds: a Tokyo/June planner contract, 5-day forecast."""
    return {
        "location": "Tokyo",
        "source": "wttr.in",
        "forecast_window": "short_range_current_service",
        "seasonal_hint": (
            "Tokyo in late June is usually tsuyu rainy season: humid, warm, "
            "frequent showers, and occasional heavy rain. Treat outdoor plans "
            "as weather-dependent and keep indoor backups."
        ),
        "current": {
            "condition": "Light rain",
            "temperature_c": "24",
            "feels_like_c": "26",
            "humidity_pct": "88",
            "precip_mm": "2.5",
            "wind_kmph": "12",
        },
        "forecast": [
            {
                "date": f"2026-06-{d:02d}",
                "min_c": "20",
                "max_c": "28",
                "rain_chance_max_pct": "80",
                "rain_hours_over_50pct": "6",
            }
            for d in range(1, n_forecast + 1)
        ],
        "errors": [f"URLError: attempt {i} failed" for i in range(n_errors)],
    }


def _size(module, payload: dict) -> int:
    return len(module._dump(payload))


# The repro table from #3498: every row was a breach before the fix except the
# default budget.
@pytest.mark.parametrize(
    ("max_chars", "was_over_by"),
    [(50, 660), (100, 660), (200, 660), (500, 660), (2500, 952)],
)
def test_the_output_respects_the_requested_budget(max_chars: int, was_over_by: int) -> None:
    module = _load_module()
    out = module._summarize(_result(), max_chars)
    assert _size(module, out) <= max_chars, (
        f"budget {max_chars} breached: {_size(module, out)} chars"
    )
    if was_over_by > max_chars:
        assert out.get("truncated") is True


def test_a_fitting_result_comes_back_untouched_and_unflagged() -> None:
    """Nothing fits: no ``truncated`` key at all — the contract for "complete"."""
    module = _load_module()
    result = _result()
    out = module._summarize(result, 100_000)
    assert out == result
    assert "truncated" not in out


@pytest.mark.parametrize("max_chars", [50, 100, 200, 500])
def test_truncated_true_means_something_was_actually_dropped(max_chars: int) -> None:
    module = _load_module()
    result = _result()
    out = module._summarize(result, max_chars)
    assert out.get("truncated") is True
    # The output must be strictly smaller than the untrimmed input — "truncated"
    # reports an outcome, not an attempt.
    assert _size(module, out) < _size(module, result)
    # …and shorter than what the old single-cut body produced at this budget
    # (660 chars with forecast[:2], for every budget at or under 500).
    if max_chars <= 500:
        assert _size(module, out) <= max_chars < 660


def test_a_budget_where_even_the_marker_does_not_fit_returns_nothing() -> None:
    """Below the marker's own size the only honest output is empty."""
    module = _load_module()
    out = module._summarize(_result(), 5)
    assert out == {}


def test_a_short_budget_still_answers_where() -> None:
    """``location`` is the one thing the caller asked about; it is the last to go."""
    module = _load_module()
    out = module._summarize(_result(), 60)
    assert _size(module, out) <= 60
    assert out.get("truncated") is True
    assert out.get("location", "").startswith("Tokyo")


def test_a_one_entry_forecast_at_a_tiny_budget_no_longer_breaches() -> None:
    """The old two-entry floor could not shrink far enough: 476 chars at budget 50."""
    module = _load_module()
    out = module._summarize(_result(n_forecast=1), 50)
    assert _size(module, out) <= 50


def test_twenty_accumulated_errors_fit_a_budget_of_200() -> None:
    """``errors`` was never trimmed at all: 1249 chars against a budget of 200."""
    module = _load_module()
    out = module._summarize(_result(n_forecast=3, n_errors=20), 200)
    assert _size(module, out) <= 200


def test_a_larger_budget_keeps_some_forecast_days() -> None:
    """The shrink must be progressive: at 500 chars real forecast data survives."""
    module = _load_module()
    out = module._summarize(_result(), 500)
    assert _size(module, out) <= 500
    assert out.get("forecast"), "expected some forecast days to survive at 500 chars"


def test_emptied_parts_are_dropped_outright() -> None:
    """A ``"forecast": []`` left behind is bytes, not data — the old shape sat at
    95 chars against a budget of 50 because of exactly this."""
    module = _load_module()
    out = module._summarize(_result(n_forecast=5, n_errors=20), 200)
    for key in ("forecast", "errors", "current", "seasonal_hint"):
        if key in out:
            assert out[key], f"{key} was left behind empty"


def test_dump_matches_what_main_prints() -> None:
    """The budget must be measured on the bytes that actually reach stdout.

    ``_summarize`` measured ``separators=(",", ":")`` while ``main`` printed with
    the default separators — so the printed output could exceed a budget the
    check said was met.
    """
    module = _load_module()
    out = module._summarize(_result(), 500)
    # What main() writes is _dump(...) — the same serialiser the budget used.
    assert module._dump(out) == json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    assert ", " not in module._dump(out).replace('", "', "")  # no default-separator spacing


def test_main_output_fits_the_flag(monkeypatch, capsys) -> None:
    """End to end: stdout itself honours ``--max-chars``, not just the dict."""
    module = _load_module()

    def fake_fetch(_location: str, _timeout: float):
        return {
            "current_condition": [
                {
                    "weatherDesc": [{"value": "Light rain"}],
                    "temp_C": "25",
                    "FeelsLikeC": "28",
                    "humidity": "84",
                    "precipMM": "1.2",
                    "windspeedKmph": "12",
                },
            ],
            "weather": [
                {
                    "date": f"2026-06-{d:02d}",
                    "mintempC": "22",
                    "maxtempC": "28",
                    "hourly": [{"chanceofrain": "40"}, {"chanceofrain": "80"}],
                }
                for d in range(1, 6)
            ],
        }

    monkeypatch.setattr(module, "_fetch_wttr_json", fake_fetch)
    for cap in (200, 500, 2500):
        status = module.main(
            ["--location", "DESTINATION: Tokyo, Japan\nDATES: late June", "--max-chars", str(cap)]
        )
        assert status == 0
        printed = capsys.readouterr().out
        assert len(printed) <= cap, f"stdout {len(printed)} chars exceeds --max-chars {cap}"
        json.loads(printed)  # still valid JSON
