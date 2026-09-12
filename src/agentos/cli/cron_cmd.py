"""Cron scheduler CLI commands backed by AgentOS gateway RPCs."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from agentos.cli.gateway_rpc import confirm_or_exit, rpc_error_exit_code, run_gateway_sync
from agentos.cli.output import emit_error, print_json
from agentos.cli.ui import ACCENT_HEADER, console

cron_app = typer.Typer(help="Inspect and manage scheduled AgentOS runs.")

_SESSION_TARGETS = {"isolated", "main", "current", "session"}
_JOB_KINDS = {"auto", "reminder", "agent_turn", "system_event", "script"}
_WAKE_MODES = {"now", "next-heartbeat"}
_ELEVATED_MODES = {"bypass", "full"}

_ELEVATED_HELP = (
    "Let this job run shell-based skills unattended. Every time it fires, with "
    "nobody watching, the agent's shell commands run on this host as you, with "
    "no approval prompt and no sandbox. Anything the job reads from the network "
    "is one reasoning step away from that shell. Only use it for a job scoped to "
    "one skill and one narrow task."
)


def _validate_session_target(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in _SESSION_TARGETS:
        raise typer.BadParameter(
            "--session-target must be one of isolated, main, current, session"
        )
    return normalized


def _validate_script_path(script: str) -> str | None:
    """Reject a script path that cannot resolve inside the scripts directory.

    A local echo of the scheduler's rule (``scheduler/scripts.py``), kept here
    so a typo fails at the prompt instead of after a gateway round trip. The
    scheduler stays the authority — this CLI talks to it over RPC and never
    imports it.
    """
    raw = (script or "").strip()
    if not raw:
        return "--script requires a path"
    if raw.startswith(("/", "~", "\\")) or (len(raw) >= 2 and raw[1] == ":"):
        return (
            "--script must be relative to ~/.agentos/scripts/ — place the script "
            "there and pass just the file name"
        )
    if ".." in Path(raw).parts:
        return "--script must stay inside ~/.agentos/scripts/"
    return None


def _as_optional_str(value: Any) -> str | None:
    """Normalize an option that may arrive as typer's unfilled default.

    These commands are also called directly (tests, other CLI code), where an
    omitted option is still the ``typer.Option`` sentinel rather than ``None``.
    """
    return value if isinstance(value, str) else None


def _as_str_list(value: Any) -> list[str]:
    """Same normalization for a repeatable option."""
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str)]


def _validate_job_kind(value: Any) -> str:
    if not isinstance(value, str):
        return "auto"
    normalized = value.strip().lower()
    if normalized not in _JOB_KINDS:
        raise typer.BadParameter(
            "--job-kind must be one of auto, reminder, agent_turn, system_event, script"
        )
    return normalized


def _resolve_elevated(
    elevated: bool | None,
    elevated_mode: str | None,
) -> str | bool | None:
    """Turn the --elevated / --elevated-mode pair into one wire value.

    Returns ``None`` when neither flag was passed, so the key stays out of the
    params dict entirely and an update leaves the stored setting alone.
    """

    if elevated_mode is not None:
        mode = elevated_mode.strip().lower()
        if mode not in _ELEVATED_MODES:
            raise typer.BadParameter("--elevated-mode must be bypass or full")
        if elevated is False:
            raise typer.BadParameter("--no-elevated cannot be combined with --elevated-mode")
        return mode
    if elevated is None:
        return None
    return "bypass" if elevated else False


def _parse_tool_policy(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"--tool-policy must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise typer.BadParameter("--tool-policy must be a JSON object")
    return parsed


_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|m|h|sec|secs|min|mins|hr|hrs)?\s*$")
_DURATION_UNIT_SECONDS = {
    "": 1.0,
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
}


def _parse_duration_seconds(value: str | float | None) -> float | None:
    """Accept '30s', '5m', '1h', or a plain numeric string/float. Return seconds.

    None / empty string → None (caller decides default). Anything ambiguous
    raises typer.BadParameter so the user sees the failure at parse time.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip()
    if not raw:
        return None
    match = _DURATION_RE.match(raw.lower())
    if not match:
        raise typer.BadParameter(
            f"invalid duration {value!r}: expected '30s', '5m', '1h', or seconds"
        )
    qty = float(match.group(1))
    unit = match.group(2) or ""
    return qty * _DURATION_UNIT_SECONDS[unit]


def _build_schedule_param(
    *,
    expression: str | None,
    cron: str | None,
    every: str | int | float | None,
    at: str | None,
    tz: str | None,
) -> dict[str, Any]:
    sources = [
        ("expression", expression.strip() if isinstance(expression, str) else expression),
        ("cron", cron.strip() if isinstance(cron, str) else cron),
        ("every", every),
        ("at", at.strip() if isinstance(at, str) else at),
    ]
    provided = [(name, value) for name, value in sources if value not in (None, "")]
    if len(provided) != 1:
        raise typer.BadParameter(
            "provide exactly one schedule source: --expression, --cron, --every, or --at"
        )
    name, value = provided[0]
    if name == "expression":
        return {"expression": str(value)}
    if name == "cron":
        schedule: dict[str, Any] = {"kind": "cron", "expr": str(value)}
        if tz:
            schedule["tz"] = tz
        return {"schedule": schedule}
    if name == "every":
        seconds = _parse_duration_seconds(value)
        if seconds is None or seconds < 1:
            raise typer.BadParameter("--every must be a duration >= 1 second")
        if not seconds.is_integer():
            raise typer.BadParameter("--every must resolve to whole seconds")
        return {"schedule": {"kind": "every", "every_seconds": int(seconds)}}
    return {"schedule": {"kind": "at", "at": str(value)}}


def _build_optional_schedule_param(
    *,
    expression: str | None,
    cron: str | None,
    every: str | int | float | None,
    at: str | None,
    tz: str | None,
) -> dict[str, Any]:
    sources = [
        expression.strip() if isinstance(expression, str) else expression,
        cron.strip() if isinstance(cron, str) else cron,
        every,
        at.strip() if isinstance(at, str) else at,
    ]
    if all(value in (None, "") for value in sources):
        return {}
    if sum(1 for value in sources if value not in (None, "")) > 1:
        raise typer.BadParameter(
            "provide at most one schedule source: --expression, --cron, --every, or --at"
        )
    return _build_schedule_param(
        expression=expression,
        cron=cron,
        every=every,
        at=at,
        tz=tz,
    )


def _resolve_webhook_token(
    *,
    inline: str | None,
    env: str | None,
    path: str | None,
) -> str | None:
    """Resolve a webhook bearer token from the safest available source.

    Priority: --webhook-token-env > --webhook-token-file > --webhook-token.
    Multiple sources is a ValueError so scripts fail loud instead of guessing.
    Inline --webhook-token is supported but emits a warning because it leaks
    via shell history and process listings.
    """
    sources = [bool(env), bool(path), bool(inline)]
    if sum(sources) > 1:
        raise typer.BadParameter(
            "specify at most one of --webhook-token, --webhook-token-env, "
            "--webhook-token-file"
        )
    if env:
        value = os.environ.get(env)
        if not value:
            raise typer.BadParameter(
                f"--webhook-token-env: environment variable {env!r} is unset or empty"
            )
        return value
    if path:
        try:
            return Path(path).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise typer.BadParameter(f"--webhook-token-file: {exc}") from exc
    if inline:
        typer.echo(
            "warning: --webhook-token is visible in shell history and process "
            "listings; prefer --webhook-token-env NAME or --webhook-token-file PATH.",
            err=True,
        )
        return inline
    return None


def _build_failure_destination_dict(
    *,
    mode: str | None,
    channel: str | None,
    to: str | None,
    account: str | None,
    webhook_url: str | None,
    webhook_token: str | None,
) -> dict[str, Any] | None:
    """Translate --failure-* flags into a delivery.failureDestination dict.

    Returns None when no failure-* flag is set. Raises BadParameter when the
    selected mode is missing required fields (webhook URL for webhook mode,
    channel + recipient for channel mode).
    """
    any_failure_flag = any(
        bool(v) for v in (mode, channel, to, account, webhook_url, webhook_token)
    )
    if not any_failure_flag:
        return None
    if not mode:
        raise typer.BadParameter(
            "--failure-* flags require --failure-mode (channel or webhook)"
        )
    mode_norm = mode.strip().lower()
    if mode_norm not in ("channel", "webhook"):
        raise typer.BadParameter("--failure-mode must be 'channel' or 'webhook'")

    if mode_norm == "webhook":
        if not webhook_url:
            raise typer.BadParameter(
                "--failure-mode=webhook requires --failure-webhook-url"
            )
        fd: dict[str, Any] = {"mode": "webhook", "webhookUrl": webhook_url}
        if webhook_token:
            fd["webhookToken"] = webhook_token
        return fd

    # channel mode
    if webhook_url or webhook_token:
        raise typer.BadParameter(
            "--failure-webhook-* requires --failure-mode=webhook"
        )
    if not (channel or to):
        raise typer.BadParameter(
            "--failure-mode=channel requires --failure-channel and/or --failure-to"
        )
    fd = {"mode": "channel"}
    if channel:
        fd["channelName"] = channel.strip().lower()
    if to:
        fd["to"] = to
    if account:
        fd["accountId"] = account
    return fd


def _build_delivery_params(
    *,
    announce: bool,
    no_deliver: bool,
    channel: str | None,
    to: str | None,
    account: str | None,
    best_effort: bool,
    webhook_url: str | None,
    webhook_token: str | None,
    failure_destination: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Translate CLI delivery flags into a delivery dict for the cron.add RPC.

    Returns None when the user did not request any delivery override AND no
    failure_destination was provided — the backend then falls back to its
    own inference (session last_channel or none). When only a failure
    destination is set, returns ``{"failureDestination": {...}}`` so the
    backend attaches it to the inferred or default primary delivery.
    """
    declared = sum([announce, no_deliver, bool(webhook_url)])
    if declared > 1:
        raise typer.BadParameter(
            "choose at most one delivery mode: --announce, --no-deliver, "
            "or --webhook-url"
        )

    if webhook_url:
        delivery: dict[str, Any] = {"mode": "webhook", "webhookUrl": webhook_url}
        if webhook_token:
            delivery["webhookToken"] = webhook_token
        if best_effort:
            delivery["bestEffort"] = True
        if failure_destination is not None:
            delivery["failureDestination"] = failure_destination
        return delivery

    if webhook_token and not webhook_url:
        raise typer.BadParameter(
            "--webhook-token* requires --webhook-url"
        )

    if no_deliver:
        result: dict[str, Any] = {"mode": "none"}
        if failure_destination is not None:
            result["failureDestination"] = failure_destination
        return result

    # Channel-mode announce. 'last' is a CLI sentinel that means "let the
    # backend infer from the session's last route"; do not forward it as an
    # explicit channelName because the RPC's _parse_delivery_overrides treats
    # any channelName as an explicit override.
    channel_norm = (channel or "").strip().lower()
    has_target = bool(to) or (channel_norm not in ("", "last"))
    if not announce and not has_target and not best_effort and not account:
        if failure_destination is not None:
            # FD-only — backend keeps inferred primary delivery and attaches FD.
            return {"failureDestination": failure_destination}
        return None  # nothing requested

    delivery = {"mode": "announce"}
    if channel_norm and channel_norm != "last":
        delivery["channelName"] = channel_norm
    if to:
        delivery["to"] = to
    if account:
        delivery["accountId"] = account
    if best_effort:
        delivery["bestEffort"] = True
    if failure_destination is not None:
        delivery["failureDestination"] = failure_destination
    return delivery


def _job_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get("jobs", [])
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _render_jobs(rows: list[dict[str, Any]], *, title: str = "Cron jobs") -> None:
    if not rows:
        typer.echo("No cron jobs.")
        return
    table = Table(title=title, show_header=True, header_style=ACCENT_HEADER)
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Enabled")
    table.add_column("Expression")
    table.add_column("Kind")
    table.add_column("Agent")
    table.add_column("Elevated")
    table.add_column("Next run")
    table.add_column("Last run")
    table.add_column("Errors", justify="right")
    for row in rows:
        table.add_row(
            str(row.get("id") or ""),
            str(row.get("name") or ""),
            str(row.get("enabled") or False),
            str(row.get("expression") or row.get("schedule_raw") or ""),
            str(row.get("payloadKind") or row.get("payload_kind") or ""),
            str(row.get("agentId") or row.get("agent_id") or ""),
            str(
                row.get("effectiveElevated")
                or row.get("elevated")
                or ""
            ),
            str(row.get("next_run") or ""),
            str(row.get("last_run") or ""),
            str(row.get("error_count") or row.get("consecutive_errors") or 0),
        )
    console.print(table)


def _render_mapping(payload: dict[str, Any], *, title: str) -> None:
    table = Table(title=title, show_header=True, header_style=ACCENT_HEADER)
    table.add_column("Field")
    table.add_column("Value")
    for key, value in payload.items():
        table.add_row(str(key), str(value))
    console.print(table)


_RUN_OUTPUT_WIDTH = 60


def _run_output_cell(summary: Any) -> str:
    """One-line preview of a run's output for the runs table.

    A script job's stdout *is* its result, and it is routinely multi-line, so it
    is flattened and clipped here rather than allowed to break the table. The
    untruncated text stays available through ``--json``.
    """
    text = " ".join(str(summary or "").split())
    if not text:
        return ""
    if len(text) <= _RUN_OUTPUT_WIDTH:
        return text
    return text[: _RUN_OUTPUT_WIDTH - 1] + "…"


def _render_runs(rows: list[dict[str, Any]]) -> None:
    if not rows:
        typer.echo("No cron runs.")
        return
    table = Table(title="Cron runs", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("ID")
    table.add_column("Started")
    table.add_column("Finished")
    table.add_column("Status")
    table.add_column("Duration ms", justify="right")
    table.add_column("Delivery")
    table.add_column("Output")
    table.add_column("Error")
    for row in rows:
        table.add_row(
            str(row.get("id") or ""),
            str(row.get("started_at") or ""),
            str(row.get("finished_at") or ""),
            str(row.get("status") or ("ok" if row.get("success") else "error")),
            str(row.get("duration_ms") or ""),
            str(row.get("deliveryStatus") or row.get("delivery_status") or ""),
            _run_output_cell(row.get("summary")),
            str(row.get("error") or ""),
        )
    console.print(table)


def _emit_success(payload: Any, *, json_output: bool, title: str) -> None:
    if json_output:
        print_json(payload)
    elif isinstance(payload, dict):
        _render_mapping(payload, title=title)
    else:
        typer.echo(str(payload))


@cron_app.command("list")
def cron_list(
    agent: str | None = typer.Option(None, "--agent", help="Filter by agent id"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List scheduled cron jobs."""

    async def _run(client):
        params: dict[str, Any] = {}
        if agent:
            params["agentId"] = agent
        return await client.call("cron.list", params)

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    _render_jobs(_job_rows(payload))


@cron_app.command("status")
def cron_status(
    job_id: str = typer.Argument(..., help="Cron job id"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show one cron job."""

    async def _run(client):
        return await client.call("cron.status", {"id": job_id})

    payload = run_gateway_sync(_run, json_output=json_output)
    _emit_success(payload, json_output=json_output, title=f"Cron job {job_id}")


@cron_app.command("add")
def cron_add(
    expression: Annotated[
        str | None, typer.Option("--expression", help="Cron expression")
    ] = None,
    cron: Annotated[
        str | None, typer.Option("--cron", help="Cron expression schedule")
    ] = None,
    every: Annotated[
        str | None, typer.Option("--every", help="Fixed interval, e.g. 30s, 5m, 1h")
    ] = None,
    at: Annotated[
        str | None, typer.Option("--at", help="One-time ISO-8601 time with timezone")
    ] = None,
    text: str | None = typer.Option(
        None,
        "--text",
        help="Prompt text to run. Required for every kind except script.",
    ),
    script: str | None = typer.Option(
        None,
        "--script",
        help=(
            "Run this script, relative to ~/.agentos/scripts/. On its own it "
            "creates a script job: stdout is delivered verbatim and no LLM "
            "runs. With --job-kind agent_turn it becomes a pre-run collector — "
            "the agent sees the stdout, and no output means the turn is skipped."
        ),
    ),
    script_arg: list[str] | None = typer.Option(
        None,
        "--script-arg",
        help="Argument passed to --script. Repeat for several; never shell-interpreted.",
    ),
    workdir: str | None = typer.Option(
        None,
        "--workdir",
        help="Working directory for --script (defaults to the script's own directory).",
    ),
    name: str | None = typer.Option(None, "--name", help="Display name"),
    agent: str | None = typer.Option(None, "--agent", help="Agent id"),
    job_kind: str = typer.Option(
        "auto",
        "--job-kind",
        help=(
            "Cron payload kind: auto, reminder, agent_turn, system_event, or script. "
            "auto creates static reminders for non-main targets, system events for "
            "main, and script jobs when --script is given."
        ),
    ),
    session_target: str = typer.Option(
        "isolated",
        "--session-target",
        help="Target session mode: isolated, main, current, or session",
    ),
    session_key: str | None = typer.Option(
        None,
        "--session-key",
        help=(
            "Chat session this job reports into. Without it a job scheduled "
            "from the CLI has no conversation to mirror results to — which is "
            "how a --script job ends up running with nothing to show for it. "
            "List keys with 'agentos sessions list'."
        ),
    ),
    timeout: float | None = typer.Option(None, "--timeout", help="Run timeout in seconds"),
    tz: str | None = typer.Option(
        None,
        "--tz",
        help=(
            "IANA timezone for cron expressions (e.g. 'America/Los_Angeles'). "
            "Empty/omitted keeps UTC."
        ),
    ),
    wake: str | None = typer.Option(
        None,
        "--wake",
        help="Wake mode for main-session jobs: now or next-heartbeat",
    ),
    exact: bool = typer.Option(
        False,
        "--exact",
        help="Fire exactly on schedule (jitter_seconds=0); overrides default stagger.",
    ),
    jitter: str | None = typer.Option(
        None,
        "--jitter",
        help=(
            "Explicit stagger (per-job). Accepts '30s', '5m', '1h', or a numeric "
            "second count. 0 == --exact; takes precedence over --exact."
        ),
    ),
    announce: bool = typer.Option(
        False, "--announce", help="Announce summary delivery (channel mode)."
    ),
    no_deliver: bool = typer.Option(
        False,
        "--no-deliver",
        help="Disable any delivery for this job.",
    ),
    channel: str | None = typer.Option(
        None,
        "--channel",
        help=(
            "Delivery channel (e.g. slack, discord). 'last' or unset → let the "
            "backend infer from the session's last route."
        ),
    ),
    to: str | None = typer.Option(
        None, "--to", help="Delivery destination (channel-specific recipient)"
    ),
    account: str | None = typer.Option(
        None, "--account", help="Channel account id for delivery (multi-account setups)"
    ),
    best_effort_deliver: bool = typer.Option(
        False,
        "--best-effort-deliver",
        help="Do not fail the job when delivery fails",
    ),
    webhook_url: str | None = typer.Option(
        None,
        "--webhook-url",
        help="Webhook delivery URL (http/https); mutually exclusive with --announce/--no-deliver.",
    ),
    webhook_token: str | None = typer.Option(
        None,
        "--webhook-token",
        help=(
            "Webhook bearer token (visible in shell history; prefer "
            "--webhook-token-env or --webhook-token-file)."
        ),
    ),
    webhook_token_env: str | None = typer.Option(
        None,
        "--webhook-token-env",
        help="Read webhook bearer token from this environment variable.",
    ),
    webhook_token_file: str | None = typer.Option(
        None,
        "--webhook-token-file",
        help="Read webhook bearer token from this file (whitespace-trimmed).",
    ),
    failure_mode: str | None = typer.Option(
        None,
        "--failure-mode",
        help=(
            "Route failure alerts separately from primary delivery. "
            "One of: 'channel', 'webhook'."
        ),
    ),
    failure_channel: str | None = typer.Option(
        None,
        "--failure-channel",
        help="Failure-destination channel name (slack, discord, …) for --failure-mode=channel.",
    ),
    failure_to: str | None = typer.Option(
        None,
        "--failure-to",
        help="Failure-destination recipient (channel-specific) for --failure-mode=channel.",
    ),
    failure_account: str | None = typer.Option(
        None,
        "--failure-account",
        help="Failure-destination channel account id (multi-account setups).",
    ),
    failure_webhook_url: str | None = typer.Option(
        None,
        "--failure-webhook-url",
        help="Failure-destination webhook URL (http/https) for --failure-mode=webhook.",
    ),
    failure_webhook_token: str | None = typer.Option(
        None,
        "--failure-webhook-token",
        help=(
            "Failure-destination webhook bearer token (visible in shell history; "
            "prefer --failure-webhook-token-env or --failure-webhook-token-file)."
        ),
    ),
    failure_webhook_token_env: str | None = typer.Option(
        None,
        "--failure-webhook-token-env",
        help="Read failure-destination webhook token from this environment variable.",
    ),
    failure_webhook_token_file: str | None = typer.Option(
        None,
        "--failure-webhook-token-file",
        help="Read failure-destination webhook token from this file (trimmed).",
    ),
    elevated: bool | None = typer.Option(
        None,
        "--elevated/--no-elevated",
        help=_ELEVATED_HELP,
    ),
    elevated_mode: str | None = typer.Option(
        None,
        "--elevated-mode",
        help=(
            "Elevation mode: bypass (default, keeps the sensitive-path block) or "
            "full (also disables it — only with a specific reason)."
        ),
    ),
    tool_policy: str | None = typer.Option(
        None,
        "--tool-policy",
        help=(
            "Per-job tool policy as JSON, e.g. '{\"deny\": [\"web_fetch\"]}'. "
            "Keys: profile, allow, alsoAllow, deny. Can only narrow the cron baseline."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Add a scheduled cron job."""

    text = _as_optional_str(text)
    script = _as_optional_str(script)
    workdir = _as_optional_str(workdir)
    session_key = _as_optional_str(session_key)
    script_args = _as_str_list(script_arg)
    target = _validate_session_target(session_target)
    payload_kind = _validate_job_kind(job_kind)
    if payload_kind == "auto":
        if script:
            payload_kind = "script"
        else:
            payload_kind = "system_event" if target == "main" else "reminder"
    if payload_kind == "reminder" and target == "main":
        raise typer.BadParameter("--job-kind reminder cannot use --session-target main")
    if payload_kind == "agent_turn" and target == "main":
        raise typer.BadParameter("--job-kind agent_turn cannot use --session-target main")
    if payload_kind == "system_event" and target != "main":
        raise typer.BadParameter("--job-kind system_event requires --session-target main")
    if payload_kind == "script":
        if target == "main":
            raise typer.BadParameter("--job-kind script cannot use --session-target main")
        if not script:
            raise typer.BadParameter("--job-kind script requires --script")
    elif payload_kind == "agent_turn":
        # --script here is a pre-run collector, not the job itself.
        if not text or not text.strip():
            raise typer.BadParameter("--text is required")
    else:
        if script:
            raise typer.BadParameter(
                "--script is only used by script and agent_turn jobs; "
                "pass --job-kind script or --job-kind agent_turn"
            )
        if not text or not text.strip():
            raise typer.BadParameter("--text is required")
    if script:
        script_error = _validate_script_path(script)
        if script_error:
            raise typer.BadParameter(script_error)
    elif workdir or script_args:
        raise typer.BadParameter("--workdir and --script-arg require --script")
    if target == "current":
        raise typer.BadParameter(
            "--session-target current is only available from session-bound clients; "
            "use the WebUI/current chat surface or choose --session-target isolated"
        )
    if session_key and target == "main":
        raise typer.BadParameter(
            "--session-key cannot be combined with --session-target main; "
            "main-target jobs report through the heartbeat, not a chat"
        )
    if target == "session" and not session_key:
        raise typer.BadParameter("--session-target session requires --session-key")
    params: dict[str, Any] = {
        **_build_schedule_param(
            expression=expression,
            cron=cron,
            every=every,
            at=at,
            tz=tz,
        ),
        "text": text or "",
        "payloadKind": payload_kind,
        "sessionTarget": target,
    }
    if session_key:
        params["sessionKey"] = session_key
    if script:
        params["script"] = script
    if workdir:
        params["workdir"] = workdir
    if script_args:
        params["scriptArgs"] = script_args
    if name:
        params["name"] = name
    if agent:
        params["agentId"] = agent
    if timeout is not None:
        params["timeout"] = timeout
    if tz:
        params["tz"] = tz
    if wake is not None:
        wake_norm = wake.strip().lower()
        if wake_norm not in _WAKE_MODES:
            raise typer.BadParameter("--wake must be now or next-heartbeat")
        params["wakeMode"] = wake_norm

    jitter_seconds = _parse_duration_seconds(jitter)
    if jitter_seconds is not None:
        params["jitterSeconds"] = max(0.0, jitter_seconds)
    elif exact:
        params["exact"] = True

    token = _resolve_webhook_token(
        inline=webhook_token,
        env=webhook_token_env,
        path=webhook_token_file,
    )
    failure_token = _resolve_webhook_token(
        inline=failure_webhook_token,
        env=failure_webhook_token_env,
        path=failure_webhook_token_file,
    )
    failure_destination = _build_failure_destination_dict(
        mode=failure_mode,
        channel=failure_channel,
        to=failure_to,
        account=failure_account,
        webhook_url=failure_webhook_url,
        webhook_token=failure_token,
    )
    delivery = _build_delivery_params(
        announce=announce,
        no_deliver=no_deliver,
        channel=channel,
        to=to,
        account=account,
        best_effort=best_effort_deliver,
        webhook_url=webhook_url,
        webhook_token=token,
        failure_destination=failure_destination,
    )
    if delivery is not None:
        params["delivery"] = delivery

    parsed_tool_policy = _parse_tool_policy(tool_policy)
    if parsed_tool_policy is not None:
        params["toolPolicy"] = parsed_tool_policy
    resolved_elevated = _resolve_elevated(elevated, elevated_mode)
    if resolved_elevated is not None:
        params["elevated"] = resolved_elevated

    async def _run(client):
        return await client.call("cron.add", params)

    payload = run_gateway_sync(_run, json_output=json_output)
    _emit_success(payload, json_output=json_output, title="Cron job added")


@cron_app.command("update")
def cron_update(
    job_id: str = typer.Argument(..., help="Cron job id"),
    expression: Annotated[
        str | None, typer.Option("--expression", help="Cron expression")
    ] = None,
    cron: Annotated[
        str | None, typer.Option("--cron", help="Cron expression schedule")
    ] = None,
    every: Annotated[
        str | None, typer.Option("--every", help="Fixed interval, e.g. 30s, 5m, 1h")
    ] = None,
    at: Annotated[
        str | None, typer.Option("--at", help="One-time ISO-8601 time with timezone")
    ] = None,
    text: str | None = typer.Option(None, "--text", help="Prompt text to run"),
    job_kind: str | None = typer.Option(
        None,
        "--job-kind",
        help=(
            "Convert the job to another kind: reminder, agent_turn, system_event, "
            "or script. Converting to script also needs --script."
        ),
    ),
    script: str | None = typer.Option(
        None,
        "--script",
        help=(
            "Point the job at a different script under ~/.agentos/scripts/. "
            "Pass an empty string to drop an agent turn's pre-run script."
        ),
    ),
    script_arg: list[str] | None = typer.Option(
        None,
        "--script-arg",
        help="Replace the script's arguments. Repeat for several.",
    ),
    workdir: str | None = typer.Option(
        None,
        "--workdir",
        help="Working directory for the job's script (empty string clears it).",
    ),
    name: str | None = typer.Option(None, "--name", help="Display name"),
    enabled: bool | None = typer.Option(None, "--enabled/--disabled", help="Enable/disable job"),
    timeout: float | None = typer.Option(None, "--timeout", help="Run timeout in seconds"),
    tz: str | None = typer.Option(
        None,
        "--tz",
        help=(
            "Update IANA timezone for cron expressions. Pass an empty string to clear "
            "(revert to UTC matching)."
        ),
    ),
    wake: str | None = typer.Option(
        None,
        "--wake",
        help="Wake mode: now or next-heartbeat",
    ),
    failure_mode: str | None = typer.Option(
        None,
        "--failure-mode",
        help=(
            "Patch the failure-alert route. One of: 'channel', 'webhook'. "
            "Other delivery fields (primary channel/webhook) are not patchable "
            "from this CLI — remove + re-add to repoint primary delivery."
        ),
    ),
    failure_channel: str | None = typer.Option(
        None,
        "--failure-channel",
        help="Failure-destination channel name for --failure-mode=channel.",
    ),
    failure_to: str | None = typer.Option(
        None,
        "--failure-to",
        help="Failure-destination recipient for --failure-mode=channel.",
    ),
    failure_account: str | None = typer.Option(
        None,
        "--failure-account",
        help="Failure-destination channel account id (multi-account setups).",
    ),
    failure_webhook_url: str | None = typer.Option(
        None,
        "--failure-webhook-url",
        help="Failure-destination webhook URL for --failure-mode=webhook.",
    ),
    failure_webhook_token: str | None = typer.Option(
        None,
        "--failure-webhook-token",
        help=(
            "Failure-destination webhook bearer token (prefer "
            "--failure-webhook-token-env or --failure-webhook-token-file)."
        ),
    ),
    failure_webhook_token_env: str | None = typer.Option(
        None,
        "--failure-webhook-token-env",
        help="Read failure-destination webhook token from this environment variable.",
    ),
    failure_webhook_token_file: str | None = typer.Option(
        None,
        "--failure-webhook-token-file",
        help="Read failure-destination webhook token from this file (trimmed).",
    ),
    elevated: bool | None = typer.Option(
        None,
        "--elevated/--no-elevated",
        help=_ELEVATED_HELP,
    ),
    elevated_mode: str | None = typer.Option(
        None,
        "--elevated-mode",
        help=(
            "Elevation mode: bypass (default, keeps the sensitive-path block) or "
            "full (also disables it — only with a specific reason)."
        ),
    ),
    tool_policy: str | None = typer.Option(
        None,
        "--tool-policy",
        help=(
            "Replace the per-job tool policy with this JSON object. Keys: profile, "
            "allow, alsoAllow, deny. Use --elevated on its own to toggle elevation "
            "without touching the lists."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Update a scheduled cron job.

    Primary delivery (channel / webhook URL) is intentionally NOT patchable
    from this CLI — remove + re-add when those need to change. Failure
    destination IS patchable via the --failure-* flags.
    """

    script = _as_optional_str(script)
    workdir = _as_optional_str(workdir)
    job_kind = _as_optional_str(job_kind)
    script_args = _as_str_list(script_arg)
    params: dict[str, Any] = {"id": job_id}
    if job_kind is not None:
        payload_kind = _validate_job_kind(job_kind)
        if payload_kind == "auto":
            raise typer.BadParameter(
                "--job-kind on update must name a kind: reminder, agent_turn, "
                "system_event, or script"
            )
        params["payloadKind"] = payload_kind
    params.update(
        _build_optional_schedule_param(
            expression=expression,
            cron=cron,
            every=every,
            at=at,
            tz=tz,
        )
    )
    if text is not None:
        params["text"] = text
    if script is not None:
        # An explicit empty string clears the job's script; anything else has
        # to be a usable path.
        if script.strip():
            script_error = _validate_script_path(script)
            if script_error:
                raise typer.BadParameter(script_error)
        params["script"] = script
    if workdir is not None:
        params["workdir"] = workdir
    if script_args:
        params["scriptArgs"] = script_args
    if name is not None:
        params["name"] = name
    if enabled is not None:
        params["enabled"] = enabled
    if timeout is not None:
        params["timeout"] = timeout
    if tz is not None:
        params["tz"] = tz
    if wake is not None:
        wake_norm = wake.strip().lower()
        if wake_norm not in _WAKE_MODES:
            raise typer.BadParameter("--wake must be now or next-heartbeat")
        params["wakeMode"] = wake_norm

    failure_token = _resolve_webhook_token(
        inline=failure_webhook_token,
        env=failure_webhook_token_env,
        path=failure_webhook_token_file,
    )
    failure_destination = _build_failure_destination_dict(
        mode=failure_mode,
        channel=failure_channel,
        to=failure_to,
        account=failure_account,
        webhook_url=failure_webhook_url,
        webhook_token=failure_token,
    )
    if failure_destination is not None:
        params["delivery"] = {"failureDestination": failure_destination}

    parsed_tool_policy = _parse_tool_policy(tool_policy)
    if parsed_tool_policy is not None:
        params["toolPolicy"] = parsed_tool_policy
    resolved_elevated = _resolve_elevated(elevated, elevated_mode)
    if resolved_elevated is not None:
        params["elevated"] = resolved_elevated

    if len(params) == 1:
        raise typer.BadParameter("provide at least one field to update")

    async def _run(client):
        return await client.call("cron.update", params)

    payload = run_gateway_sync(_run, json_output=json_output)
    _emit_success(payload, json_output=json_output, title="Cron job updated")


@cron_app.command("remove")
def cron_remove(
    job_id: str = typer.Argument(..., help="Cron job id"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Remove a scheduled cron job."""

    confirm_or_exit(f"Remove cron job {job_id!r}?", yes=yes, json_output=json_output)

    async def _run(client):
        await client.call("cron.remove", {"id": job_id})
        return {"id": job_id, "removed": True}

    payload = run_gateway_sync(_run, json_output=json_output)
    _emit_success(payload, json_output=json_output, title="Cron job removed")


@cron_app.command("run")
def cron_run(
    job_id: str = typer.Argument(..., help="Cron job id"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Run a scheduled cron job now."""

    confirm_or_exit(
        f"Run cron job {job_id!r} now? This may post into a live session or channel.",
        yes=yes,
        json_output=json_output,
    )

    async def _run(client):
        return await client.call("cron.run", {"id": job_id})

    payload = run_gateway_sync(_run, json_output=json_output)
    _emit_success(payload, json_output=json_output, title="Cron run result")
    _exit_on_manual_run_failure(payload, json_output=json_output)


def _exit_on_manual_run_failure(payload: Any, *, json_output: bool) -> None:
    """Turn a ``success: false`` ``cron.run`` payload into a non-zero exit.

    ``cron.run`` reports a missing / disabled / busy job (and a run whose
    handler failed) as a *successful* RPC carrying ``success: false`` so the
    Control UI can render it; the CLI has to map that onto ``$?`` itself or a
    script would treat a job that never ran as a success. ``not_found`` exits
    2 like ``cron status`` / ``cron update`` on the same id; every other
    failure exits 1. The wire payload has already been printed above, so
    ``--json`` consumers keep the full body on stdout.
    """

    if not isinstance(payload, dict) or payload.get("success") is not False:
        return
    status = str(payload.get("status") or "").strip().lower()
    error = payload.get("error")
    reason = payload.get("reason")
    if error:
        message = str(error)
    elif status == "accepted":
        message = "cron run failed"
    else:
        message = f"cron run rejected: {reason or status or 'unknown'}"
    if status == "not_found":
        code = "NOT_FOUND"
    elif status and status != "accepted":
        code = f"RUN_{status.upper()}"
    else:
        code = "RUN_FAILED"
    emit_error(message, json_output=json_output, code=code)
    raise typer.Exit(rpc_error_exit_code(code))


@cron_app.command("runs")
def cron_runs(
    job_id: str = typer.Argument(..., help="Cron job id"),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum rows"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List recent runs for a cron job."""

    async def _run(client):
        return await client.call("cron.runs", {"id": job_id, "limit": limit})

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    _render_runs(_job_rows(payload))


@cron_app.command("output")
def cron_output(
    job_id: str = typer.Argument(..., help="Cron job id"),
    run: str = typer.Option("", "--run", "-r", help="Run id (default: the most recent run)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Print the full output of one run.

    'cron runs' shows a short preview per row; this prints the whole thing for a
    single run — which for a script job is its entire stdout.
    """

    async def _run(client):
        params = {"id": job_id}
        if run:
            params["runId"] = run
        return await client.call("cron.runOutput", params)

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    if isinstance(payload, dict):
        error = payload.get("error")
        if error:
            console.print(f"[red]error:[/red] {error}")
        output = payload.get("output")
        if output:
            # Raw script stdout — JSON brackets must not be read as rich markup.
            console.print(output, markup=False, highlight=False)
        else:
            console.print("[dim](no output)[/dim]")
