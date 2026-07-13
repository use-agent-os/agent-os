#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

import yaml

from agentos.cli.gateway_client import GatewayClient


def _load_cases(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise SystemExit(f"Golden prompt file must contain a top-level cases list: {path}")
    return list(data["cases"])


def _tool_names(events: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for event in events:
        for key in ("tool_name", "name"):
            value = event.get(key)
            if isinstance(value, str) and value:
                names.add(value)
        payload = event.get("payload")
        if isinstance(payload, dict):
            value = payload.get("tool_name") or payload.get("name")
            if isinstance(value, str) and value:
                names.add(value)
    return names


def _assistant_text(events: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for event in events:
        for key in ("text", "delta", "message"):
            value = event.get(key)
            if isinstance(value, str):
                chunks.append(value)
    return "\n".join(chunks)


def _case_result(case: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    text = _assistant_text(events)
    tools = _tool_names(events)
    failures: list[str] = []
    for token in case.get("must_contain", []):
        if token not in text:
            failures.append(f"missing text: {token}")
    any_tokens = case.get("must_contain_any", [])
    if any_tokens and not any(token in text for token in any_tokens):
        failures.append(f"missing any text: {any_tokens}")
    for token in case.get("must_not_contain", []):
        if token in text:
            failures.append(f"forbidden text: {token}")
    for name in case.get("expected_tools", []):
        if name not in tools:
            failures.append(f"missing tool: {name}")
    for name in case.get("forbidden_tools", []):
        if name in tools:
            failures.append(f"forbidden tool: {name}")
    return {
        "id": case.get("id"),
        "ok": not failures,
        "failures": failures,
        "tools": sorted(tools),
        "text_excerpt": text[:1000],
    }


async def _run_case(client: GatewayClient, case: dict[str, Any]) -> dict[str, Any]:
    session_key = await client.create_session(display_name=f"golden:{case['id']}")
    events = [
        event
        async for event in client.send_message(session_key, str(case["prompt"]))
    ]
    return _case_result(case, events)


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Run public release golden prompts.")
    parser.add_argument(
        "--golden",
        type=Path,
        default=Path("tests/golden/public_release_open.yaml"),
    )
    parser.add_argument("--gateway", default="ws://127.0.0.1:18791/ws")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("tests/functional/reports/public-release-golden.json"),
    )
    args = parser.parse_args()

    cases = _load_cases(args.golden)
    client = GatewayClient()
    await client.connect(args.gateway)
    try:
        results = [await _run_case(client, case) for case in cases]
    finally:
        await client.close()

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "gateway": args.gateway,
        "results": results,
        "ok": all(result["ok"] for result in results),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
