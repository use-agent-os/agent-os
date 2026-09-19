#!/usr/bin/env python3
"""Turn ``gmgn-cli market kline --raw`` output into a chat chart artifact.

The Web chat renders an artifact whose mime is
``application/vnd.agentos.chart+json`` as an inline candlestick chart instead of
a download chip. This script does the shape conversion so the model never has to
copy a candle array through its context:

    gmgn-cli market kline --chain sol --address <addr> --resolution 1h --raw \\
      | python3 {baseDir}/scripts/kline_chart.py --symbol BONK --chain sol \\
          --resolution 1h --output bonk-1h.chart.json

It then prints the written path, which is what ``publish_artifact`` takes.

Two conversions matter and are easy to get wrong by hand:

* GMGN returns ``time`` in **milliseconds**; the chart wants **seconds**.
* GMGN's ``volume`` is USD traded while ``amount`` is token units. The chart's
  volume histogram shows USD, so ``volume`` is the field to carry over.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Bundled scripts run under AgentOS's own interpreter; the path insert only
# matters in a source checkout where the package is not installed (#2804).
_SRC_ROOT = str(Path(__file__).resolve().parents[5])
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)
from agentos.skills.stdio import configure_utf8_stdio  # noqa: E402

CHART_MIME = "application/vnd.agentos.chart+json"
CANDLE_KEYS = ("open", "high", "low", "close")


def _looks_like_candles(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    first = value[0]
    return isinstance(first, dict) and all(key in first for key in CANDLE_KEYS)


def _find_candles(payload: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Locate the candle array inside a GMGN response.

    The CLI has shipped the rows bare, under ``list``, and under
    ``data.list`` across versions, so search rather than assume a path.
    """
    if depth > 6:
        return []
    if _looks_like_candles(payload):
        return list(payload)
    if isinstance(payload, dict):
        for key in ("list", "data", "klines", "candles", "result"):
            if key in payload:
                found = _find_candles(payload[key], depth + 1)
                if found:
                    return found
        for value in payload.values():
            found = _find_candles(value, depth + 1)
            if found:
                return found
    return []


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _seconds(value: Any) -> int | None:
    raw = _number(value)
    if raw is None or raw <= 0:
        return None
    # Anything past 1e11 seconds is far outside any real candle range, so it is
    # a millisecond timestamp — which is what GMGN actually returns.
    return int(raw // 1000) if raw > 1e11 else int(raw)


def convert_candles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return chart-ready candles, oldest first, one row per timestamp."""
    by_time: dict[int, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        time_s = _seconds(row.get("time"))
        values = {key: _number(row.get(key)) for key in CANDLE_KEYS}
        if time_s is None or any(v is None for v in values.values()):
            continue
        candle: dict[str, Any] = {"time": time_s}
        candle.update({key: values[key] for key in CANDLE_KEYS})
        volume = _number(row.get("volume"))
        if volume is not None and volume >= 0:
            candle["volume"] = volume
        by_time[time_s] = candle
    return [by_time[key] for key in sorted(by_time)]


def build_payload(
    candles: list[dict[str, Any]],
    *,
    symbol: str,
    chain: str,
    resolution: str,
) -> dict[str, Any]:
    title = f"{symbol} · {resolution}" if symbol and resolution else (symbol or "Price")
    subtitle_parts = [part for part in (chain.upper() if chain else "", resolution) if part]
    return {
        "type": "candlestick",
        "title": title,
        "subtitle": " · ".join(subtitle_parts),
        "candles": candles,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        default="-",
        help="Kline JSON file, or '-' to read stdin (default).",
    )
    parser.add_argument("--output", required=True, help="Where to write the chart artifact.")
    parser.add_argument("--symbol", default="", help="Token symbol, for the chart title.")
    parser.add_argument("--chain", default="", help="Chain id, for the chart subtitle.")
    parser.add_argument("--resolution", default="", help="Candle resolution, e.g. 1h.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio(stdin=True)
    args = parse_args(argv)

    raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
    if not raw.strip():
        print("kline_chart: no input received", file=sys.stderr)
        return 1
    try:
        response = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"kline_chart: input is not valid JSON: {exc}", file=sys.stderr)
        return 1

    candles = convert_candles(_find_candles(response))
    if not candles:
        print("kline_chart: no usable candles in the response", file=sys.stderr)
        return 1

    payload = build_payload(
        candles,
        symbol=args.symbol.strip(),
        chain=args.chain.strip(),
        resolution=args.resolution.strip(),
    )
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    print(f"wrote {len(candles)} candles to {output}")
    print(f"publish_artifact path={output} mime={CHART_MIME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
