"""The inline-chart converter shipped with the GMGN skills.

Both `gmgn-market` and `gmgn-token` publish chart artifacts on their own, so
each carries its own copy of the converter — `{baseDir}` only ever resolves to
the skill that is actually loaded. These tests pin the copies together and cover
the two conversions that are easy to get wrong by hand: GMGN's millisecond
timestamps and its USD-vs-token-units volume fields.
"""

from __future__ import annotations

import importlib.util
import json
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


def test_main_reports_missing_input_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_converter()
    missing = tmp_path / "does_not_exist.json"
    out = tmp_path / "chart.json"
    code = module.main(["--input", str(missing), "--output", str(out)])

    assert code == 1
    err = capsys.readouterr().err
    assert "kline_chart: input file does not exist:" in err
    assert str(missing) in err
    assert not out.exists()


def test_main_reports_directory_as_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_converter()
    out = tmp_path / "chart.json"
    code = module.main(["--input", str(tmp_path), "--output", str(out)])

    assert code == 1
    err = capsys.readouterr().err
    assert "kline_chart: input path is not a file:" in err
    assert str(tmp_path) in err
    assert not out.exists()


def test_main_reports_unreadable_or_invalid_utf8_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_converter()
    bad_file = tmp_path / "bad.json"
    bad_file.write_bytes(b"\xff\xfe\x00\x00")
    out = tmp_path / "chart.json"
    code = module.main(["--input", str(bad_file), "--output", str(out)])

    assert code == 1
    err = capsys.readouterr().err
    assert "kline_chart: failed to read input file" in err
    assert not out.exists()


def test_main_reports_empty_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_converter()
    empty_file = tmp_path / "empty.json"
    empty_file.write_text("   \n\t  ", encoding="utf-8")
    out = tmp_path / "chart.json"
    code = module.main(["--input", str(empty_file), "--output", str(out)])

    assert code == 1
    err = capsys.readouterr().err
    assert "kline_chart: no input received" in err
    assert not out.exists()


def test_main_reports_invalid_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_converter()
    invalid_file = tmp_path / "invalid.json"
    invalid_file.write_text("{not: valid", encoding="utf-8")
    out = tmp_path / "chart.json"
    code = module.main(["--input", str(invalid_file), "--output", str(out)])

    assert code == 1
    err = capsys.readouterr().err
    assert "kline_chart: input is not valid JSON:" in err
    assert not out.exists()


def test_main_successful_chart_generation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_converter()
    input_file = tmp_path / "candles.json"
    input_file.write_text(
        json.dumps(
            [{"time": 1735689600, "open": "1.0", "high": "2.0", "low": "0.5", "close": "1.5"}]
        ),
        encoding="utf-8",
    )
    out = tmp_path / "nested" / "chart.json"
    code = module.main(
        [
            "--input",
            str(input_file),
            "--output",
            str(out),
            "--symbol",
            "BONK",
            "--chain",
            "sol",
            "--resolution",
            "1h",
        ]
    )

    assert code == 0
    cap = capsys.readouterr()
    assert "wrote 1 candles to" in cap.out
    assert "publish_artifact" in cap.out
    assert out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["type"] == "candlestick"
    assert data["title"] == "BONK · 1h"
    assert len(data["candles"]) == 1

