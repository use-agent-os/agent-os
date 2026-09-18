"""The inline-chart converter shipped with the GMGN skills.

Both `gmgn-market` and `gmgn-token` publish chart artifacts on their own, so
each carries its own copy of the converter — `{baseDir}` only ever resolves to
the skill that is actually loaded. These tests pin the copies together and cover
the two conversions that are easy to get wrong by hand: GMGN's millisecond
timestamps and its USD-vs-token-units volume fields.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

BUNDLED = Path(__file__).resolve().parents[1] / "src" / "agentos" / "skills" / "bundled"
MARKET_SCRIPT = BUNDLED / "gmgn-market" / "scripts" / "kline_chart.py"
TOKEN_SCRIPT = BUNDLED / "gmgn-token" / "scripts" / "kline_chart.py"
CHART_MIME = "application/vnd.agentos.chart+json"


def load_converter() -> Any:
    spec = importlib.util.spec_from_file_location("kline_chart", MARKET_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_both_skills_ship_the_same_converter_byte_for_byte() -> None:
    # Two copies exist so either skill works alone; they must not drift apart.
    assert MARKET_SCRIPT.read_bytes() == TOKEN_SCRIPT.read_bytes()


@pytest.mark.parametrize("skill", ["gmgn-market", "gmgn-token"])
def test_each_skill_can_publish_a_chart_without_the_other(skill: str) -> None:
    body = (BUNDLED / skill / "SKILL.md").read_text(encoding="utf-8")

    # Its own script, its own mime — no hop through a sibling skill that may not
    # be enabled.
    assert "{baseDir}/scripts/kline_chart.py" in body
    assert CHART_MIME in body
    assert "--resolution" in body


def test_gmgn_token_no_longer_defers_the_chart_to_gmgn_market() -> None:
    body = (BUNDLED / "gmgn-token" / "SKILL.md").read_text(encoding="utf-8")
    chart_section = body.split("## Price Chart During Research", maxsplit=1)[1]
    chart_section = chart_section.split("\n## ", maxsplit=1)[0]

    assert "gmgn-market" not in chart_section


def test_candles_are_found_under_every_shape_the_cli_has_shipped() -> None:
    module = load_converter()
    row = {"time": 1, "open": "1", "high": "2", "low": "0.5", "close": "1.5"}

    assert module._find_candles([row]) == [row]
    assert module._find_candles({"list": [row]}) == [row]
    assert module._find_candles({"data": {"list": [row]}}) == [row]
    assert module._find_candles({"nope": 1}) == []


def test_millisecond_timestamps_become_seconds() -> None:
    module = load_converter()
    rows = [{"time": 1735689600000, "open": "1", "high": "2", "low": "1", "close": "2"}]

    assert module.convert_candles(rows)[0]["time"] == 1735689600


def test_second_precision_timestamps_are_left_alone() -> None:
    module = load_converter()
    rows = [{"time": 1735689600, "open": "1", "high": "2", "low": "1", "close": "2"}]

    assert module.convert_candles(rows)[0]["time"] == 1735689600


def test_rows_are_sorted_oldest_first_and_deduplicated_by_timestamp() -> None:
    module = load_converter()
    rows = [
        {"time": 200, "open": "2", "high": "2", "low": "2", "close": "2"},
        {"time": 100, "open": "1", "high": "1", "low": "1", "close": "1"},
        # A restatement of the 200 candle: the later row wins.
        {"time": 200, "open": "3", "high": "3", "low": "3", "close": "3"},
    ]

    candles = module.convert_candles(rows)

    assert [candle["time"] for candle in candles] == [100, 200]
    assert candles[1]["close"] == 3.0


def test_a_row_missing_any_ohlc_field_is_dropped_rather_than_zero_filled() -> None:
    module = load_converter()
    rows = [
        {"time": 100, "open": "1", "high": "2", "low": "1"},
        {"time": 200, "open": "1", "high": "2", "low": "1", "close": "2"},
    ]

    assert [candle["time"] for candle in module.convert_candles(rows)] == [200]


def test_usd_volume_is_carried_and_a_negative_one_dropped() -> None:
    module = load_converter()
    base = {"open": "1", "high": "2", "low": "1", "close": "2"}
    rows = [
        # `volume` is USD; `amount` is token units and must not be used.
        {"time": 100, **base, "volume": "1214", "amount": "5379110"},
        {"time": 200, **base, "volume": "-5"},
    ]

    candles = module.convert_candles(rows)

    assert candles[0]["volume"] == 1214.0
    assert "volume" not in candles[1]


def test_the_payload_carries_the_mime_the_web_chat_matches_on() -> None:
    module = load_converter()

    assert module.CHART_MIME == CHART_MIME


def test_the_payload_titles_itself_from_the_symbol_and_resolution() -> None:
    module = load_converter()
    candles = [{"time": 100, "open": 1.0, "high": 2.0, "low": 1.0, "close": 2.0}]

    payload = module.build_payload(candles, symbol="BONK", chain="sol", resolution="1h")

    assert payload["type"] == "candlestick"
    assert payload["title"] == "BONK · 1h"
    assert payload["subtitle"] == "SOL · 1h"
    assert payload["candles"] == candles


@pytest.mark.parametrize("script_path", [MARKET_SCRIPT, TOKEN_SCRIPT])
def test_kline_chart_stdin_utf8_decoding_and_stdout(
    script_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import json

    spec = importlib.util.spec_from_file_location(script_path.parent.parent.name, script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    out = tmp_path / "chart.json"
    rows = [
        {
            "time": 1735689600000,
            "open": "1.5",
            "high": "2.0",
            "low": "1.0",
            "close": "1.8",
            "volume": "500",
        }
    ]
    payload = {"data": {"list": rows}}
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    class FakeStdin:
        def __init__(self, raw: bytes):
            self.buffer = io.BytesIO(raw)

        def read(self):
            return self.buffer.read().decode("utf-8")

    monkeypatch.setattr(sys, "stdin", FakeStdin(encoded))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "kline_chart.py",
            "--symbol",
            "cổ phiếu 日本語",
            "--chain",
            "sol",
            "--resolution",
            "1h",
            "--output",
            str(out),
        ],
    )
    assert mod.main() == 0
    assert out.is_file()
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert "cổ phiếu 日本語" in saved["title"]
    assert len(saved["candles"]) == 1
    assert saved["candles"][0]["time"] == 1735689600


@pytest.mark.parametrize("script_path", [MARKET_SCRIPT, TOKEN_SCRIPT])
def test_kline_chart_file_input_utf8(
    script_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    spec = importlib.util.spec_from_file_location(script_path.parent.parent.name, script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    in_file = tmp_path / "input.json"
    out_file = tmp_path / "output.chart.json"
    rows = [{"time": 100000, "open": "10", "high": "20", "low": "5", "close": "15"}]
    in_file.write_text(json.dumps({"list": rows}), encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "kline_chart.py",
            "--input",
            str(in_file),
            "--symbol",
            "TEST",
            "--output",
            str(out_file),
        ],
    )
    assert mod.main() == 0
    assert out_file.is_file()
    saved = json.loads(out_file.read_text(encoding="utf-8"))
    assert len(saved["candles"]) == 1
