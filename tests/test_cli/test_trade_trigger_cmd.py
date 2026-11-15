"""``agentos trade trigger …``: flag rules, RPC params, the card file and its marker.

The condition and action flags are checked in the CLI before any RPC (exit 2,
JSON on stderr under ``--json``). The card is written only with ``--json``; a
person gets a panel or a table and nothing in the working directory. Payloads
are the engine's fixtures under ``tests/fixtures/trigger_cards/``.
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
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "trigger_cards"
TID = "trg_1a2b3c4d"


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
        if method == "trading.trigger.list":
            return _fixture("triggers")
        if method == "trading.trigger.create":
            return _fixture("trigger-awaiting")
        return _fixture("trigger-armed")


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
    ("text", "flag", "price"),
    [
        ("3800", "--below", "3800"),
        ("$3,800", "--below", "3800"),
        ("0.998", "--above", "0.998"),
        ("-10%", "--below", "-10%"),
        ("10%", "--below", "-10%"),
        ("2.5%", "--below", "-2.5%"),
        ("+15%", "--above", "+15%"),
        ("15%", "--above", "+15%"),
        (" 20 % ", "--above", "+20%"),
    ],
)
def test_parse_trigger_price(text: str, flag: str, price: str) -> None:
    assert trade_cmd.parse_trigger_price(text, flag) == price


@pytest.mark.parametrize(
    ("text", "flag", "message"),
    [
        ("", "--below", "is not a price"),
        ("cheap", "--below", "is not a price"),
        ("-3800", "--below", "takes no sign"),
        ("0", "--above", "must be above 0"),
        ("0%", "--below", "must be above 0"),
        ("+10%", "--below", "use --above +10%"),
        ("-10%", "--above", "use --below -10%"),
        ("100%", "--below", "100 % or more"),
        ("10%%", "--below", "is not a price"),
    ],
)
def test_parse_trigger_price_refuses(text: str, flag: str, message: str) -> None:
    with pytest.raises(ValueError, match=message.replace("+", r"\+")):
        trade_cmd.parse_trigger_price(text, flag)


STOP_LOSS = ["create", "ETH", "--sell", "--pct", "50", "--below", "3800"]


@pytest.mark.parametrize(
    ("args", "method", "params"),
    [
        (
            STOP_LOSS,
            "trading.trigger.create",
            {
                "chainId": 8453,
                "kind": "sell",
                "token": "ETH",
                "direction": "below",
                "price": "3800",
                "amountPct": 50.0,
                "initiator": "manual",
            },
        ),
        (
            # A value starting with "-" is the option's value, not a flag.
            ["create", "ETH", "--sell", "--pct", "100", "--below", "-10%"],
            "trading.trigger.create",
            {
                "chainId": 8453,
                "kind": "sell",
                "token": "ETH",
                "direction": "below",
                "price": "-10%",
                "amountPct": 100.0,
                "initiator": "manual",
            },
        ),
        (
            ["create", "ETH", "--sell", "--amount", "0.05", "--above=+20%", "--quote", "USDC"],
            "trading.trigger.create",
            {
                "chainId": 8453,
                "kind": "sell",
                "token": "ETH",
                "direction": "above",
                "price": "+20%",
                "amount": "0.05",
                "quote": "USDC",
                "initiator": "manual",
            },
        ),
        (
            ["create", "ETH", "--sell", "--usd", "100", "--trail", "10", "--slippage", "1"],
            "trading.trigger.create",
            {
                "chainId": 8453,
                "kind": "sell",
                "token": "ETH",
                "direction": "trail",
                "trailPct": 10.0,
                "amountUsd": 100.0,
                "slippagePct": 1.0,
                "initiator": "manual",
            },
        ),
        (
            [
                "create",
                "0xabc",
                "--buy",
                "--usd",
                "50",
                "--below",
                "3500",
                "--chain",
                "robinhood",
                "--quote",
                "USDG",
                "--wallet",
                "Main",
                "--name",
                "Buy the dip",
                "--for",
                "7d",
            ],
            "trading.trigger.create",
            {
                "chainId": 4663,
                "kind": "buy",
                "token": "0xabc",
                "direction": "below",
                "price": "3500",
                "amountUsd": 50.0,
                "quote": "USDG",
                "wallet": "Main",
                "name": "Buy the dip",
                "validForSeconds": 604_800,
                "initiator": "manual",
            },
        ),
        (
            ["create", "ETH", "--alert", "--above", "5000", "--for", "30m"],
            "trading.trigger.create",
            {
                "chainId": 8453,
                "kind": "alert",
                "token": "ETH",
                "direction": "above",
                "price": "5000",
                "validForSeconds": 1_800,
                "initiator": "manual",
            },
        ),
        (["list"], "trading.trigger.list", {}),
        (
            ["list", "--all", "--wallet", "0x1"],
            "trading.trigger.list",
            {"all": True, "wallet": "0x1"},
        ),
        (["show", TID], "trading.trigger.get", {"triggerId": TID}),
        (["approve", TID, "--yes"], "trading.trigger.approve", {"triggerId": TID}),
        (["reject", TID], "trading.trigger.reject", {"triggerId": TID}),
        (
            ["reject", TID, "--reason", "too tight"],
            "trading.trigger.reject",
            {"triggerId": TID, "reason": "too tight"},
        ),
        (["pause", TID], "trading.trigger.pause", {"triggerId": TID}),
        (["resume", TID], "trading.trigger.resume", {"triggerId": TID}),
        (
            ["stop", TID, "--reason", "changed my mind"],
            "trading.trigger.stop",
            {"triggerId": TID, "reason": "changed my mind"},
        ),
        (["fire", TID], "trading.trigger.fire", {"triggerId": TID}),
    ],
)
def test_json_prints_payload_then_the_marker_last(
    client: _Client, args: list[str], method: str, params: dict[str, Any], tmp_path: Path
) -> None:
    result = runner.invoke(trade_cmd.app, ["trigger", *args, "--json"])
    assert result.exit_code == 0, result.output
    assert client.calls == [(method, params)]
    lines = _lines(result.stdout)
    payload = json.loads(lines[0])
    marker = INLINE_ARTIFACT_MARKER_RE.search(lines[-1])
    assert marker is not None and lines[-1] == marker.group(0)
    assert marker.group("mime") == trade_cmd.TRIGGER_MIME == "application/vnd.agentos.trigger+json"
    assert marker.group("mime").startswith(INLINE_ARTIFACT_MIME_PREFIX)
    path = marker.group("path")
    assert not Path(path).is_absolute() and path.startswith("trigger-cards/")
    written = tmp_path / path
    assert json.loads(written.read_text(encoding="utf-8")) == payload
    name = Path(path).name
    if payload["kind"] == "triggers":
        all_ = payload["request"]["params"].get("all")
        assert name.startswith("triggers-all-" if all_ else "triggers-live-")
    else:
        assert name.startswith(f"trigger-{payload['trigger']['id']}-")
    assert name.endswith("Z.json")


@pytest.mark.parametrize(("all_", "slug"), [(True, "all"), (False, "live")])
def test_list_card_slug(client: _Client, all_: bool, slug: str) -> None:
    payload = _fixture("triggers-empty")
    payload["request"] = {"kind": "list", "params": {"all": all_}}
    client.responses["trading.trigger.list"] = payload
    result = runner.invoke(trade_cmd.app, ["trigger", "list", "--json"])
    assert result.exit_code == 0, result.output
    marker = _lines(result.stdout)[-1]
    assert marker.startswith(f"publish_artifact path=trigger-cards/triggers-{slug}-")


def test_agent_shell_create_passes_initiator_and_session_key(
    client: _Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTOS_SESSION_KEY", "agent:trading:desk:1")
    result = runner.invoke(trade_cmd.app, ["trigger", *STOP_LOSS, "--json"])
    assert result.exit_code == 0, result.output
    _, params = client.calls[0]
    assert params["initiator"] == "agent" and params["sessionKey"] == "agent:trading:desk:1"
    assert json.loads(_lines(result.stdout)[0])["trigger"]["status"] == "awaiting_approval"


def test_no_card_writes_nothing(client: _Client, tmp_path: Path) -> None:
    result = runner.invoke(trade_cmd.app, ["trigger", "show", TID, "--json", "--no-card"])
    assert result.exit_code == 0, result.output
    assert "publish_artifact" not in result.stdout
    assert json.loads(result.stdout)["kind"] == "trigger"
    assert not (tmp_path / trade_cmd.TRIGGER_CARD_DIR).exists()


def test_card_directory_keeps_only_the_newest_cards(client: _Client, tmp_path: Path) -> None:
    cards = tmp_path / trade_cmd.TRIGGER_CARD_DIR
    cards.mkdir()
    for i in range(trade_cmd.TRIGGER_CARDS_KEPT + 5):
        old = cards / f"trigger-trg_old{i:02d}-20260101T0000{i:02d}Z.json"
        old.write_text("{}", encoding="utf-8")
        os.utime(old, (1_700_000_000 + i, 1_700_000_000 + i))
    unrelated = cards / "notes.json"
    unrelated.write_text("{}", encoding="utf-8")
    os.utime(unrelated, (1, 1))
    result = runner.invoke(trade_cmd.app, ["trigger", "show", TID, "--json"])
    assert result.exit_code == 0, result.output
    marker = INLINE_ARTIFACT_MARKER_RE.search(_lines(result.stdout)[-1])
    assert marker is not None
    kept = sorted(p.name for p in cards.iterdir() if p.name != "notes.json")
    assert len(kept) == trade_cmd.TRIGGER_CARDS_KEPT == 20
    assert Path(marker.group("path")).name in kept
    assert "trigger-trg_old00-20260101T000000Z.json" not in kept
    assert "trigger-trg_old24-20260101T000024Z.json" in kept
    assert unrelated.exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        # Exactly one condition.
        (["create", "ETH", "--sell", "--pct", "50"], "exactly one condition"),
        (
            ["create", "ETH", "--sell", "--pct", "50", "--below", "3800", "--trail", "10"],
            "(got --below, --trail)",
        ),
        (
            ["create", "ETH", "--alert", "--below", "3800", "--above", "5000"],
            "(got --below, --above)",
        ),
        # Exactly one action.
        (["create", "ETH", "--below", "3800"], "exactly one action"),
        (
            ["create", "ETH", "--sell", "--alert", "--below", "3800", "--pct", "5"],
            "(got --sell, --alert)",
        ),
        (["create", "ETH", "--sell", "--buy", "--usd", "5", "--below", "3800"], "(got --sell"),
        # Size rules.
        (["create", "ETH", "--sell", "--below", "3800"], "--sell takes exactly one size"),
        (
            ["create", "ETH", "--sell", "--pct", "5", "--usd", "5", "--below", "3800"],
            "(got --pct, --usd)",
        ),
        (["create", "ETH", "--buy", "--below", "3500"], "--buy takes --usd"),
        (["create", "ETH", "--buy", "--pct", "50", "--below", "3500"], "--buy takes --usd"),
        (
            ["create", "ETH", "--buy", "--usd", "5", "--amount", "1", "--below", "3500"],
            "--buy takes --usd",
        ),
        (["create", "ETH", "--alert", "--usd", "5", "--above", "5000"], "--alert takes no size"),
        (["create", "ETH", "--buy", "--usd", "50", "--trail", "10"], "not --buy"),
        # Values.
        (["create", "ETH", "--alert", "--below", "cheap"], "is not a price"),
        (["create", "ETH", "--alert", "--below", "+10%"], "use --above"),
        (["create", "ETH", "--alert", "--trail", "0"], "--trail must be above 0"),
        (["create", "ETH", "--alert", "--trail", "100"], "--trail must be above 0"),
        (["create", "ETH", "--sell", "--pct", "0", "--below", "1"], "--pct must be above 0"),
        (["create", "ETH", "--sell", "--pct", "101", "--below", "1"], "at most 100"),
        (["create", "ETH", "--sell", "--amount", "lots", "--below", "1"], "--amount 'lots'"),
        (["create", "ETH", "--sell", "--amount", "0", "--below", "1"], "--amount '0'"),
        (["create", "ETH", "--buy", "--usd", "0", "--below", "1"], "--usd must be above 0"),
        (["create", "ETH", "--alert", "--above", "5", "--for", "30s"], "--for must be at least"),
        (["create", "ETH", "--alert", "--above", "5", "--for", "soon"], "--for 'soon'"),
        (["create", "ETH", "--alert", "--above", "5", "--chain", "eth"], "unknown chain 'eth'"),
        (["create", "ETH", "--alert", "--trail", "ten"], "--trail"),
        (["show", " "], "trigger id is required"),
        (["show"], "Missing argument"),
        (["list", "--bogus"], "No such option: --bogus"),
        (["fire", TID, "--wait-seconds", "0"], "--wait-seconds"),
    ],
)
def test_usage_errors_under_json_are_json(client: _Client, args: list[str], message: str) -> None:
    result = runner.invoke(trade_cmd.app, ["trigger", *args, "--json"])
    assert result.exit_code == 2, result.output
    assert client.calls == []
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "INVALID_ARGUMENT" and message in error["message"], error
    assert result.stdout == ""


def test_usage_error_without_json_is_still_exit_2(client: _Client) -> None:
    result = runner.invoke(trade_cmd.app, ["trigger", "create", "ETH", "--below", "3800"])
    assert result.exit_code == 2
    assert "exactly one action" in result.stderr
    assert client.calls == []
    result = runner.invoke(trade_cmd.app, ["trigger", "show"])
    assert result.exit_code == 2
    assert "Usage:" in result.stderr and not result.stderr.lstrip().startswith("{")


@pytest.mark.parametrize(
    "code",
    [
        "trading.trigger.invalid",
        "trading.trigger.bad_state",
        "trading.trigger.not_found",
        "trading.token_not_found",
        "trading.invalid",
    ],
)
def test_input_or_state_the_engine_refuses_exits_2(
    client: _Client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: str
) -> None:
    from agentos.cli.gateway_client import GatewayRPCError

    async def call(method: str, params: dict | None = None) -> Any:
        raise GatewayRPCError(method, code=code, message="cannot pause trigger: it is done")

    monkeypatch.setattr(client, "call", call)
    result = runner.invoke(trade_cmd.app, ["trigger", "pause", TID, "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == code
    assert result.stdout == ""
    assert not (tmp_path / trade_cmd.TRIGGER_CARD_DIR).exists()


def test_usage_codes_are_the_contracts() -> None:
    assert trade_cmd._TRIGGER_USAGE_CODES == {
        "trading.trigger.invalid",
        "trading.trigger.bad_state",
        "trading.trigger.not_found",
        "trading.invalid",
        "trading.token_not_found",
    }


def test_operator_required_is_json_exit_1_and_no_card(
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
    result = runner.invoke(trade_cmd.app, ["trigger", "approve", TID, "--yes", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["code"] == "trading.operator_required"
    assert "publish_artifact" not in result.stdout
    assert not (tmp_path / trade_cmd.TRIGGER_CARD_DIR).exists()


def _fire_payload(status: str, order_id: str | None = "ord_00000000000d") -> dict[str, Any]:
    payload = _fixture("trigger-armed")
    payload["fire"] = {
        "n": 7,
        "at": "2026-10-04T09:20:00Z",
        "manual": True,
        "status": status,
        "reasonCode": None,
        "reason": None,
        "priceUsd": 3790.0,
        "orderId": order_id,
        "txHash": None,
        "explorerUrl": None,
    }
    return payload


def test_fire_wait_waits_for_the_order_then_refetches(client: _Client) -> None:
    fresh = _fixture("trigger-armed")
    settled = {**_fire_payload("pending")["fire"], "status": "filled", "txHash": "0xfeed"}
    fresh["trigger"]["fires"] = [settled, *fresh["trigger"].get("fires", [])]
    client.responses["trading.trigger.fire"] = _fire_payload("pending")
    client.responses["trading.orders.wait"] = {"order": {"status": "confirmed"}}
    client.responses["trading.trigger.get"] = fresh
    result = runner.invoke(
        trade_cmd.app, ["trigger", "fire", TID, "--wait", "--wait-seconds", "30", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert client.calls == [
        ("trading.trigger.fire", {"triggerId": TID}),
        ("trading.orders.wait", {"orderId": "ord_00000000000d", "timeoutSeconds": 30}),
        ("trading.trigger.get", {"triggerId": TID}),
    ]
    payload = json.loads(_lines(result.stdout)[0])
    assert payload["fire"]["status"] == "filled" and payload["fire"]["txHash"] == "0xfeed"
    assert _lines(result.stdout)[-1].startswith("publish_artifact path=trigger-cards/trigger-")


@pytest.mark.parametrize(
    ("status", "order_id", "wait"),
    [("alerted", None, True), ("filled", "ord_1", True), ("pending", "ord_1", False)],
)
def test_fire_does_not_wait_when_there_is_nothing_to_wait_for(
    client: _Client, status: str, order_id: str | None, wait: bool
) -> None:
    client.responses["trading.trigger.fire"] = _fire_payload(status, order_id)
    args = ["trigger", "fire", TID, "--json", *(["--wait"] if wait else [])]
    result = runner.invoke(trade_cmd.app, args)
    assert result.exit_code == 0, result.output
    assert [m for m, _ in client.calls] == ["trading.trigger.fire"]
    assert json.loads(_lines(result.stdout)[0])["fire"]["status"] == status


def test_fire_human_output_shows_the_fire(client: _Client) -> None:
    client.responses["trading.trigger.fire"] = _fire_payload("parked")
    result = runner.invoke(trade_cmd.app, ["trigger", "fire", TID])
    assert result.exit_code == 0, result.output
    assert "Fire: #7" in result.stdout and "fire now" in result.stdout
    assert "awaiting approval" in result.stdout


@pytest.mark.parametrize(
    "fixture", ["trigger-armed", "trigger-awaiting", "trigger-done", "trigger-alert"]
)
def test_show_panel_renders_and_writes_no_card(
    client: _Client, tmp_path: Path, fixture: str
) -> None:
    payload = _fixture(fixture)
    trigger = payload["trigger"]
    client.responses["trading.trigger.get"] = payload
    result = runner.invoke(trade_cmd.app, ["trigger", "show", trigger["id"]])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "publish_artifact" not in out
    assert not (tmp_path / trade_cmd.TRIGGER_CARD_DIR).exists()
    glyph = trade_cmd._TRIGGER_GLYPHS[trigger["kind"]]
    assert f"{glyph} {trigger['name']} · {trigger['status']}" in out, out
    assert trigger["condition"]["label"] in out
    assert trigger["action"]["label"] in out
    condition = trigger["condition"]
    assert f"checks {condition['hits']}/{condition['confirmTicks']}" in out
    assert "valid until" in out
    if trigger["status"] == "awaiting_approval":
        assert f"agentos trade trigger approve {trigger['id']}" in out
    if trigger["kind"] == "alert":
        assert "approval" not in out.replace("awaiting_approval", "")
    if trigger.get("result"):
        assert "result " in out
    if trigger["fires"]:
        assert "recent fires" in out and "#1 · " in out


def test_panel_distance_and_trail(client: _Client) -> None:
    payload = _fixture("trigger-armed")
    trigger = payload["trigger"]
    trigger["market"]["distancePct"] = -2.1
    trigger["condition"].update(
        direction="trail", trailPct=10.0, peakPriceUsd=4200.0, stopPriceUsd=3780.0
    )
    client.responses["trading.trigger.get"] = payload
    result = runner.invoke(trade_cmd.app, ["trigger", "show", TID])
    assert result.exit_code == 0, result.output
    assert "-2.10% to fall" in result.stdout
    assert "peak $4,200.00 · stop $3,780.00" in result.stdout


def test_panel_amounts_are_never_scientific(client: _Client) -> None:
    payload = _fixture("trigger-done")
    trigger = payload["trigger"]
    trigger["market"]["balance"] = {"raw": "95367200000000", "human": "0.0000953672", "usd": 0.2}
    trigger["result"]["amountIn"]["human"] = "0.00001234567"
    client.responses["trading.trigger.get"] = payload
    result = runner.invoke(trade_cmd.app, ["trigger", "show", TID])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "balance 0.0000953672 WETH" in out, out
    assert "0.0000123457 WETH →" in out, out
    assert "e-0" not in out


@pytest.mark.parametrize(
    ("human", "text"),
    [
        ("9.53672e-05", "0.0000953672 ETH"),
        ("0.0000953672312", "0.0000953672 ETH"),
        ("37.6", "37.6 ETH"),
        ("1234567.891", "1234570 ETH"),
        ("0", "0 ETH"),
        ("", "—"),
        ("n/a", "n/a ETH"),
    ],
)
def test_amount_text_is_plain_decimal(human: str, text: str) -> None:
    assert trade_cmd._dca_amount({"human": human}, "ETH") == text


def test_list_table(client: _Client) -> None:
    payload = _fixture("triggers")
    client.responses["trading.trigger.list"] = payload
    result = runner.invoke(trade_cmd.app, ["trigger", "list"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert f"Triggers · {len(payload['triggers'])}" in out
    header = next(line for line in out.splitlines() if "Condition" in line)
    columns = ["Name", "Kind", "Status", "Condition", "Price now", "Distance", "Wallet"]
    positions = [header.index(column) for column in columns]
    assert positions == sorted(positions), header
    for trigger in payload["triggers"]:
        assert trigger["id"] in out  # the id rides under the name
        assert trigger["status"] in out
    assert " armed · " in out and " awaiting · " in out
    assert "publish_artifact" not in out


def test_list_empty(client: _Client) -> None:
    client.responses["trading.trigger.list"] = _fixture("triggers-empty")
    result = runner.invoke(trade_cmd.app, ["trigger", "list"])
    assert result.exit_code == 0, result.output
    assert "No triggers yet." in result.stdout
