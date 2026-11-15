"""``agentos trade`` as an approval surface (WAL-08, WAL-12 on the CLI).

Ports the v3-15 harness: two orders that differ only in a token contract, or
only in the middle of a recipient, must not render alike; an approve shows
the server record — every leg of a batch — and asks before it acts.
"""

from __future__ import annotations

import json
from typing import Any

from typer.testing import CliRunner

from agentos.cli import trade_cmd
from tests.test_cli.test_trade_cmd import TOKENS, WALLET, _FakeClient, _order, client

__all__ = ["client"]

runner = CliRunner()

TWIN_WETH = {
    "symbol": "WETH",
    "address": "0xabababababababababababababababababababab",
    "verified": False,
    "name": "Wrapped Ether",
}
RECIPIENT = "0x1234000000000000000000000000000000005678"
TWIN_RECIPIENT = "0x1234999999999999999999999999999999995678"
DIGEST = "ab" * 32


def _send(**extra: Any) -> dict[str, Any]:
    order = _order(
        kind="send",
        tokenOut=TOKENS["USDC"],
        tokenIn=TOKENS["USDC"],
        amountIn="100",
        recipient=RECIPIENT,
        status="awaiting_approval",
        initiator="agent",
        valueUsd=100.0,
        txHash=None,
        approval={"digest": DIGEST, "legCount": 1, "batchId": None},
    )
    order.update(extra)
    return order


def _masked(output: str, *ids: str) -> str:
    for order_id in ids:
        output = output.replace(order_id, "ord-X")
    return output


def test_a_twin_token_does_not_render_like_the_real_one(client: _FakeClient) -> None:
    real = _order(orderId="ord-real", status="awaiting_approval", initiator="agent")
    twin = _order(
        orderId="ord-twin", status="awaiting_approval", initiator="agent", tokenOut=TWIN_WETH
    )
    rows: dict[str, str] = {}
    shown: dict[str, str] = {}
    for order in (real, twin):
        client.payloads["trading.orders.list"] = {"orders": [order], "pendingApprovals": 1}
        client.payloads["trading.orders.get"] = {"order": order}
        rows[order["orderId"]] = runner.invoke(trade_cmd.app, ["orders"]).output
        shown[order["orderId"]] = runner.invoke(trade_cmd.app, ["order", order["orderId"]]).output
    assert _masked(rows["ord-real"], "ord-real") != _masked(rows["ord-twin"], "ord-twin")
    assert "WETH (UNVERIFIED)" in rows["ord-twin"] and "UNVERIFIED" not in rows["ord-real"]
    # The detail view names the contract in full, with its mark.
    assert TWIN_WETH["address"] in shown["ord-twin"] and "UNVERIFIED" in shown["ord-twin"]
    assert TOKENS["WETH"]["address"] in shown["ord-real"] and "verified" in shown["ord-real"]


def test_two_recipients_sharing_their_ends_render_differently(client: _FakeClient) -> None:
    outputs = []
    for recipient in (RECIPIENT, TWIN_RECIPIENT):
        client.payloads["trading.orders.list"] = {
            "orders": [_send(recipient=recipient)],
            "pendingApprovals": 1,
        }
        outputs.append(runner.invoke(trade_cmd.app, ["orders"]).output)
    assert outputs[0] != outputs[1]
    assert RECIPIENT in outputs[0] and TWIN_RECIPIENT in outputs[1]
    # The pending row names the wallet in full too.
    assert WALLET in outputs[0]


def test_approve_without_a_yes_off_a_terminal_sends_nothing(client: _FakeClient) -> None:
    client.payloads["trading.orders.get"] = {"order": _send()}
    result = runner.invoke(trade_cmd.app, ["approve", "ord-1"], input="")
    assert result.exit_code != 0
    assert client.calls_to("trading.orders.approve") == []
    # What would have been approved was on screen before the question.
    assert RECIPIENT in result.output and TOKENS["USDC"]["address"] in result.output
    as_json = runner.invoke(trade_cmd.app, ["approve", "ord-1", "--json"])
    assert as_json.exit_code == 2
    assert json.loads(as_json.stderr)["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert client.calls_to("trading.orders.approve") == []


def test_approve_shows_then_confirms_then_sends_the_digest(client: _FakeClient) -> None:
    client.payloads["trading.orders.get"] = {"order": _send()}
    declined = runner.invoke(trade_cmd.app, ["approve", "ord-1"], input="n\n")
    assert declined.exit_code != 0 and client.calls_to("trading.orders.approve") == []
    result = runner.invoke(trade_cmd.app, ["approve", "ord-1"], input="y\n")
    assert result.exit_code == 0, result.output
    assert f"Approve order ord-1 (100 USDC → {RECIPIENT})?" in result.output
    assert client.calls_to("trading.orders.approve") == [
        {"orderId": "ord-1", "approvalDigest": DIGEST}
    ]


def test_approving_a_batch_leg_shows_every_leg_and_binds_the_batch(
    client: _FakeClient,
) -> None:
    legs = [
        {"orderId": "ord-1", "recipient": RECIPIENT, "amount": "1", "valueUsd": 1.0},
        {"orderId": "ord-2", "recipient": TWIN_RECIPIENT, "amount": "450", "valueUsd": 450.0},
        {
            "orderId": "ord-3",
            "recipient": "0x0000000000000000000000000000000000bad002",
            "amount": "449",
            "valueUsd": 449.0,
        },
    ]
    leg = _send(
        amountIn="1",
        valueUsd=1.0,
        batchId="bat_1",
        approval={
            "digest": None,
            "batchId": "bat_1",
            "legCount": 3,
            "total": "900",
            "totalUsd": 900.0,
            "token": TOKENS["USDC"],
        },
    )
    client.payloads["trading.orders.get"] = {"order": leg}
    client.payloads["trading.orders.batch"] = {
        "orders": [leg],
        "batchId": "bat_1",
        "approval": {
            "digest": DIGEST,
            "batchId": "bat_1",
            "legCount": 3,
            "total": "900",
            "totalUsd": 900.0,
            "token": TOKENS["USDC"],
            "unpriced": False,
            "legs": legs,
        },
    }
    result = runner.invoke(trade_cmd.app, ["approve", "ord-1"], input="y\n")
    assert result.exit_code == 0, result.output
    for item in legs:
        assert str(item["recipient"]) in result.output
    assert "3 legs awaiting, one approval runs them all" in result.output
    assert "Approve ALL 3 legs of batch bat_1 (900 USDC, $900.00)" in result.output
    assert client.calls_to("trading.orders.approve") == [
        {"orderId": "ord-1", "approvalDigest": DIGEST}
    ]


def test_an_unpriced_batch_says_the_cap_does_not_bound_it(client: _FakeClient) -> None:
    leg = _send(batchId="bat_2", valueUsd=None, approval={"digest": None, "legCount": 2})
    client.payloads["trading.orders.get"] = {"order": leg}
    client.payloads["trading.orders.batch"] = {
        "orders": [leg],
        "batchId": "bat_2",
        "approval": {
            "digest": DIGEST,
            "batchId": "bat_2",
            "legCount": 2,
            "total": "3",
            "totalUsd": None,
            "token": TOKENS["USDC"],
            "unpriced": True,
            "legs": [
                {"orderId": "ord-1", "recipient": RECIPIENT, "amount": "1"},
                {"orderId": "ord-2", "recipient": TWIN_RECIPIENT, "amount": "2"},
            ],
        },
    }
    shown = runner.invoke(trade_cmd.app, ["order", "ord-1"])
    assert "the daily cap does not bound this batch" in shown.output
    assert TWIN_RECIPIENT in shown.output


# ── WAL-13: control characters are shown as escapes, never sent ────────────────

ESC_SYMBOL = {
    "symbol": "TKN\x1b[31mRED‮",
    "address": "0xcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd",
    "verified": False,
}
_RAW = ("\x1b", "\x9b", "‮", "​", "\x07")


def _assert_neutral(output: str) -> None:
    for char in _RAW:
        assert char not in output, repr(char)


def test_hostile_text_reaches_no_trade_view_raw(client: _FakeClient) -> None:
    failed = _order(
        orderId="ord-f",
        status="failed",
        amountIn="7 \x1b[31mRED",
        reason="trading.invalid: amount is not a number: '7 \x1b[31mRED'‮",
        txHash=None,
    )
    parked = _send(
        tokenIn=ESC_SYMBOL,
        note="pay‮evil",
        recipientLabel="Label\x9b2J",
    )
    client.payloads["trading.orders.list"] = {"orders": [failed, parked], "pendingApprovals": 1}
    listing = runner.invoke(trade_cmd.app, ["orders"])
    assert listing.exit_code == 0, listing.output
    _assert_neutral(listing.output)
    # Visible, so the operator sees something is off.
    assert "TKN\\x1b[31mRED\\u202e" in listing.output
    for order in (failed, parked):
        client.payloads["trading.orders.get"] = {"order": order}
        shown = runner.invoke(trade_cmd.app, ["order", str(order["orderId"])])
        assert shown.exit_code == 0, shown.output
        _assert_neutral(shown.output)
    approve = runner.invoke(trade_cmd.app, ["approve", "ord-1"], input="n\n")
    _assert_neutral(approve.output)


def test_json_output_escapes_what_a_terminal_would_act_on(client: _FakeClient) -> None:
    order = _send(tokenIn=ESC_SYMBOL, note="a\x9bb‮c")
    client.payloads["trading.orders.get"] = {"order": order}
    result = runner.invoke(trade_cmd.app, ["order", "ord-1", "--json"])
    assert result.exit_code == 0
    _assert_neutral(result.stdout)
    # Same data once parsed: only the encoding changed.
    assert json.loads(result.stdout)["order"]["note"] == "a\x9bb‮c"
    assert json.loads(result.stdout)["order"]["tokenIn"]["symbol"] == ESC_SYMBOL["symbol"]


def test_a_server_error_is_printed_neutralised(monkeypatch) -> None:
    from agentos.cli import output

    lines: list[str] = []
    monkeypatch.setattr(output.typer, "secho", lambda text, **_kw: lines.append(text))
    output.emit_error("holds 5 TKN\x1b[2J‮\nsecond line")
    assert lines == ["Error: holds 5 TKN\\x1b[2J\\u202e\nsecond line"]
