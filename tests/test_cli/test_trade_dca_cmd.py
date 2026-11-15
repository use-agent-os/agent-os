"""``agentos trade dca …``: RPC params, ``--every`` parsing, the card file and its marker.

The card is written only with ``--json`` (the agent always passes it); a person
running the command gets a panel or a table and nothing in the working directory.
Payloads are the engine's fixtures under ``tests/fixtures/dca_cards/``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from agentos.cli import trade_cmd
from agentos.cli.output import emit_error
from agentos.tools.builtin.artifacts import INLINE_ARTIFACT_MARKER_RE, INLINE_ARTIFACT_MIME_PREFIX

runner = CliRunner()
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "dca_cards"
MID = "dca_1a2b3c4d"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, Any] = {}

    async def call(self, method: str, params: dict | None = None) -> Any:
        self.calls.append((method, dict(params or {})))
        if method in self.responses:
            answer = self.responses[method]
            return answer() if callable(answer) else answer
        if method == "trading.dca.list":
            return _fixture("mandates")
        if method == "trading.dca.create":
            return _fixture("mandate-awaiting")
        return _fixture("mandate-active")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Client:
    fake = _Client()
    monkeypatch.setattr(
        trade_cmd, "run_gateway_sync", lambda action, **kw: asyncio.run(action(fake))
    )
    monkeypatch.delenv("AGENTOS_SESSION_KEY", raising=False)
    monkeypatch.delenv("AGENTOS_AGENT", raising=False)
    monkeypatch.setenv("COLUMNS", "220")
    monkeypatch.setattr(trade_cmd.console, "_width", 220)
    monkeypatch.chdir(tmp_path)
    return fake


def _lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.strip()]


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("30m", 1_800),
        ("2h", 7_200),
        ("1d", 86_400),
        ("1w", 604_800),
        ("60", 60),
        ("3600", 3_600),
        ("90s", 90),
        ("1.5h", 5_400),
        (" 12H ", 43_200),
    ],
)
def test_parse_every(text: str, seconds: int) -> None:
    assert trade_cmd.parse_every(text) == seconds


@pytest.mark.parametrize("text", ["", "59", "30s", "0.5m", "1y", "daily", "-1d", "1d2h", "1.2s"])
def test_parse_every_refuses(text: str) -> None:
    with pytest.raises(ValueError):
        trade_cmd.parse_every(text)


CREATE_BASE = ["create", "WETH", "--usd", "10", "--every", "1d"]


@pytest.mark.parametrize(
    ("args", "method", "params"),
    [
        (
            [*CREATE_BASE, "--cap", "300"],
            "trading.dca.create",
            {
                "chainId": 8453,
                "token": "WETH",
                "usdPerRun": 10.0,
                "everySeconds": 86_400,
                "startNow": True,
                "initiator": "manual",
                "capUsd": 300.0,
            },
        ),
        (
            [
                "create",
                "0xabc",
                "--usd",
                "5",
                "--every",
                "2h",
                "--runs",
                "30",
                "--cap",
                "100",
                "--max-price",
                "3000",
                "--quote",
                "USDG",
                "--chain",
                "robinhood",
                "--wallet",
                "Main",
                "--slippage",
                "0.5",
                "--name",
                "Stack",
                "--start",
                "next",
            ],
            "trading.dca.create",
            {
                "chainId": 4663,
                "token": "0xabc",
                "usdPerRun": 5.0,
                "everySeconds": 7_200,
                "startNow": False,
                "initiator": "manual",
                "capUsd": 100.0,
                "runsMax": 30,
                "maxPriceUsd": 3000.0,
                "quote": "USDG",
                "wallet": "Main",
                "slippagePct": 0.5,
                "name": "Stack",
            },
        ),
        (["list"], "trading.dca.list", {}),
        (["list", "--all", "--wallet", "0x1"], "trading.dca.list", {"all": True, "wallet": "0x1"}),
        (["show", MID], "trading.dca.get", {"mandateId": MID}),
        (["approve", MID, "--yes"], "trading.dca.approve", {"mandateId": MID}),
        (["reject", MID], "trading.dca.reject", {"mandateId": MID}),
        (
            ["reject", MID, "--reason", "too much"],
            "trading.dca.reject",
            {"mandateId": MID, "reason": "too much"},
        ),
        (["pause", MID], "trading.dca.pause", {"mandateId": MID}),
        (["resume", MID], "trading.dca.resume", {"mandateId": MID}),
        (
            ["stop", MID, "--reason", "done"],
            "trading.dca.stop",
            {"mandateId": MID, "reason": "done"},
        ),
        (["run", MID], "trading.dca.run", {"mandateId": MID}),
        (
            ["update", MID, "--usd", "20", "--runs", "0", "--every", "12h", "--max-price", "0"],
            "trading.dca.update",
            {
                "mandateId": MID,
                "usdPerRun": 20.0,
                "runsMax": 0,
                "everySeconds": 43_200,
                "maxPriceUsd": 0.0,
            },
        ),
        (
            ["update", MID, "--cap", "500", "--name", "Stack ETH"],
            "trading.dca.update",
            {"mandateId": MID, "capUsd": 500.0, "name": "Stack ETH"},
        ),
    ],
)
def test_json_prints_payload_then_the_marker_last(
    client: _Client, args: list[str], method: str, params: dict[str, Any], tmp_path: Path
) -> None:
    result = runner.invoke(trade_cmd.app, ["dca", *args, "--json"])
    assert result.exit_code == 0, result.output
    assert client.calls == [(method, params)]
    lines = _lines(result.stdout)
    payload = json.loads(lines[0])
    marker = INLINE_ARTIFACT_MARKER_RE.search(lines[-1])
    assert marker is not None and lines[-1] == marker.group(0)
    assert marker.group("mime") == trade_cmd.DCA_MIME == "application/vnd.agentos.dca+json"
    assert marker.group("mime").startswith(INLINE_ARTIFACT_MIME_PREFIX)
    path = marker.group("path")
    assert not Path(path).is_absolute() and path.startswith("dca-cards/")
    written = tmp_path / path
    assert json.loads(written.read_text(encoding="utf-8")) == payload
    name = Path(path).name
    if payload["kind"] == "mandates":
        assert name.startswith("mandates-all-")  # the fixture's request is {all: true}
    else:
        assert name.startswith(f"mandate-{payload['mandate']['id']}-")
    assert name.endswith("Z.json")


def test_list_card_slug_is_live_without_all(client: _Client) -> None:
    live = _fixture("mandates-empty")
    live["request"] = {"kind": "list", "params": {"all": False}}
    client.responses["trading.dca.list"] = live
    result = runner.invoke(trade_cmd.app, ["dca", "list", "--json"])
    assert result.exit_code == 0, result.output
    assert _lines(result.stdout)[-1].startswith("publish_artifact path=dca-cards/mandates-live-")


def test_agent_shell_create_passes_initiator_and_session_key(
    client: _Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTOS_SESSION_KEY", "agent:trading:desk:1")
    result = runner.invoke(trade_cmd.app, ["dca", *CREATE_BASE, "--runs", "30", "--json"])
    assert result.exit_code == 0, result.output
    method, params = client.calls[0]
    assert params["initiator"] == "agent" and params["sessionKey"] == "agent:trading:desk:1"
    assert json.loads(_lines(result.stdout)[0])["mandate"]["status"] == "awaiting_approval"


def test_no_card_writes_nothing(client: _Client, tmp_path: Path) -> None:
    result = runner.invoke(trade_cmd.app, ["dca", "show", MID, "--json", "--no-card"])
    assert result.exit_code == 0, result.output
    assert "publish_artifact" not in result.stdout
    assert json.loads(result.stdout)["kind"] == "mandate"
    assert not (tmp_path / trade_cmd.DCA_CARD_DIR).exists()


@pytest.mark.parametrize(
    ("args", "fixture", "expect"),
    [
        (["show", MID], "mandate-active", ["DCA ETH · active", "$60.00 of $300.00", "recent runs"]),
        (["show", MID], "mandate-awaiting", ["awaiting_approval", "agentos trade dca approve"]),
        (["show", MID], "mandate-completed", ["completed", "cap reached"]),
        (["list"], "mandates", ["DCA · 3 mandates", "dca_9c0d1e2f", "acquired"]),
        (["list"], "mandates-empty", ["No DCA mandates yet."]),
    ],
)
def test_human_output_renders_and_writes_no_card(
    client: _Client, tmp_path: Path, args: list[str], fixture: str, expect: list[str]
) -> None:
    method = "trading.dca.list" if args[0] == "list" else "trading.dca.get"
    client.responses[method] = _fixture(fixture)
    result = runner.invoke(trade_cmd.app, ["dca", *args])
    assert result.exit_code == 0, result.output
    assert "publish_artifact" not in result.stdout
    assert not (tmp_path / trade_cmd.DCA_CARD_DIR).exists()
    for text in expect:
        assert text in result.stdout, (text, result.stdout)


def test_card_directory_keeps_only_the_newest_cards(client: _Client, tmp_path: Path) -> None:
    cards = tmp_path / trade_cmd.DCA_CARD_DIR
    cards.mkdir()
    for i in range(trade_cmd.DCA_CARDS_KEPT + 5):
        old = cards / f"mandate-dca_old{i:02d}-20260101T0000{i:02d}Z.json"
        old.write_text("{}", encoding="utf-8")
        os.utime(old, (1_700_000_000 + i, 1_700_000_000 + i))
    unrelated = cards / "notes.json"
    unrelated.write_text("{}", encoding="utf-8")
    os.utime(unrelated, (1, 1))
    result = runner.invoke(trade_cmd.app, ["dca", "show", MID, "--json"])
    assert result.exit_code == 0, result.output
    marker = INLINE_ARTIFACT_MARKER_RE.search(_lines(result.stdout)[-1])
    assert marker is not None
    kept = sorted(p.name for p in cards.iterdir() if p.name != "notes.json")
    assert len(kept) == trade_cmd.DCA_CARDS_KEPT
    assert Path(marker.group("path")).name in kept
    assert "mandate-dca_old00-20260101T000000Z.json" not in kept
    assert "mandate-dca_old24-20260101T000024Z.json" in kept
    assert unrelated.exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["create", "WETH", "--usd", "10", "--every", "1d"], "--cap, --runs or both"),
        (["create", "WETH", "--usd", "10", "--every", "30s", "--cap", "5"], "at least 60"),
        (["create", "WETH", "--usd", "10", "--every", "daily", "--cap", "5"], "not an interval"),
        (["create", "WETH", "--usd", "0", "--every", "1d", "--cap", "5"], "--usd must be above 0"),
        (
            ["create", "WETH", "--usd", "10", "--every", "1d", "--cap", "-1"],
            "--cap must be above 0",
        ),
        (
            ["create", "WETH", "--usd", "10", "--every", "1d", "--runs", "0"],
            "--runs must be at least 1",
        ),
        (
            ["create", "WETH", "--usd", "10", "--every", "1d", "--runs", "3", "--start", "later"],
            "--start must be now or next",
        ),
        (
            ["create", "WETH", "--usd", "10", "--every", "1d", "--runs", "3", "--chain", "eth"],
            "unknown chain 'eth'",
        ),
        (["create", "WETH", "--every", "1d", "--runs", "3"], "Missing option '--usd'"),
        (["create", "WETH", "--usd", "ten", "--every", "1d", "--runs", "3"], "--usd"),
        (["update", MID], "Nothing to update"),
        (["update", MID, "--every", "10s"], "at least 60"),
        (["update", MID, "--runs", "-1"], "--runs must be 0 or more"),
        (["update", MID, "--max-price", "-5"], "--max-price must be 0 or more"),
        (["update", MID, "--name", "  "], "--name must not be empty"),
        (["show", " "], "mandate id is required"),
        (["show"], "Missing argument"),
        (["list", "--bogus"], "No such option: --bogus"),
        (["run", MID, "--wait-seconds", "0"], "--wait-seconds"),
    ],
)
def test_usage_errors_under_json_are_json(client: _Client, args: list[str], message: str) -> None:
    result = runner.invoke(trade_cmd.app, ["dca", *args, "--json"])
    assert result.exit_code == 2, result.output
    assert client.calls == []
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "INVALID_ARGUMENT" and message in error["message"], error
    assert result.stdout == ""


def test_usage_error_without_json_keeps_the_usage_panel(client: _Client) -> None:
    result = runner.invoke(trade_cmd.app, ["dca", "show"])
    assert result.exit_code == 2
    assert "Usage:" in result.stderr and not result.stderr.lstrip().startswith("{")


@pytest.mark.parametrize(
    "code",
    [
        "trading.dca.invalid",
        "trading.dca.bad_state",
        "trading.dca.not_found",
        "trading.token_not_found",
        "trading.invalid",
    ],
)
def test_input_or_state_the_engine_refuses_exits_2(
    client: _Client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: str
) -> None:
    from agentos.cli.gateway_client import GatewayRPCError

    async def call(method: str, params: dict | None = None) -> Any:
        raise GatewayRPCError(method, code=code, message="cannot pause DCA mandate: it is stopped")

    monkeypatch.setattr(client, "call", call)
    result = runner.invoke(trade_cmd.app, ["dca", "pause", MID, "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == code
    assert result.stdout == ""
    assert not (tmp_path / trade_cmd.DCA_CARD_DIR).exists()


def test_gateway_error_is_json_exit_1_and_no_card(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def failing(action: Any, *, json_output: bool = False, **kw: Any) -> Any:
        emit_error(
            "approve is the user's action",
            json_output=json_output,
            code="trading.operator_required",
        )
        raise typer.Exit(1)

    monkeypatch.setattr(trade_cmd, "run_gateway_sync", failing)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(trade_cmd.app, ["dca", "approve", MID, "--yes", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["code"] == "trading.operator_required"
    assert "publish_artifact" not in result.stdout
    assert not (tmp_path / trade_cmd.DCA_CARD_DIR).exists()


def _run_payload(status: str, order_id: str | None = "ord_00000000000b") -> dict[str, Any]:
    payload = _fixture("mandate-active")
    payload["run"] = {
        "n": 9,
        "at": "2026-09-27T10:00:00Z",
        "manual": True,
        "status": status,
        "reasonCode": None,
        "reason": None,
        "usd": 10.0,
        "amount": None,
        "priceUsd": 2350.0,
        "orderId": order_id,
        "txHash": None,
        "explorerUrl": None,
        "gasUsd": None,
    }
    return payload


def test_run_wait_waits_for_the_order_then_refetches(client: _Client) -> None:
    fresh = _fixture("mandate-active")
    settled = {**_run_payload("pending")["run"], "status": "filled", "txHash": "0xfeed"}
    fresh["mandate"]["history"].insert(0, settled)
    client.responses["trading.dca.run"] = _run_payload("pending")
    client.responses["trading.orders.wait"] = {"order": {"status": "confirmed"}}
    client.responses["trading.dca.get"] = fresh
    result = runner.invoke(
        trade_cmd.app, ["dca", "run", MID, "--wait", "--wait-seconds", "30", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert client.calls == [
        ("trading.dca.run", {"mandateId": MID}),
        ("trading.orders.wait", {"orderId": "ord_00000000000b", "timeoutSeconds": 30}),
        ("trading.dca.get", {"mandateId": MID}),
    ]
    payload = json.loads(_lines(result.stdout)[0])
    assert payload["run"]["status"] == "filled" and payload["run"]["txHash"] == "0xfeed"
    assert _lines(result.stdout)[-1].startswith(f"publish_artifact path=dca-cards/mandate-{MID}-")


@pytest.mark.parametrize(
    ("status", "order_id", "wait"),
    [("skipped", None, True), ("filled", "ord_1", True), ("pending", "ord_1", False)],
)
def test_run_does_not_wait_when_there_is_nothing_to_wait_for(
    client: _Client, status: str, order_id: str | None, wait: bool
) -> None:
    client.responses["trading.dca.run"] = _run_payload(status, order_id)
    args = ["dca", "run", MID, "--json", *(["--wait"] if wait else [])]
    result = runner.invoke(trade_cmd.app, args)
    assert result.exit_code == 0, result.output
    assert [m for m, _ in client.calls] == ["trading.dca.run"]
    assert json.loads(_lines(result.stdout)[0])["run"]["status"] == status


def test_run_human_output_shows_the_run(client: _Client) -> None:
    client.responses["trading.dca.run"] = _run_payload("parked")
    result = runner.invoke(trade_cmd.app, ["dca", "run", MID])
    assert result.exit_code == 0, result.output
    assert "Run: #9" in result.stdout and "awaiting approval" in result.stdout


@pytest.fixture
def utc_clock(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Pin the run lines' zone to UTC so ``HH:MM`` is deterministic on every OS."""
    from datetime import UTC

    monkeypatch.setattr(trade_cmd, "_DCA_LOCAL_TZ", UTC)
    yield


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, "—"),
        (-0.004, "$0.00"),
        (0.004, "$0.00"),
        (-0.0, "$0.00"),
        (0.0, "$0.00"),
        (5.44526, "$5.45"),
        (-1.206, "-$1.21"),
        (-0.005001, "-$0.01"),
    ],
)
def test_signed_usd_never_prints_minus_zero(value: Any, text: str) -> None:
    assert trade_cmd._signed_usd(value) == text


def test_run_lines_by_status(utc_clock: Any) -> None:
    from datetime import UTC, datetime

    history = _fixture("mandate-active")["mandate"]["history"]
    by_n = {run["n"]: run for run in history}
    now = datetime(2026, 9, 25, 18, 0, tzinfo=UTC)
    line = trade_cmd._dca_run_line
    assert line(by_n[6], "WETH", now) == (
        "#6 · 09:09 · $10.00 → 0.00434783 WETH @ $2,300.00 · tx 0x0000…ed09"
    )
    assert line(by_n[5], "WETH", now).startswith("#5 · Sep 24 09:08 · $10.00 → ")
    assert line(by_n[7], "WETH", now) == (
        "#7 · Sep 26 09:10 · skipped · WETH at $2,700 above $2,600"
    )
    assert line(by_n[8], "WETH", now) == (
        "#8 · Sep 27 09:01 · $10.00 awaiting approval · ord_00000000000a"
    )
    failed = {**by_n[7], "status": "failed", "reason": "quote expired"}
    assert line(failed, "WETH", now).endswith(" · failed · quote expired")
    pending = {**by_n[8], "status": "pending", "manual": True}
    assert line(pending, "WETH", now) == (
        "#8 · Sep 27 09:01 · buy now · $10.00 pending, waiting to fill · ord_00000000000a"
    )


def test_show_panel_gas_and_unrealised(client: _Client, utc_clock: Any) -> None:
    payload = _fixture("mandate-active")
    payload["mandate"]["acquired"].update(unrealizedUsd=-0.0031, gasUsd=0.0059)
    client.responses["trading.dca.get"] = payload
    result = runner.invoke(trade_cmd.app, ["dca", "show", MID])
    assert result.exit_code == 0, result.output
    assert "unrealised $0.00 · gas $0.0059" in result.stdout
    assert "-$0.00" not in result.stdout
    assert "Sep 26 09:10 · skipped · WETH at $2,700 above $2,600" in result.stdout
    assert "tx 0x0000…ed09" in result.stdout

    payload["mandate"]["acquired"].update(unrealizedUsd=None, gasUsd=0.13)
    result = runner.invoke(trade_cmd.app, ["dca", "show", MID])
    assert "unrealised — · gas $0.13" in result.stdout


def test_list_table_leads_with_the_name(client: _Client) -> None:
    result = runner.invoke(trade_cmd.app, ["dca", "list"])
    assert result.exit_code == 0, result.output
    header = next(line for line in result.stdout.splitlines() if "Pair" in line)
    columns = ["Name", "Pair", "Cadence", "Status", "Progress", "Buys", "Next buy"]
    positions = [header.index(column) for column in columns]
    assert positions == sorted(positions), header
    assert "Mandate" not in header
    row = next(line for line in result.stdout.splitlines() if "Weekly ETH" in line)
    assert "$150.00 every week" in row and "$0.00 / $1,500.00" in row
    assert "dca_9c0d1e2f" in result.stdout  # the id rides under the name
    active = next(line for line in result.stdout.splitlines() if "DCA ETH" in line)
    assert "$60.00 / $300.00" in active


def test_approve_shows_the_mandate_and_asks_first(client: _Client) -> None:
    """WAL-08: an automation approval is never a bare id sent blind."""
    client.responses["trading.dca.get"] = _fixture("mandate-awaiting")
    declined = runner.invoke(trade_cmd.app, ["dca", "approve", MID], input="")
    assert declined.exit_code != 0
    assert [m for m, _ in client.calls] == ["trading.dca.get"]
    out = declined.output
    assert "0x4200000000000000000000000000000000000006" in out and "verified" in out
    assert "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913" in out
    assert "0xdB00079cad3e665853Bf766eFe26F4C38cdbdCDA" in out
    assert f"Approve DCA mandate {MID}?" in out
    approved = runner.invoke(trade_cmd.app, ["dca", "approve", MID], input="y\n")
    assert approved.exit_code == 0, approved.output
    assert [m for m, _ in client.calls][-1] == "trading.dca.approve"
