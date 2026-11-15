"""``agentos trade protect`` and ``agentos trade bracket …``: flag rules, params, cards.

The flag rules (``--tp`` required, exactly one of ``--sl`` / ``--trail``, one
size at most, ``--tp-pct`` only with ``--pct``, no size on ``--alert``) are
checked in the CLI before any RPC (exit 2, JSON on stderr under ``--json``).
Cards share ``trigger-cards/`` and the trigger mime with the prefixes
``bracket-`` / ``brackets-``. Payloads are built here from the trigger
fixtures (the two legs are trigger objects) in the shape of docs/brackets.md.
"""

from __future__ import annotations

import asyncio
import copy
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
BID = "brk_1a2b3c4d"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _leg(leg: str, status: str = "armed", fires: list[dict[str, Any]] | None = None) -> dict:
    trigger = copy.deepcopy(_fixture("trigger-armed")["trigger"])
    above = leg == "tp"
    trigger.update(
        id="trg_0000000a" if above else "trg_0000000b",
        name="Take-profit WETH" if above else "Stop-loss WETH",
        status=status,
        statusReason=None,
        fires=fires or [],
        bracket={"id": BID, "name": "Protect WETH", "leg": leg},
    )
    trigger["condition"].update(
        direction="above" if above else "below",
        priceUsd=2400.0 if above else 1800.0,
        label="over $2,400" if above else "under $1,800",
        hits=1 if above else 0,
    )
    return trigger


def _bracket(status: str = "armed", kind: str = "sell", **extra: Any) -> dict[str, Any]:
    tp, sl = _leg("tp", status), _leg("sl", status)
    base = tp
    bracket = {
        "id": BID,
        "name": "Protect WETH" if kind == "sell" else "Watch WETH",
        "kind": kind,
        "status": status,
        "statusReason": None,
        "chain": base["chain"],
        "wallet": base["wallet"],
        "token": base["token"],
        "quote": base["quote"],
        "takeProfit": tp,
        "stopLoss": sl,
        "lines": {
            "takeProfitUsd": 2400.0,
            "stopLossUsd": 1800.0,
            "trailPct": None,
            "fromPriceUsd": 2000.0,
            "takeProfitLabel": "over $2,400",
            "stopLossLabel": "under $1,800",
        },
        "action": {
            "kind": kind,
            "amountPct": 100.0 if kind == "sell" else None,
            "amount": None,
            "amountUsd": None,
            "tpPct": None,
            "estimatedUsd": 189.4 if kind == "sell" else None,
            "slippagePct": None,
            "needsApproval": False,
            "approvalThresholdUsd": 100.0,
            "dailyCapUsd": 1000.0,
            "label": "sell 100 % of WETH → USDC" if kind == "sell" else "notify",
        },
        "market": {
            "priceUsd": 2000.0,
            "armedPriceUsd": 2000.0,
            "checkedAt": "2026-10-05T09:00:00Z",
            "balance": {"raw": "94700000000000000", "human": "0.0947", "usd": 189.4}
            if kind == "sell"
            else None,
            "upsidePct": 20.0,
            "downsidePct": -10.0,
            "positionPct": 33.3,
            "rewardRisk": 2.0,
            "nearest": "sl",
        },
        "fired": None,
        "result": None,
        "validUntil": None,
        "initiator": "manual",
        "sessionKey": None,
        "createdAt": "2026-10-05T08:00:00Z",
        "updatedAt": "2026-10-05T09:00:00Z",
        "approvedAt": None,
        "armedAt": "2026-10-05T08:00:00Z",
        "expiresAt": None,
    }
    bracket.update(extra)
    return {
        "version": 1,
        "kind": "bracket",
        "fetchedAt": "2026-10-05T09:00:05Z",
        "warnings": [],
        "request": {"kind": "get", "params": {"bracketId": BID}},
        "bracket": bracket,
    }


def _brackets(*items: dict[str, Any], all_: bool = False) -> dict[str, Any]:
    return {
        "version": 1,
        "kind": "brackets",
        "fetchedAt": "2026-10-05T09:00:05Z",
        "warnings": [],
        "request": {"kind": "list", "params": {"all": True} if all_ else {}},
        "brackets": list(items),
        "totals": {"count": len(items), "armed": 1, "awaiting": 1, "triggered": 0},
    }


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, Any] = {}

    async def call(self, method: str, params: dict | None = None) -> Any:
        self.calls.append((method, dict(params or {})))
        if method in self.responses:
            answer = self.responses[method]
            return answer() if callable(answer) else answer
        if method == "trading.bracket.list":
            return _brackets(_bracket()["bracket"], _bracket("awaiting_approval")["bracket"])
        if method == "trading.bracket.create":
            return _bracket("awaiting_approval")
        return _bracket()


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


PROTECT = ["protect", "ETH", "--tp", "+20%", "--sl", "-10%"]
SELL_DEFAULTS = {"chainId": 8453, "kind": "sell", "token": "ETH", "initiator": "manual"}


@pytest.mark.parametrize(
    ("args", "method", "params"),
    [
        (
            # No size: the whole position (--pct 100).
            PROTECT,
            "trading.bracket.create",
            {**SELL_DEFAULTS, "takeProfit": "+20%", "stopLoss": "-10%", "amountPct": 100.0},
        ),
        (
            ["protect", "ETH", "--tp", "4560", "--sl", "$3,420", "--pct", "50", "--tp-pct", "25"],
            "trading.bracket.create",
            {
                **SELL_DEFAULTS,
                "takeProfit": "4560",
                "stopLoss": "3420",
                "amountPct": 50.0,
                "tpPct": 25.0,
            },
        ),
        (
            # "chốt lời một nửa": half at take-profit, the stop guards the whole.
            ["protect", "ETH", "--tp", "20%", "--sl", "10%", "--tp-pct", "50"],
            "trading.bracket.create",
            {
                **SELL_DEFAULTS,
                "takeProfit": "+20%",
                "stopLoss": "-10%",
                "amountPct": 100.0,
                "tpPct": 50.0,
            },
        ),
        (
            ["protect", "ETH", "--tp=+20%", "--trail", "10", "--amount", "0.05", "--slippage", "1"],
            "trading.bracket.create",
            {
                **SELL_DEFAULTS,
                "takeProfit": "+20%",
                "trailPct": 10.0,
                "amount": "0.05",
                "slippagePct": 1.0,
            },
        ),
        (
            [
                "protect",
                "0xabc",
                "--tp",
                "2",
                "--sl",
                "1",
                "--usd",
                "100",
                "--chain",
                "robinhood",
                "--quote",
                "USDG",
                "--wallet",
                "Main",
                "--name",
                "Guard",
                "--for",
                "7d",
            ],
            "trading.bracket.create",
            {
                "chainId": 4663,
                "kind": "sell",
                "token": "0xabc",
                "takeProfit": "2",
                "stopLoss": "1",
                "amountUsd": 100.0,
                "quote": "USDG",
                "wallet": "Main",
                "name": "Guard",
                "validForSeconds": 604_800,
                "initiator": "manual",
            },
        ),
        (
            ["protect", "ETH", "--tp", "4560", "--sl", "3420", "--alert", "--for", "30m"],
            "trading.bracket.create",
            {
                "chainId": 8453,
                "kind": "alert",
                "token": "ETH",
                "takeProfit": "4560",
                "stopLoss": "3420",
                "validForSeconds": 1_800,
                "initiator": "manual",
            },
        ),
        (["bracket", "list"], "trading.bracket.list", {}),
        (
            ["bracket", "list", "--all", "--wallet", "0x1"],
            "trading.bracket.list",
            {"all": True, "wallet": "0x1"},
        ),
        (["bracket", "show", BID], "trading.bracket.get", {"bracketId": BID}),
        (["bracket", "approve", BID, "--yes"], "trading.bracket.approve", {"bracketId": BID}),
        (["bracket", "reject", BID], "trading.bracket.reject", {"bracketId": BID}),
        (
            ["bracket", "reject", BID, "--reason", "too tight"],
            "trading.bracket.reject",
            {"bracketId": BID, "reason": "too tight"},
        ),
        (["bracket", "pause", BID], "trading.bracket.pause", {"bracketId": BID}),
        (["bracket", "resume", BID], "trading.bracket.resume", {"bracketId": BID}),
        (
            ["bracket", "stop", BID, "--reason", "changed my mind"],
            "trading.bracket.stop",
            {"bracketId": BID, "reason": "changed my mind"},
        ),
        (["bracket", "fire", BID], "trading.bracket.fire", {"bracketId": BID}),
        (
            ["bracket", "fire", BID, "--leg", "SL"],
            "trading.bracket.fire",
            {"bracketId": BID, "leg": "sl"},
        ),
    ],
)
def test_json_prints_payload_then_the_marker_last(
    client: _Client, args: list[str], method: str, params: dict[str, Any], tmp_path: Path
) -> None:
    result = runner.invoke(trade_cmd.app, [*args, "--json"])
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
    assert json.loads((tmp_path / path).read_text(encoding="utf-8")) == payload
    name = Path(path).name
    if payload["kind"] == "brackets":
        all_ = payload["request"]["params"].get("all")
        assert name.startswith("brackets-all-" if all_ else "brackets-live-")
    else:
        assert name.startswith(f"bracket-{payload['bracket']['id']}-")
    assert name.endswith("Z.json")


@pytest.mark.parametrize(("all_", "slug"), [(True, "all"), (False, "live")])
def test_list_card_slug(client: _Client, all_: bool, slug: str) -> None:
    client.responses["trading.bracket.list"] = _brackets(all_=all_)
    result = runner.invoke(trade_cmd.app, ["bracket", "list", "--json"])
    assert result.exit_code == 0, result.output
    marker = _lines(result.stdout)[-1]
    assert marker.startswith(f"publish_artifact path=trigger-cards/brackets-{slug}-")


def test_agent_shell_protect_passes_initiator_and_session_key(
    client: _Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTOS_SESSION_KEY", "agent:trading:desk:1")
    result = runner.invoke(trade_cmd.app, [*PROTECT, "--json"])
    assert result.exit_code == 0, result.output
    _, params = client.calls[0]
    assert params["initiator"] == "agent" and params["sessionKey"] == "agent:trading:desk:1"
    assert json.loads(_lines(result.stdout)[0])["bracket"]["status"] == "awaiting_approval"


def test_no_card_writes_nothing(client: _Client, tmp_path: Path) -> None:
    result = runner.invoke(trade_cmd.app, ["bracket", "show", BID, "--json", "--no-card"])
    assert result.exit_code == 0, result.output
    assert "publish_artifact" not in result.stdout
    assert json.loads(result.stdout)["kind"] == "bracket"
    assert not (tmp_path / trade_cmd.TRIGGER_CARD_DIR).exists()


def test_pruning_keeps_the_newest_cards_of_all_four_prefixes(
    client: _Client, tmp_path: Path
) -> None:
    cards = tmp_path / trade_cmd.TRIGGER_CARD_DIR
    cards.mkdir()
    prefixes = ("trigger-trg_", "triggers-live", "bracket-brk_", "brackets-all")
    for i in range(trade_cmd.TRIGGER_CARDS_KEPT + 8):
        old = cards / f"{prefixes[i % 4]}{i:02d}-20260101T0000{i:02d}Z.json"
        old.write_text("{}", encoding="utf-8")
        os.utime(old, (1_700_000_000 + i, 1_700_000_000 + i))
    unrelated = cards / "notes.json"
    unrelated.write_text("{}", encoding="utf-8")
    os.utime(unrelated, (1, 1))
    result = runner.invoke(trade_cmd.app, ["bracket", "show", BID, "--json"])
    assert result.exit_code == 0, result.output
    marker = INLINE_ARTIFACT_MARKER_RE.search(_lines(result.stdout)[-1])
    assert marker is not None
    kept = sorted(p.name for p in cards.iterdir() if p.name != "notes.json")
    assert len(kept) == trade_cmd.TRIGGER_CARDS_KEPT == 20
    assert Path(marker.group("path")).name in kept
    assert "trigger-trg_00-20260101T000000Z.json" not in kept
    assert "bracket-brk_02-20260101T000002Z.json" not in kept
    assert "brackets-all27-20260101T000027Z.json" in kept
    assert unrelated.exists()
    for name in ("bracket-brk_1-x.json", "brackets-live-x.json", "trigger-x.json"):
        assert trade_cmd._TRIGGER_CARD_FILE.match(name)
    assert not trade_cmd._TRIGGER_CARD_FILE.match("brackets.json")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["protect", "ETH", "--sl", "-10%"], "--tp is required"),
        (["protect", "ETH", "--tp", "+20%"], "exactly one stop line"),
        (["protect", "ETH", "--tp", "+20%", "--sl", "-10%", "--trail", "5"], "(got --sl, --trail)"),
        ([*PROTECT, "--pct", "50", "--usd", "5"], "at most one size"),
        ([*PROTECT, "--amount", "1", "--usd", "5"], "(got --amount, --usd)"),
        ([*PROTECT, "--usd", "5", "--tp-pct", "50"], "--tp-pct works with --pct"),
        ([*PROTECT, "--amount", "1", "--tp-pct", "50"], "not --amount"),
        ([*PROTECT, "--pct", "50", "--tp-pct", "60"], "at most --pct (50)"),
        ([*PROTECT, "--tp-pct", "0"], "--tp-pct must be above 0"),
        ([*PROTECT, "--tp-pct", "101"], "at most --pct (100)"),
        ([*PROTECT, "--alert", "--pct", "50"], "--alert takes no size"),
        ([*PROTECT, "--alert", "--tp-pct", "50"], "(it sends no order; got --tp-pct)"),
        (["protect", "ETH", "--tp", "3000", "--sl", "3400"], "--tp 3000 must be above --sl 3400"),
        (["protect", "ETH", "--tp", "3400", "--sl", "3400"], "must be above --sl"),
        (["protect", "ETH", "--tp", "-20%", "--sl", "-10%"], "--tp '-20%' is under"),
        (["protect", "ETH", "--tp", "+20%", "--sl", "+10%"], "--sl '+10%' is over"),
        (["protect", "ETH", "--tp", "high", "--sl", "-10%"], "--tp 'high' is not a price"),
        (["protect", "ETH", "--tp", "+20%", "--sl", "100%"], "100 % or more"),
        (["protect", "ETH", "--tp", "+20%", "--trail", "0"], "--trail must be above 0"),
        (["protect", "ETH", "--tp", "+20%", "--trail", "100"], "--trail must be above 0"),
        (["protect", "ETH", "--tp", "+20%", "--trail", "ten"], "--trail"),
        ([*PROTECT, "--pct", "0"], "--pct must be above 0"),
        ([*PROTECT, "--pct", "101"], "at most 100"),
        ([*PROTECT, "--amount", "lots"], "--amount 'lots'"),
        ([*PROTECT, "--usd", "0"], "--usd must be above 0"),
        ([*PROTECT, "--slippage", "0"], "--slippage must be above 0"),
        ([*PROTECT, "--for", "30s"], "--for must be at least"),
        ([*PROTECT, "--for", "soon"], "--for 'soon'"),
        ([*PROTECT, "--chain", "eth"], "unknown chain 'eth'"),
        (["protect", "--tp", "+20%", "--sl", "-10%"], "Missing argument"),
        ([*PROTECT, "--bogus"], "No such option: --bogus"),
        (["bracket", "show", " "], "bracket id is required"),
        (["bracket", "show"], "Missing argument"),
        (["bracket", "list", "--bogus"], "No such option: --bogus"),
        (["bracket", "fire", BID, "--leg", "both"], "--leg 'both' must be tp or sl"),
        (["bracket", "fire", BID, "--wait-seconds", "0"], "--wait-seconds"),
    ],
)
def test_usage_errors_under_json_are_json(client: _Client, args: list[str], message: str) -> None:
    result = runner.invoke(trade_cmd.app, [*args, "--json"])
    assert result.exit_code == 2, result.output
    assert client.calls == []
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "INVALID_ARGUMENT" and message in error["message"], error
    assert result.stdout == ""


def test_usage_error_without_json_is_still_exit_2(client: _Client) -> None:
    result = runner.invoke(trade_cmd.app, ["protect", "ETH", "--tp", "+20%"])
    assert result.exit_code == 2
    assert "exactly one stop line" in result.stderr
    assert client.calls == []
    result = runner.invoke(trade_cmd.app, ["protect", "--tp", "1", "--sl", "1"])
    assert result.exit_code == 2
    assert "Usage:" in result.stderr and not result.stderr.lstrip().startswith("{")


@pytest.mark.parametrize(
    "code",
    [
        "trading.bracket.invalid",
        "trading.bracket.bad_state",
        "trading.bracket.not_found",
        "trading.token_not_found",
        "trading.invalid",
    ],
)
@pytest.mark.parametrize("args", [PROTECT, ["bracket", "pause", BID]])
def test_input_or_state_the_engine_refuses_exits_2(
    client: _Client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    code: str,
    args: list[str],
) -> None:
    from agentos.cli.gateway_client import GatewayRPCError

    async def call(method: str, params: dict | None = None) -> Any:
        raise GatewayRPCError(method, code=code, message="take-profit $3,000 must be above")

    monkeypatch.setattr(client, "call", call)
    result = runner.invoke(trade_cmd.app, [*args, "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == code
    assert result.stdout == ""
    assert not (tmp_path / trade_cmd.TRIGGER_CARD_DIR).exists()


def test_usage_codes_are_the_contracts() -> None:
    assert trade_cmd._BRACKET_USAGE_CODES == {
        "trading.bracket.invalid",
        "trading.bracket.bad_state",
        "trading.bracket.not_found",
        "trading.invalid",
        "trading.token_not_found",
    }


def test_a_gateway_error_is_exit_1(client: _Client, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentos.cli.gateway_client import GatewayRPCError

    async def call(method: str, params: dict | None = None) -> Any:
        raise GatewayRPCError(method, code="trading.trigger.bad_state", message="leg")

    monkeypatch.setattr(client, "call", call)
    # A trigger code is not a bracket usage code: it is not swallowed as exit 2.
    with pytest.raises(GatewayRPCError):
        asyncio.run(
            trade_cmd._trigger_rpc(
                client,
                "trading.bracket.pause",
                {},
                json_output=True,
                usage_codes=trade_cmd._BRACKET_USAGE_CODES,
            )
        )


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
    result = runner.invoke(trade_cmd.app, ["bracket", "approve", BID, "--yes", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["code"] == "trading.operator_required"
    assert "publish_artifact" not in result.stdout
    assert not (tmp_path / trade_cmd.TRIGGER_CARD_DIR).exists()


def _fire(status: str, order_id: str | None = "ord_00000000000d", n: int = 1) -> dict:
    return {
        "n": n,
        "at": "2026-10-05T09:20:00Z",
        "manual": True,
        "status": status,
        "reasonCode": None,
        "reason": None,
        "priceUsd": 2000.0,
        "orderId": order_id,
        "txHash": None,
        "explorerUrl": None,
    }


def test_fire_wait_waits_for_the_order_then_refetches(client: _Client) -> None:
    fired = _bracket("triggered")
    fired["fire"] = _fire("pending")
    fresh = _bracket("done", fired="sl")
    other = _fire("filled", "ord_other", n=1)  # the take-profit's own #1, another order
    fresh["bracket"]["takeProfit"]["fires"] = [other]
    settled = {**_fire("pending"), "status": "filled", "txHash": "0xfeed"}
    fresh["bracket"]["stopLoss"]["fires"] = [settled]
    client.responses["trading.bracket.fire"] = fired
    client.responses["trading.orders.wait"] = {"order": {"status": "confirmed"}}
    client.responses["trading.bracket.get"] = fresh
    result = runner.invoke(
        trade_cmd.app,
        ["bracket", "fire", BID, "--leg", "sl", "--wait", "--wait-seconds", "30", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert client.calls == [
        ("trading.bracket.fire", {"bracketId": BID, "leg": "sl"}),
        ("trading.orders.wait", {"orderId": "ord_00000000000d", "timeoutSeconds": 30}),
        ("trading.bracket.get", {"bracketId": BID}),
    ]
    payload = json.loads(_lines(result.stdout)[0])
    assert payload["bracket"]["status"] == "done"
    assert payload["fire"]["status"] == "filled" and payload["fire"]["txHash"] == "0xfeed"
    assert _lines(result.stdout)[-1].startswith("publish_artifact path=trigger-cards/bracket-")


@pytest.mark.parametrize(
    ("status", "order_id", "wait"),
    [("alerted", None, True), ("filled", "ord_1", True), ("pending", "ord_1", False)],
)
def test_fire_does_not_wait_when_there_is_nothing_to_wait_for(
    client: _Client, status: str, order_id: str | None, wait: bool
) -> None:
    payload = _bracket("triggered")
    payload["fire"] = _fire(status, order_id)
    client.responses["trading.bracket.fire"] = payload
    args = ["bracket", "fire", BID, "--json", *(["--wait"] if wait else [])]
    result = runner.invoke(trade_cmd.app, args)
    assert result.exit_code == 0, result.output
    assert [m for m, _ in client.calls] == ["trading.bracket.fire"]
    assert json.loads(_lines(result.stdout)[0])["fire"]["status"] == status


def test_fire_human_output_shows_the_fire(client: _Client) -> None:
    payload = _bracket("triggered")
    payload["fire"] = _fire("parked")
    client.responses["trading.bracket.fire"] = payload
    result = runner.invoke(trade_cmd.app, ["bracket", "fire", BID])
    assert result.exit_code == 0, result.output
    assert "Fire: #1" in result.stdout and "fire now" in result.stdout
    assert "awaiting approval" in result.stdout


def test_show_panel(client: _Client, tmp_path: Path) -> None:
    payload = _bracket(statusReason=None)
    payload["bracket"]["takeProfit"]["fires"] = [
        {**_fire("failed", None, n=1), "at": "2026-10-05T08:30:00Z", "reason": "no route"}
    ]
    payload["bracket"]["stopLoss"]["fires"] = [
        {**_fire("skipped", None, n=1), "at": "2026-10-05T08:40:00Z", "reason": "nothing to sell"}
    ]
    payload["warnings"] = ["take-profit: the line is close"]
    client.responses["trading.bracket.get"] = payload
    result = runner.invoke(trade_cmd.app, ["bracket", "show", BID])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "publish_artifact" not in out
    assert not (tmp_path / trade_cmd.TRIGGER_CARD_DIR).exists()
    assert "▼▲ Protect WETH · armed" in out
    assert "take-profit over $2,400 · stop under $1,800" in out
    # The price now sits between the two lines.
    line = next(x for x in out.splitlines() if "now" in x and "take-profit $" in x)
    assert line.index("stop $1,800.00") < line.index("$2,000.00 now") < line.index("$2,400.00")
    assert "upside +20.00% · downside -10.00% · reward:risk 2 : 1" in out
    assert "size 100 % · ≈ $189.40" in out and "balance 0.0947 WETH" in out
    assert "approval automatic" in out
    assert "valid until GTC" in out
    assert "take-profit   over $2,400 · armed · checks 1/2" in out
    assert "stop-loss     under $1,800 · armed · checks 0/2" in out
    # Both legs' fires, merged newest first.
    fires = [x for x in out.splitlines() if " #1 · " in x]
    assert "stop-loss #1" in fires[0] and "nothing to sell" in fires[0]
    assert "take-profit #1" in fires[1] and "no route" in fires[1]
    assert "the line is close" in out
    assert BID in out


def test_show_panel_awaiting_partial_trail_and_done(client: _Client) -> None:
    payload = _bracket("awaiting_approval")
    bracket = payload["bracket"]
    bracket["action"].update(amountPct=100.0, tpPct=50.0)
    bracket["lines"].update(trailPct=10.0, stopLossLabel="10 % below peak")
    bracket["stopLoss"]["condition"]["direction"] = "trail"
    client.responses["trading.bracket.get"] = payload
    result = runner.invoke(trade_cmd.app, ["bracket", "show", BID])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "50 % at take-profit, 100 % at stop" in out
    assert "stop $1,800.00 (10 % below peak)" in out
    assert "trailing stop" in out
    assert f"agentos trade bracket approve {BID}" in out and "both legs" in out

    done = _bracket("done", fired="tp", statusReason="take-profit: sold 0.02 WETH")
    done["bracket"]["result"] = _fixture("trigger-done")["trigger"]["result"]
    client.responses["trading.bracket.get"] = done
    result = runner.invoke(trade_cmd.app, ["bracket", "show", BID])
    assert result.exit_code == 0, result.output
    assert "Protect WETH · done (take-profit: sold 0.02 WETH)" in result.stdout
    assert "result (take-profit) 0.02 WETH → 42.4 USDC @ $2,120.00" in result.stdout


def test_show_panel_alert_has_no_size(client: _Client) -> None:
    client.responses["trading.bracket.get"] = _bracket(kind="alert")
    result = runner.invoke(trade_cmd.app, ["bracket", "show", BID])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "◆ Watch WETH · armed" in out
    assert "notify when WETH is over $2,400 or under $1,800" in out
    assert "size " not in out and "approval" not in out


def test_list_table(client: _Client) -> None:
    alert = _bracket("awaiting_approval", kind="alert")["bracket"]
    alert["id"] = "brk_00000002"
    payload = _brackets(_bracket()["bracket"], alert)
    client.responses["trading.bracket.list"] = payload
    result = runner.invoke(trade_cmd.app, ["bracket", "list"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "Brackets · 2" in out
    header = next(line for line in out.splitlines() if "Up / down" in line)
    columns = ["Name", "Status", "Token", "Range", "Price now", "Up / down", "Size"]
    positions = [header.index(column) for column in columns]
    assert positions == sorted(positions), header
    assert BID in out and "brk_00000002" in out  # the id rides under the name
    assert "$1,800.00 – $2,400.00" in out and "+20.00% / -10.00%" in out
    assert "100 % · ≈ $189.40" in out and "notify" in out
    assert " armed · " in out and " awaiting · " in out
    assert "publish_artifact" not in out


def test_list_empty(client: _Client) -> None:
    client.responses["trading.bracket.list"] = _brackets()
    result = runner.invoke(trade_cmd.app, ["bracket", "list"])
    assert result.exit_code == 0, result.output
    assert "No brackets yet." in result.stdout


@pytest.mark.parametrize(
    "name",
    [
        "bracket-armed",
        "bracket-awaiting",
        "bracket-done",
        "bracket-alert",
        "brackets",
        "brackets-empty",
    ],
)
def test_engine_fixtures_render(client: _Client, name: str) -> None:
    """The engine's regenerated fixtures (when present) render without error."""
    if not (FIXTURES / f"{name}.json").exists():
        pytest.skip(f"{name}.json not generated yet")
    payload = _fixture(name)
    method = "trading.bracket.list" if payload["kind"] == "brackets" else "trading.bracket.get"
    client.responses[method] = payload
    args = ["bracket", "list"] if payload["kind"] == "brackets" else ["bracket", "show", BID]
    result = runner.invoke(trade_cmd.app, args)
    assert result.exit_code == 0, result.output
    if payload["kind"] == "bracket":
        assert f"{payload['bracket']['name']} · {payload['bracket']['status']}" in result.stdout
