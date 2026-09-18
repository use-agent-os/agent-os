"""weather — building the wttr.in URL from a caller-supplied location.

The entrypoint defaults ``location`` to the user's own message, so whatever is
typed becomes a path segment. These tests pin the URL the script composes;
nothing here opens a socket -- ``urlopen`` is replaced and the request object
it is handed is what gets asserted.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

BUNDLED = Path(__file__).resolve().parents[1] / "src" / "agentos" / "skills" / "bundled"
SCRIPT = BUNDLED / "weather" / "scripts" / "weather_fetch.py"

_PAYLOAD = {
    "current_condition": [
        {
            "weatherDesc": [{"value": "Partly cloudy"}],
            "temp_C": "31",
            "FeelsLikeC": "38",
            "humidity": "70",
            "precipMM": "0.0",
            "windspeedKmph": "11",
        }
    ],
    "weather": [
        {
            "date": "2026-09-15",
            "mintempC": "24",
            "maxtempC": "33",
            "hourly": [{"chanceofrain": "80"}],
        }
    ],
}


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("weather_fetch", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


weather = _load()


class _FakeResponse:
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(_PAYLOAD).encode("utf-8")


def _capture_url(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the URL each request would have gone to, and answer it offline."""
    seen: list[str] = []

    def fake_urlopen(request: Any, timeout: float | None = None) -> _FakeResponse:
        seen.append(request.full_url)
        return _FakeResponse()

    monkeypatch.setattr(weather.urllib.request, "urlopen", fake_urlopen)
    return seen


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        pytest.param("London", "https://wttr.in/London?format=j1", id="plain"),
        pytest.param(
            "Dallas/Fort Worth",
            "https://wttr.in/Dallas%2FFort%20Worth?format=j1",
            id="slash_in_the_name",
        ),
        pytest.param(
            "cuaca 15/09 di Bandung",
            "https://wttr.in/cuaca%2015%2F09%20di%20Bandung?format=j1",
            id="date_in_a_sentence",
        ),
        pytest.param(
            "a/../../etc",
            "https://wttr.in/a%2F..%2F..%2Fetc?format=j1",
            id="relative_segments",
        ),
        pytest.param("New York", "https://wttr.in/New%20York?format=j1", id="space"),
        pytest.param("São Paulo", "https://wttr.in/S%C3%A3o%20Paulo?format=j1", id="non_ascii"),
        pytest.param("東京", "https://wttr.in/%E6%9D%B1%E4%BA%AC?format=j1", id="cjk"),
        pytest.param("Washington D.C.", "https://wttr.in/Washington%20D.C.?format=j1", id="dots"),
    ],
)
def test_the_location_is_one_escaped_path_segment(
    location: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slash in the location must not become a path separator."""
    seen = _capture_url(monkeypatch)

    weather._fetch_wttr_json(location, 5.0)

    assert seen == [expected]


def test_the_query_string_is_not_escaped_with_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard: only the segment is quoted, so format=j1 still reaches wttr.in."""
    seen = _capture_url(monkeypatch)

    weather._fetch_wttr_json("Dallas/Fort Worth", 5.0)

    assert seen[0].endswith("?format=j1")
    assert seen[0].count("?") == 1


def test_a_slash_no_longer_adds_a_path_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stated as the property rather than the spelling: one segment, always."""
    seen = _capture_url(monkeypatch)

    weather._fetch_wttr_json("Dallas/Fort Worth", 5.0)

    path = seen[0].removeprefix("https://wttr.in/").split("?", 1)[0]
    assert "/" not in path


def test_an_ordinary_location_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard: passes either way, and pins that escaping nothing else moved."""
    seen = _capture_url(monkeypatch)

    weather._fetch_wttr_json("London", 5.0)

    assert seen == ["https://wttr.in/London?format=j1"]


def test_the_url_survives_the_whole_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end: the escaped URL is what main() actually requests."""
    seen = _capture_url(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["weather_fetch.py", "--location", "Dallas/Fort Worth"])

    assert weather.main(["--location", "Dallas/Fort Worth"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert seen == ["https://wttr.in/Dallas%2FFort%20Worth?format=j1"]
    assert payload["location"] == "Dallas/Fort Worth"
    assert payload["errors"] == []
    assert payload["current"]["temperature_c"] == "31"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("DESTINATION: Dallas/Fort Worth", "Dallas/Fort Worth", id="destination_line"),
        pytest.param("Dallas/Fort Worth\nsecond line", "Dallas/Fort Worth", id="first_line"),
        pytest.param("", "London", id="empty_falls_back"),
        pytest.param("   ", "London", id="blank_falls_back"),
    ],
)
def test_location_extraction_is_untouched(raw: str, expected: str) -> None:
    """Guard: the escaping change must not move where the location comes from."""
    assert weather._extract_location(raw) == expected
