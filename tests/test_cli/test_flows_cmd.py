"""``agentos flows``: a thin client of the ``flows.*`` RPC (docs/flows.md).

The gateway is a canned fake: each test hands ``run_gateway_sync`` a client
whose ``call`` answers from a table and records what was asked.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from agentos.cli import flows_cmd
from agentos.cli.gateway_client import GatewayRPCError
from agentos.cli.main import app

FLOW = {"id": "flw_ab12cd34", "name": "Lighthouse", "rev": 3, "updated": "2026-10-29T10:00:00Z"}
GRAPH = {
    **FLOW,
    "nodes": [
        {"id": "n1", "type": "text", "values": {"text": "a lighthouse"}},
        {"id": "n2", "type": "image", "values": {"model": "auto", "count": 1}},
        {"id": "n3", "type": "save", "values": {}},
    ],
    "links": [{"id": "l1"}, {"id": "l2"}],
    "inputs": {"prompt": "n1.text", "count": "n2.count"},
}


def _run_record(status: str, **extra: Any) -> dict[str, Any]:
    done = status == "done"
    record = {
        "run_id": "run_0123456789ab",
        "flow_id": FLOW["id"],
        "status": status,
        "origin": "cli",
        "graph": GRAPH,
        "nodes": {
            "n1": {"state": "cached"},
            "n2": {"state": "done" if done else "running"},
            "n3": {"state": "done" if done else "queued"},
        },
        "outputs": (
            {
                "n2": {"images": [{"sha256": "a" * 64, "width": 1024, "height": 1024}]},
                "n3": {
                    "images": [{"sha256": "b" * 64, "width": 1024, "height": 1024}],
                    "files": ["/tmp/flows/outputs/flw_ab12cd34/lighthouse_run_1.png"],
                },
            }
            if done
            else {}
        ),
        "est_usd": 0.0672,
        "cost_usd": 0.05 if done else None,
        "error": None,
    }
    record.update(extra)
    return record


class FakeClient:
    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, dict(params or {})))
        reply = self.replies.get(method)
        if isinstance(reply, list):  # successive answers (polling)
            reply = reply.pop(0) if len(reply) > 1 else reply[0]
        if isinstance(reply, BaseException):
            raise reply
        return reply


@pytest.fixture
def gateway(monkeypatch):
    def install(**replies: Any) -> FakeClient:
        replies.setdefault("flows_list", {"flows": [FLOW]})
        client = FakeClient({key.replace("_", "."): value for key, value in replies.items()})

        def runner(fn, *, json_output: bool = False):  # noqa: ARG001
            return asyncio.run(fn(client))

        monkeypatch.setattr(flows_cmd, "run_gateway_sync", runner)
        monkeypatch.setattr(flows_cmd, "POLL_SECONDS", 0.0)
        return client

    return install


def _exit_code(fn, **kwargs: Any) -> int:
    try:
        fn(**kwargs)
    except typer.Exit as exc:
        return int(exc.exit_code)
    return 0


_RUN_DEFAULTS: dict[str, Any] = {
    "input_values": None,
    "to_node": None,
    "json_output": False,
    "no_wait": False,
    "timeout": 600,
}


def _run(**kwargs: Any) -> int:
    return _exit_code(flows_cmd.flows_run, **{**_RUN_DEFAULTS, **kwargs})


def test_flows_is_a_registered_command_group() -> None:
    result = CliRunner().invoke(app, ["flows", "--help"])

    assert result.exit_code == 0
    for command in ("list", "show", "nodes", "models", "run", "runs", "stop"):
        assert command in result.output


def test_list_human_and_json(gateway, capsys) -> None:
    gateway()
    assert _exit_code(flows_cmd.flows_list, json_output=False) == 0
    assert "Lighthouse" in capsys.readouterr().out

    gateway()
    assert _exit_code(flows_cmd.flows_list, json_output=True) == 0
    assert json.loads(capsys.readouterr().out) == {"flows": [FLOW]}


def test_show_resolves_a_name_and_lists_public_inputs(gateway, capsys) -> None:
    client = gateway(flows_get={"flow": GRAPH})

    assert _exit_code(flows_cmd.flows_show, flow="Lighthouse", json_output=False) == 0

    out = capsys.readouterr().out
    assert ("flows.get", {"id": FLOW["id"]}) in client.calls
    assert "prompt -> n1.text" in out and "n2  image" in out


def test_unknown_or_ambiguous_flow_exits_2(gateway, capsys) -> None:
    gateway()
    assert _exit_code(flows_cmd.flows_show, flow="Nope", json_output=True) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "flows.not_found"

    twin = {**FLOW, "id": "flw_ffff0000"}
    gateway(flows_list={"flows": [FLOW, twin]})
    assert _exit_code(flows_cmd.flows_show, flow="Lighthouse", json_output=False) == 2
    assert "Several flows are named" in capsys.readouterr().err


def test_run_waits_and_prints_files_cost_and_cache(gateway, capsys) -> None:
    client = gateway(
        flows_run={"run_id": "run_0123456789ab", "status": "queued"},
        flows_run_get=[{"run": _run_record("running")}, {"run": _run_record("done")}],
    )

    code = _run(flow="Lighthouse", input_values=["prompt=a red fox", "count=2"])

    assert code == 0
    method, params = client.calls[1]
    assert method == "flows.run"
    assert params == {
        "flow_id": FLOW["id"],
        "origin": "cli",
        "inputs": {"prompt": "a red fox", "count": "2"},
    }
    assert [m for m, _ in client.calls].count("flows.run.get") == 2
    out = capsys.readouterr().out
    assert "run_0123456789ab  Lighthouse  done  3/3 nodes, 1 cached" in out
    assert "cost $0.050 (estimate ~$0.067)" in out
    assert "/tmp/flows/outputs/flw_ab12cd34/lighthouse_run_1.png  1024x1024" in out


def test_run_json_is_a_summary_with_image_paths(gateway, capsys) -> None:
    gateway(
        flows_run={"run_id": "run_0123456789ab", "status": "queued"},
        flows_run_get=[{"run": _run_record("done")}],
    )

    assert _run(flow=FLOW["id"], to_node="n2", json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "done" and payload["timed_out"] is False
    assert payload["flow_name"] == "Lighthouse"
    assert payload["images"][0]["path"].endswith("lighthouse_run_1.png")
    assert payload["cached"] == 1 and payload["nodes"] == {"done": 3, "total": 3}
    assert payload["cost_usd"] == 0.05


def test_run_passes_to_node(gateway) -> None:
    client = gateway(
        flows_run={"run_id": "run_0123456789ab", "status": "queued"},
        flows_run_get=[{"run": _run_record("done")}],
    )
    _run(flow=FLOW["id"], to_node="n2")
    assert client.calls[1][1]["to_node"] == "n2"


def test_no_wait_returns_the_queued_run(gateway, capsys) -> None:
    client = gateway(flows_run={"run_id": "run_0123456789ab", "status": "queued"})

    assert _run(flow="Lighthouse", no_wait=True, json_output=True) == 0

    assert "flows.run.get" not in [m for m, _ in client.calls]
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == "run_0123456789ab" and payload["status"] == "queued"


def test_failed_run_exits_1_with_the_node_error(gateway, capsys) -> None:
    failed = _run_record(
        "failed",
        error={"code": "flows.node_failed", "message": "Image failed"},
        nodes={
            "n1": {"state": "done"},
            "n2": {
                "state": "error",
                "error": {"code": "moderation", "message": "OpenRouter refused the prompt"},
            },
            "n3": {"state": "skipped"},
        },
    )
    gateway(flows_run={"run_id": "run_0123456789ab"}, flows_run_get=[{"run": failed}])

    assert _run(flow="Lighthouse") == 1

    err = capsys.readouterr().err
    assert "Image failed" in err and "n2: OpenRouter refused the prompt" in err


def test_timeout_exits_1_and_says_it_keeps_running(gateway, capsys, monkeypatch) -> None:
    gateway(
        flows_run={"run_id": "run_0123456789ab"}, flows_run_get=[{"run": _run_record("running")}]
    )
    clock = itertools.count(0.0, 10.0)
    monkeypatch.setattr(flows_cmd.time, "monotonic", lambda: next(clock))

    assert _run(flow="Lighthouse", timeout=5) == 1

    assert "keeps running" in capsys.readouterr().err


def test_invalid_flow_exits_2_and_lists_node_errors(gateway, capsys) -> None:
    error = GatewayRPCError(
        "flows.run",
        code="flows.invalid",
        message="The flow has errors",
        data={
            "node_errors": {
                "n2": [{"code": "unavailable", "message": "OPENROUTER_API_KEY is not set"}]
            }
        },
    )
    gateway(flows_run=error)

    assert _run(flow="Lighthouse") == 2

    err = capsys.readouterr().err
    assert "The flow has errors" in err and "n2: OPENROUTER_API_KEY is not set" in err


def test_bad_input_syntax_exits_2_before_any_call(gateway, capsys) -> None:
    client = gateway()

    assert _run(flow="Lighthouse", input_values=["prompt"]) == 2

    assert client.calls == []
    assert "name=value" in capsys.readouterr().err


def test_runs_filters_by_flow_and_names_it(gateway, capsys) -> None:
    summary = {
        "run_id": "run_0123456789ab",
        "flow_id": FLOW["id"],
        "status": "failed",
        "origin": "agent",
        "created": "2026-10-29T10:01:00Z",
        "image_count": 0,
        "cost_usd": None,
        "error": {"code": "flows.interrupted"},
    }
    client = gateway(flows_runs={"runs": [summary]})

    assert _exit_code(flows_cmd.flows_runs, flow="Lighthouse", limit=5, json_output=False) == 0

    assert ("flows.runs", {"limit": 5, "flow_id": FLOW["id"]}) in client.calls
    out = capsys.readouterr().out
    assert "Lighthouse" in out and "flows.interrupted" in out


def test_stop(gateway, capsys) -> None:
    client = gateway(flows_stop={"ok": True})

    assert _exit_code(flows_cmd.flows_stop, run_id="run_0123456789ab", json_output=False) == 0

    assert client.calls == [("flows.stop", {"run_id": "run_0123456789ab"})]
    assert "Stopped run_0123456789ab" in capsys.readouterr().out


def test_models_without_a_key_warns(gateway, capsys) -> None:
    gateway(
        flows_models={
            "default": None,
            "models": [
                {
                    "id": "openrouter/nano-banana-2",
                    "display": "Nano Banana 2",
                    "available": False,
                    "unavailable_reason": "OPENROUTER_API_KEY is not set",
                    "ops": {"edit": True},
                    "price": {"per_image": {"1K": 0.0672}, "approximate": True},
                }
            ],
        }
    )

    assert _exit_code(flows_cmd.flows_models, json_output=False) == 0

    captured = capsys.readouterr()
    assert "OPENROUTER_API_KEY is not set" in captured.out
    assert "No image model is available" in captured.err


def test_estimates_print_like_the_desktop(gateway, capsys) -> None:
    estimate = {"total": {"kind": "range", "min": 0.0034, "max": 0.1, "approximate": True}}
    gateway(
        flows_run={"run_id": "run_0123456789ab"},
        flows_run_get=[{"run": _run_record("done", estimate=estimate, est_usd=0.1)}],
    )

    assert _run(flow="Lighthouse") == 0

    assert "(estimate ~$0.0034–$0.10)" in capsys.readouterr().out
    assert flows_cmd.price_text({"kind": "free", "approximate": False}) == "free"
    assert flows_cmd.price_text({"kind": "usd", "usd": 0.27, "approximate": True}) == "~$0.27"
    assert flows_cmd.price_text({"kind": "unknown", "approximate": True}) == "unknown"
    assert flows_cmd.price_text(None) == "unknown"
