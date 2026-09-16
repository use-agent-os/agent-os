from __future__ import annotations

import importlib.util
import json
from pathlib import Path

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
    spec = importlib.util.spec_from_file_location("weather_fetch", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_weather_entrypoint_extracts_destination_and_compacts_forecast(
    monkeypatch,
    capsys,
) -> None:
    module = _load_module()

    def fake_fetch(location: str, timeout: float):
        assert location == "Tokyo, Japan"
        assert timeout == 8.0
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
                    "date": "2026-06-25",
                    "mintempC": "22",
                    "maxtempC": "28",
                    "hourly": [
                        {"chanceofrain": "40"},
                        {"chanceofrain": "80"},
                    ],
                },
            ],
        }

    monkeypatch.setattr(module, "_fetch_wttr_json", fake_fetch)

    status = module.main(
        [
            "--location",
            "DESTINATION: Tokyo, Japan\nDATES: late June",
            "--days",
            "3",
        ],
    )

    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["location"] == "Tokyo, Japan"
    assert payload["current"]["condition"] == "Light rain"
    assert payload["forecast"][0]["rain_chance_max_pct"] == "80"
    assert "rainy season" in payload["seasonal_hint"]
    assert payload["errors"] == []


def test_weather_entrypoint_returns_seasonal_hint_on_network_error(
    monkeypatch,
    capsys,
) -> None:
    module = _load_module()

    def fake_fetch(_location: str, _timeout: float):
        raise TimeoutError("slow")

    monkeypatch.setattr(module, "_fetch_wttr_json", fake_fetch)

    status = module.main(["--location", "DESTINATION: Tokyo\nDATES: late June"])

    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["location"] == "Tokyo"
    assert payload["forecast"] == []
    assert payload["errors"]
    assert "rainy season" in payload["seasonal_hint"]


def test_weather_seasonal_hint_does_not_match_june_in_place_names(
    monkeypatch,
    capsys,
) -> None:
    module = _load_module()

    def fake_fetch(location: str, timeout: float):
        return {
            "current_condition": [
                {
                    "weatherDesc": [{"value": "Sunny"}],
                    "temp_C": "18",
                    "FeelsLikeC": "18",
                    "humidity": "50",
                    "precipMM": "0.0",
                    "windspeedKmph": "8",
                },
            ],
            "weather": [],
        }

    monkeypatch.setattr(module, "_fetch_wttr_json", fake_fetch)

    # Place name containing 'june' like Juneau, Alaska should NOT trigger
    # the month-based out-of-window warning
    status = module.main(["--location", "DESTINATION: Juneau, Alaska\nDATES: tomorrow"])
    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["location"] == "Juneau, Alaska"
    assert "Requested dates appear outside" not in payload["seasonal_hint"]
    assert "Short-range forecast only" in payload["seasonal_hint"]


def test_weather_seasonal_hint_matches_word_boundary_june(
    monkeypatch,
    capsys,
) -> None:
    module = _load_module()

    def fake_fetch(location: str, timeout: float):
        return {
            "current_condition": [
                {
                    "weatherDesc": [{"value": "Sunny"}],
                    "temp_C": "20",
                    "FeelsLikeC": "20",
                    "humidity": "50",
                    "precipMM": "0.0",
                    "windspeedKmph": "10",
                },
            ],
            "weather": [],
        }

    monkeypatch.setattr(module, "_fetch_wttr_json", fake_fetch)

    # Explicit month June should trigger the seasonal date advisory
    status = module.main(["--location", "DESTINATION: Seattle\nDATES: 15 June"])
    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["location"] == "Seattle"
    hint = payload["seasonal_hint"]
    assert "Requested dates appear outside the reliable short forecast window" in hint
