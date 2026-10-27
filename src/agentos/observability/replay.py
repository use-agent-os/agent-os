"""Read-only replay API for decision-log entries.

This module NEVER re-executes tools. It only reads
``decisions-YYYYMMDD.jsonl`` files and renders a human-readable transcript.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentos.observability.decision_log import (
    DecisionEntry,
    _default_log_dir,
    load_entries,
)


def load_turn(
    session_key: str,
    turn_id: str,
    log_dir: Path | None = None,
) -> DecisionEntry | None:
    """Find the DecisionEntry for ``(session_key, turn_id)`` or return ``None``.

    Scans all ``decisions-*.jsonl`` files under ``log_dir`` in filename order
    so callers don't need to know which day a turn was logged on.
    """

    log_dir = log_dir or _default_log_dir()
    if not log_dir.is_dir():
        return None
    for jsonl in sorted(log_dir.glob("decisions-*.jsonl")):
        for entry in load_entries(jsonl):
            if entry.session_key == session_key and entry.turn_id == turn_id:
                return entry
    return None


def format_transcript(entry: DecisionEntry) -> str:
    """Render a DecisionEntry as a human-readable transcript string."""

    session_label = entry.session_key
    if entry.session_id:
        session_label = f"{entry.session_key}/{entry.session_id}"
    lines = [
        f"Turn {entry.turn_id} (session {session_label})",
        f"  Model: {entry.model} / Provider: {entry.provider}",
        f"  Tokens: in={entry.tokens_input} out={entry.tokens_output}",
        f"  Latency: {entry.latency_ms} ms",
        f"  Tool choice: {entry.tool_choice}",
        (
            f"  Hashes: prompt={entry.prompt_hash} "
            f"system={entry.system_prompt_hash} "
            f"tools={entry.tool_list_hash}"
        ),
    ]
    lines.extend(_timing_lines(entry.timing))
    lines.append("  Pipeline steps:")
    for step in entry.pipeline_steps:
        status = "OK" if step.applied else f"FAIL({step.fallback_reason})"
        lines.append(
            f"    - {step.step_name} [{status}] "
            f"tier={step.routed_tier} "
            f"source={step.routing_source} "
            f"confidence={step.confidence}"
        )
    return "\n".join(lines)


def _timing_lines(timing: dict[str, Any]) -> list[str]:
    """Render the schema-16 ``timing`` breakdown; nothing for older rows."""

    if not isinstance(timing, dict) or not timing:
        return []

    def val(key: str, source: dict[str, Any] = timing) -> str:
        value = source.get(key)
        return "-" if value is None else f"{value}"

    def ms(key: str, source: dict[str, Any] = timing) -> str:
        value = source.get(key)
        return "-" if value is None else f"{value}ms"

    lines = [
        (
            f"  Timing: total={ms('total_ms')} pre_provider={ms('pre_provider_ms')} "
            f"first_text={ms('first_text_ms')} provider={ms('provider_ms')} "
            f"tools={ms('tool_ms')} approvals={ms('approval_wait_ms')} "
            f"finalize={ms('finalize_ms')} iterations={val('iterations')}"
        )
    ]
    stages = timing.get("stages")
    if isinstance(stages, dict) and stages:
        lines.append("  Stages: " + " ".join(f"{name}={value}ms" for name, value in stages.items()))
    retries = timing.get("retries")
    if isinstance(retries, dict) and retries:
        lines.append("  Retries: " + " ".join(f"{name}={value}" for name, value in retries.items()))
    calls = timing.get("provider_calls")
    if isinstance(calls, list):
        for index, call in enumerate(calls, start=1):
            if not isinstance(call, dict):
                continue
            lines.append(
                f"    - call {index}: start=+{ms('request_started_ms', call)} "
                f"first_event={ms('first_event_ms', call)} "
                f"first_text={ms('first_text_ms', call)} "
                f"done={ms('stream_done_ms', call)} "
                f"tokens in={val('input_tokens', call)} out={val('output_tokens', call)}"
            )
    batches = timing.get("tool_batch_detail")
    if isinstance(batches, list):
        for index, batch in enumerate(batches, start=1):
            if not isinstance(batch, dict):
                continue
            lines.append(
                f"    - tool batch {index}: start=+{ms('started_ms', batch)} "
                f"duration={ms('duration_ms', batch)} calls={val('calls', batch)} "
                f"approvals={ms('approval_wait_ms', batch)}"
            )
    return lines
