"""``agentos flows``: list, inspect and run Image Flows over gateway RPC.

Image Flows (``docs/flows.md``) are node graphs the gateway stores and runs;
the desktop's Flows editor is where they are built. Every command here is a
thin client of the ``flows.*`` RPC on the running gateway, so a run started
from the terminal shows up live in the editor too. Runs started here carry
``origin: "cli"``. From inside an agent's shell the gateway refuses
``flows.run`` / ``flows.stop`` (``flows.operator_required``): agents run flows
through their ``flow_run`` tool, which applies the cost gate.
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

import typer
from rich.table import Table
from rich.text import Text

from agentos.cli.gateway_rpc import run_gateway_sync
from agentos.cli.output import emit_error, print_json
from agentos.cli.ui import ACCENT_HEADER, console

flows_app = typer.Typer(
    help=(
        "Image Flows: list, inspect and run the node-graph image workflows built in "
        "the desktop Flows editor."
    ),
    no_args_is_help=True,
)

TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})
#: Gateway error codes that mean "change the input": exit 2, not 1.
_USAGE_CODES = frozenset({"flows.invalid", "flows.not_found", "flows.conflict"})
POLL_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 600


def _cell(value: Any) -> Text:
    """Gateway data shown as stored (never parsed as Rich markup)."""
    return Text("" if value is None else str(value))


def _usd(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "-"
    if value == 0 or value >= 0.1:
        return f"${value:.2f}"
    return f"${value:.3f}" if value >= 0.01 else f"${value:.4f}"


def price_text(price: Any) -> str:
    """A Price as the desktop writes it: ``free``, ``~$0.067``, ``~$0.003–$0.10``, ``unknown``."""
    if not isinstance(price, dict):
        return "unknown"
    mark = "~" if price.get("approximate") else ""
    kind = price.get("kind")
    if kind == "free":
        return "free"
    if kind == "usd" and isinstance(price.get("usd"), (int, float)):
        return f"{mark}{_usd(price['usd'])}"
    low, high = price.get("min"), price.get("max")
    if kind == "range" and isinstance(low, (int, float)) and isinstance(high, (int, float)):
        if low == high:
            return f"{mark}{_usd(low)}"
        return f"{mark}{_usd(low)}–{_usd(high)}"
    return "unknown"


def _per_image_text(per_image: Any, *, approximate: bool) -> str:
    """``~1K $0.067, 2K $0.10``, ``~$0.10`` or a range when a model has many prices."""
    if not isinstance(per_image, dict) or not per_image:
        return "price unknown"
    prefix = "~" if approximate else ""
    numbers = [v for v in per_image.values() if isinstance(v, (int, float))]
    if list(per_image) == ["default"]:
        return f"{prefix}{_usd(per_image['default'])}/image"
    if len(per_image) > 4 and numbers:
        return f"{prefix}{_usd(min(numbers))}–{_usd(max(numbers))}/image by quality and size"
    return prefix + ", ".join(f"{key} {_usd(value)}" for key, value in per_image.items()) + "/image"


def _node_errors_text(details: Any) -> list[str]:
    if not isinstance(details, dict):
        return []
    node_errors = details.get("node_errors")
    if not isinstance(node_errors, dict) and isinstance(details.get("details"), dict):
        node_errors = details["details"].get("node_errors")
    if not isinstance(node_errors, dict):
        return []
    lines = []
    for node_id, errors in node_errors.items():
        for error in errors if isinstance(errors, list) else []:
            if not isinstance(error, dict):
                continue
            where = f"{node_id}.{error['input']}" if error.get("input") else str(node_id)
            lines.append(f"  {where}: {error.get('message') or error.get('code')}")
    return lines


async def _call(client: Any, method: str, params: dict[str, Any], *, json_output: bool) -> Any:
    """One ``flows.*`` call; an input the engine refuses exits 2 with its node errors."""
    from agentos.cli.gateway_client import GatewayRPCError

    try:
        return await client.call(method, params)
    except GatewayRPCError as exc:
        if exc.code not in _USAGE_CODES:
            raise
        message = exc.message
        if not json_output:
            lines = _node_errors_text(exc.data)
            if lines:
                message = message + "\n" + "\n".join(lines)
        emit_error(message, json_output=json_output, code=exc.code, details=exc.data)
        raise typer.Exit(2) from exc


async def _resolve(client: Any, ref: str, *, json_output: bool) -> dict[str, Any]:
    """The flow summary for an id or an exact name (ids win, like the engine)."""
    listing = await _call(client, "flows.list", {}, json_output=json_output)
    flows = [f for f in (listing or {}).get("flows") or [] if isinstance(f, dict)]
    for flow in flows:
        if flow.get("id") == ref:
            return flow
    named = [flow for flow in flows if flow.get("name") == ref]
    if len(named) == 1:
        return named[0]
    if named:
        ids = ", ".join(str(flow.get("id")) for flow in named)
        emit_error(
            f"Several flows are named {ref!r}: {ids}; pass the id instead",
            json_output=json_output,
            code="flows.invalid",
        )
        raise typer.Exit(2)
    emit_error(
        f"No flow with id or name {ref!r} (see agentos flows list)",
        json_output=json_output,
        code="flows.not_found",
    )
    raise typer.Exit(2)


def _parse_inputs(values: list[str] | None, *, json_output: bool) -> dict[str, str]:
    inputs: dict[str, str] = {}
    for raw in values or []:
        if not isinstance(raw, str):
            continue
        name, sep, value = raw.partition("=")
        if not sep or not name.strip():
            emit_error(
                f"--input expects name=value (got {raw!r})",
                json_output=json_output,
                code="flows.invalid",
            )
            raise typer.Exit(2)
        inputs[name.strip()] = value
    return inputs


# ── run summaries ─────────────────────────────────────────────────────────


def _node_types(run: dict[str, Any]) -> dict[str, str]:
    graph = run.get("graph") or {}
    return {str(n.get("id")): str(n.get("type")) for n in graph.get("nodes") or []}


def run_images(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Saved images with their files, else the generated ones (graph order)."""
    types = _node_types(run)
    outputs = run.get("outputs") or {}
    order = [str(n.get("id")) for n in (run.get("graph") or {}).get("nodes") or []]
    saved: list[dict[str, Any]] = []
    generated: list[dict[str, Any]] = []
    for node_id in order:
        out = outputs.get(node_id)
        if not isinstance(out, dict):
            continue
        refs = [ref for ref in out.get("images") or [] if isinstance(ref, dict)]
        files = [str(path) for path in out.get("files") or []]
        for index, ref in enumerate(refs):
            row = {
                "node_id": node_id,
                "sha256": ref.get("sha256"),
                "width": ref.get("width"),
                "height": ref.get("height"),
                "path": files[index] if index < len(files) else None,
            }
            if types.get(node_id) == "save":
                saved.append(row)
            elif types.get(node_id) == "image":
                generated.append(row)
    return saved or generated


def run_summary(run: dict[str, Any], flow_name: str | None = None) -> dict[str, Any]:
    states = [
        str(state.get("state"))
        for state in (run.get("nodes") or {}).values()
        if isinstance(state, dict)
    ]
    node_errors = {
        node_id: state.get("error")
        for node_id, state in (run.get("nodes") or {}).items()
        if isinstance(state, dict) and state.get("state") == "error"
    }
    return {
        "run_id": run.get("run_id"),
        "flow_id": run.get("flow_id"),
        "flow_name": flow_name,
        "status": run.get("status"),
        "origin": run.get("origin"),
        "images": run_images(run),
        "est_usd": run.get("est_usd"),
        "estimate": (run.get("estimate") or {}).get("total"),
        "cost_usd": run.get("cost_usd"),
        "cached": sum(1 for s in states if s == "cached"),
        "nodes": {"done": sum(1 for s in states if s in ("done", "cached")), "total": len(states)},
        "error": run.get("error"),
        "node_errors": node_errors,
        "created": run.get("created"),
        "ended": run.get("ended"),
    }


def _render_run(summary: dict[str, Any]) -> None:
    name = summary.get("flow_name") or summary.get("flow_id")
    nodes = summary.get("nodes") or {}
    typer.echo(
        f"{summary.get('run_id')}  {name}  {summary.get('status')}  "
        f"{nodes.get('done', 0)}/{nodes.get('total', 0)} nodes"
        + (f", {summary['cached']} cached" if summary.get("cached") else "")
    )
    estimate = summary.get("estimate")
    if not isinstance(estimate, dict) and isinstance(summary.get("est_usd"), (int, float)):
        estimate = {"kind": "usd", "usd": summary["est_usd"], "approximate": True}
    typer.echo(f"cost {_usd(summary.get('cost_usd'))} (estimate {price_text(estimate)})")
    images = summary.get("images") or []
    if images:
        typer.echo(f"{len(images)} image(s):")
        for image in images:
            size = f"{image.get('width')}x{image.get('height')}"
            where = image.get("path") or f"media {str(image.get('sha256'))[:12]} (not saved)"
            typer.echo(f"  {where}  {size}")
    error = summary.get("error")
    if isinstance(error, dict) and error:
        typer.secho(
            f"error: {error.get('message') or error.get('code')}", fg=typer.colors.RED, err=True
        )
    for node_id, node_error in (summary.get("node_errors") or {}).items():
        if isinstance(node_error, dict):
            typer.secho(
                f"  {node_id}: {node_error.get('message') or node_error.get('code')}",
                fg=typer.colors.RED,
                err=True,
            )


# ── commands ──────────────────────────────────────────────────────────────


@flows_app.command("list")
def flows_list(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List saved flows, newest first."""

    async def _run(client):
        return await _call(client, "flows.list", {}, json_output=json_output)

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    rows = [row for row in (payload or {}).get("flows") or [] if isinstance(row, dict)]
    if not rows:
        typer.echo("No flows yet. Build one in the desktop app (Flows).")
        return
    table = Table(title="Flows", show_header=True, header_style=ACCENT_HEADER)
    for column in ("ID", "Name", "Nodes", "Rev", "Updated"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            _cell(row.get("id")),
            _cell(row.get("name")),
            _cell(row.get("node_count")),
            _cell(row.get("rev")),
            _cell(row.get("updated")),
        )
    console.print(table)


@flows_app.command("show")
def flows_show(
    flow: str = typer.Argument(..., help="Flow id (flw_...) or exact name"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show one flow: its nodes, links and public inputs."""

    async def _run(client):
        summary = await _resolve(client, flow, json_output=json_output)
        got = await _call(client, "flows.get", {"id": summary["id"]}, json_output=json_output)
        return (got or {}).get("flow") or {}

    doc = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json({"flow": doc})
        return
    nodes = [n for n in doc.get("nodes") or [] if isinstance(n, dict)]
    typer.echo(f"{doc.get('name')}  {doc.get('id')}  rev {doc.get('rev')}  {doc.get('updated')}")
    typer.echo(f"{len(nodes)} node(s), {len(doc.get('links') or [])} link(s)")
    for node in nodes:
        title = f" ({node['title']})" if node.get("title") else ""
        values = node.get("values") or {}
        shown = ", ".join(
            f"{key}={str(value)[:40]!r}" if isinstance(value, str) else f"{key}={value}"
            for key, value in values.items()
            if not isinstance(value, (dict, list))
        )
        line = f"  {node.get('id')}  {node.get('type')}{title}"
        typer.echo(line + (f"  {shown}" if shown else ""))
    inputs = doc.get("inputs") or {}
    if inputs:
        typer.echo("public inputs (--input name=value):")
        for name, target in inputs.items():
            typer.echo(f"  {name} -> {target}")
    else:
        typer.echo("no public inputs")


@flows_app.command("nodes")
def flows_nodes(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List the node types a flow can use."""

    async def _run(client):
        return await _call(client, "flows.nodes", {}, json_output=json_output)

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    table = Table(title="Flow nodes", show_header=True, header_style=ACCENT_HEADER)
    for column in ("ID", "Group", "Inputs", "Outputs", "Paid"):
        table.add_column(column)
    for node in (payload or {}).get("nodes") or []:
        if not isinstance(node, dict):
            continue
        inputs = ", ".join(str(i.get("name")) for i in node.get("inputs") or [])
        outputs = ", ".join(f"{o.get('name')}:{o.get('type')}" for o in node.get("outputs") or [])
        table.add_row(
            _cell(node.get("id")),
            _cell(node.get("group")),
            _cell(inputs),
            _cell(outputs or "-"),
            _cell("yes" if node.get("price") else "no"),
        )
    console.print(table)


@flows_app.command("models")
def flows_models(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List image models, whether each has a key, and their prices."""

    async def _run(client):
        return await _call(client, "flows.models", {}, json_output=json_output)

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    default = (payload or {}).get("default")
    for model in (payload or {}).get("models") or []:
        if not isinstance(model, dict):
            continue
        price = model.get("price") or {}
        per_image = price.get("per_image") if isinstance(price, dict) else None
        price_text = _per_image_text(per_image, approximate=bool(price.get("approximate")))
        mark = " (default)" if model.get("id") == default else ""
        edit = ", edit" if (model.get("ops") or {}).get("edit") else ""
        typer.echo(f"{model.get('id')}{mark}  {model.get('display')}  {price_text}{edit}")
        if not model.get("available"):
            typer.echo(f"  unavailable: {model.get('unavailable_reason') or 'no API key'}")
    if not default:
        typer.secho(
            "No image model is available: set one of the API keys named above "
            "(agentos env set NAME).",
            fg=typer.colors.YELLOW,
            err=True,
        )


@flows_app.command("run")
def flows_run(
    flow: str = typer.Argument(..., help="Flow id (flw_...) or exact name"),
    input_values: list[str] | None = typer.Option(
        None,
        "--input",
        "-i",
        help="Override a public input: name=value (repeatable; see agentos flows show)",
    ),
    to_node: str | None = typer.Option(
        None, "--to", help="Run only up to this node id (and what it needs)"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    no_wait: bool = typer.Option(False, "--no-wait", help="Queue the run and return at once"),
    timeout: int = typer.Option(
        DEFAULT_TIMEOUT_SECONDS,
        "--timeout",
        min=1,
        max=7200,
        help="How long to wait for the run, in seconds (it keeps running after)",
    ),
) -> None:
    """Run a flow and wait for its images (prints status, files and cost)."""

    inputs = _parse_inputs(
        input_values if isinstance(input_values, list) else None, json_output=json_output
    )
    target = to_node if isinstance(to_node, str) and to_node.strip() else None
    started: dict[str, Any] = {}
    progress_tty = not json_output and sys.stderr.isatty()

    async def _run(client):
        summary = await _resolve(client, flow, json_output=json_output)
        started["flow"] = summary
        params: dict[str, Any] = {"flow_id": summary["id"], "origin": "cli"}
        if inputs:
            params["inputs"] = inputs
        if target:
            params["to_node"] = target
        queued = await _call(client, "flows.run", params, json_output=json_output)
        run_id = str((queued or {}).get("run_id") or "")
        started["run_id"] = run_id
        if no_wait:
            return {"queued": queued}
        deadline = time.monotonic() + timeout
        last_line = ""
        while True:
            got = await _call(client, "flows.run.get", {"run_id": run_id}, json_output=json_output)
            run = (got or {}).get("run") or {}
            if run.get("status") in TERMINAL_STATUSES:
                return {"run": run}
            if time.monotonic() >= deadline:
                return {"run": run, "timed_out": True}
            if progress_tty:
                info = run_summary(run)
                line = f"{run_id} {info['status']} {info['nodes']['done']}/{info['nodes']['total']}"
                if line != last_line:
                    typer.echo(line, err=True)
                    last_line = line
            await asyncio.sleep(POLL_SECONDS)

    try:
        result = run_gateway_sync(_run, json_output=json_output)
    except KeyboardInterrupt:
        run_id = started.get("run_id")
        if run_id:
            typer.echo(
                f"\nStopped waiting; {run_id} keeps running. "
                f"Stop it with: agentos flows stop {run_id}",
                err=True,
            )
        raise typer.Exit(130) from None

    flow_name = (started.get("flow") or {}).get("name")
    if "queued" in result:
        queued = dict(result["queued"] or {})
        queued.setdefault("flow_id", (started.get("flow") or {}).get("id"))
        queued["flow_name"] = flow_name
        if json_output:
            print_json(queued)
        else:
            typer.echo(f"{queued.get('run_id')} queued for {flow_name}")
            typer.echo(f"Follow it with: agentos flows runs {queued.get('flow_id')}")
        return
    summary = run_summary(result["run"], flow_name)
    timed_out = bool(result.get("timed_out"))
    if json_output:
        summary["timed_out"] = timed_out
        print_json(summary)
    else:
        _render_run(summary)
        if timed_out:
            typer.secho(
                f"Still {summary['status']} after {timeout}s; it keeps running. "
                f"Check later with: agentos flows runs {summary['flow_id']}",
                fg=typer.colors.YELLOW,
                err=True,
            )
    if timed_out or summary.get("status") != "done":
        raise typer.Exit(1)


@flows_app.command("runs")
def flows_runs(
    flow: str | None = typer.Argument(None, help="Only this flow (id or exact name)"),
    limit: int = typer.Option(30, "--limit", "-n", min=1, max=200, help="Maximum rows"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List recent runs, newest first."""
    ref = flow if isinstance(flow, str) and flow.strip() else None

    async def _run(client):
        listing = await _call(client, "flows.list", {}, json_output=json_output)
        names = {
            str(f.get("id")): f.get("name")
            for f in (listing or {}).get("flows") or []
            if isinstance(f, dict)
        }
        params: dict[str, Any] = {"limit": limit}
        if ref:
            params["flow_id"] = (await _resolve(client, ref, json_output=json_output))["id"]
        runs = await _call(client, "flows.runs", params, json_output=json_output)
        return {"runs": (runs or {}).get("runs") or [], "names": names}

    payload = run_gateway_sync(_run, json_output=json_output)
    rows = [row for row in payload["runs"] if isinstance(row, dict)]
    if json_output:
        print_json({"runs": rows})
        return
    if not rows:
        typer.echo("No runs yet.")
        return
    for row in rows:
        error = row.get("error")
        status = str(row.get("status") or "")
        if isinstance(error, dict) and error.get("code"):
            status += f" ({error.get('code')})"
        name = payload["names"].get(str(row.get("flow_id"))) or row.get("flow_id")
        typer.echo(
            f"{row.get('run_id')}  {name}  {status}  {row.get('origin')}  "
            f"{row.get('created')}  {row.get('image_count') or 0} image(s)  "
            f"{_usd(row.get('cost_usd'))}"
        )


@flows_app.command("stop")
def flows_stop(
    run_id: str = typer.Argument(..., help="Run id (run_...)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Stop a queued or running run (a finished run is left as it is)."""

    async def _run(client):
        return await _call(client, "flows.stop", {"run_id": run_id}, json_output=json_output)

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    typer.echo(f"Stopped {run_id}.")
