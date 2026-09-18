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


def test_fetch_wttr_json_escapes_slash_in_location(monkeypatch) -> None:
    module = _load_module()
    requested_urls: list[str] = []

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"current_condition": [{}], "weather": []}'

    def fake_urlopen(req, timeout=None):
        requested_urls.append(req.full_url)
        return FakeResp()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    module._fetch_wttr_json("Dallas/Fort Worth", 5.0)

    assert len(requested_urls) == 1
    assert requested_urls[0] == "https://wttr.in/Dallas%2FFort%20Worth?format=j1"

