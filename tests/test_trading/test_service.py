from __future__ import annotations

import asyncio
import json

import pytest

from agentos.trading import service as service_module
from agentos.trading.chains import BASE, NATIVE_ADDRESS, ROBINHOOD
from agentos.trading.service import TradingError, TradingService
from tests.test_trading.conftest import PASSWORD
from tests.test_trading.fakes import (
    AAPL,
    OTHER,
    ROUTER,
    SWAP_TARGETS,
    USDC,
    WETH,
    FakeAggregator,
    FakeChain,
    FakeIndexer,
    FakePrices,
    FakeUniswap,
    decode_fake_raw,
    transfer_log,
)


def _events(service: TradingService, name: str) -> list[dict]:
    return [payload for event, payload in service.events if event == name]  # type: ignore[attr-defined]


def _wire_swap_effects(
    chain: FakeChain,
    wallet: str,
    *,
    out_token: str = WETH,
    out_amount: int = 5 * 10**14,
    status: int = 1,
):
    """Make eth_sendRawTransaction move balances and mint a receipt, like a node would."""

    def on_send(raw: str) -> str:
        tx = decode_fake_raw(raw)
        chain._seq += 1
        tx_hash = "0x" + format(0xFEED00 + chain._seq, "x").rjust(64, "0")
        gas_wei = 100_000 * 10**8
        chain.nonces[wallet.lower()] = tx.get("nonce", 0) + 1
        if tx["to"].lower() in SWAP_TARGETS and status == 1:
            data_in = int(tx["value"])
            chain.native[wallet.lower()] -= data_in + gas_wei
            if data_in == 0:
                chain.set_erc20(USDC, wallet, chain.get_erc20(USDC, wallet) - 10 * 10**6)
            if out_token == NATIVE_ADDRESS:
                chain.native[wallet.lower()] += out_amount
                logs = []
            else:
                chain.set_erc20(out_token, wallet, chain.get_erc20(out_token, wallet) + out_amount)
                logs = [
                    transfer_log(
                        tx_hash=tx_hash,
                        log_index=3,
                        block=chain.block,
                        token=out_token,
                        sender=ROUTER,
                        recipient=wallet,
                        amount=out_amount,
                    )
                ]
            chain.receipt(tx_hash, status=1, gas_used=100_000, gas_price=10**8, logs=logs)
        elif tx["to"].lower() == USDC:  # approval
            chain.native[wallet.lower()] -= gas_wei
            chain.apply_approve(wallet, tx)
            chain.receipt(tx_hash, status=1, gas_used=100_000, gas_price=10**8)
        else:
            chain.native[wallet.lower()] -= gas_wei
            chain.receipt(tx_hash, status=status, gas_used=100_000, gas_price=10**8)
        return tx_hash

    chain.on_send = on_send


class TestStatusAndWallets:
    async def test_status_reports_config_and_vault(self, service: TradingService) -> None:
        status = await service.status(check_rpc=True)
        assert status["apiKeyConfigured"] is True
        assert [c["chainId"] for c in status["chains"]] == [8453, 4663]
        assert all(c["healthy"] for c in status["chains"])
        assert status["limits"] == {
            "approvalThresholdUsd": 100.0,
            "dailyCapUsd": 1000.0,
            "approvalTtlSeconds": 900,
            "agentMaxPriceImpactPct": 5.0,
            "agentMaxSlippagePct": 5.0,
        }
        assert status["initialized"] is False and status["unlocked"] is False

    async def test_probe(self, service: TradingService, fake_uniswap: FakeUniswap) -> None:
        # The default provider is the aggregator: no key, so nothing to reject.
        assert (await service.probe())["ok"] is True
        assert (await service.probe(provider_id="uniswap"))["ok"] is True
        bad = await service.probe("nope", provider_id="uniswap")
        assert bad["ok"] is False and bad["error"]
        service.config.uniswap_api_key = ""
        no_key = await service.probe(provider_id="uniswap")
        assert no_key["error"] == "No Uniswap API key configured"
        # Losing the Uniswap key does not disturb the default route.
        assert (await service.probe())["ok"] is True

    async def test_create_wallet_stamps_block_and_mirrors(
        self, service: TradingService, base_chain: FakeChain, robinhood_chain: FakeChain
    ) -> None:
        service.vault.setup(PASSWORD, "auto")
        wallet = await service.create_wallet("Main")
        assert wallet["primary"] is True
        assert wallet["createdBlock"] == {"8453": base_chain.block, "4663": robinhood_chain.block}
        assert service.wallet_dicts()[0]["chains"] == [8453, 4663]
        assert _events(service, "trading.changed")[-1]["reason"] == "wallet"
        second = await service.import_wallet("Second", private_key="0x" + "11" * 32)
        assert second["primary"] is False and second["imported"] is True
        with pytest.raises(TradingError, match="privateKey or keystoreJson"):
            await service.import_wallet("x")
        await service.remove_wallet(second["address"], PASSWORD)
        assert len(service.wallet_dicts()) == 1

    async def test_wallet_selectors(self, funded_service: TradingService) -> None:
        service = funded_service
        second = await service.import_wallet("Second", private_key="0x" + "22" * 32)
        primary = service.test_wallet  # type: ignore[attr-defined]
        assert [w.address for w in service._wallets_for(None)] == [primary]
        assert [w.address for w in service._wallets_for("all")] == [primary, second["address"]]
        assert [w.address for w in service._wallets_for([second["address"], "Main"])] == [
            second["address"],
            primary,
        ]
        assert [w.address for w in service._wallets_for("second")] == [second["address"]]
        with pytest.raises(TradingError):
            service._wallets_for(42)


class TestTokensAndBalances:
    async def test_resolve_token_prefers_stock_tokens(self, service: TradingService) -> None:
        aapl = await service.resolve_token(ROBINHOOD, "AAPL")
        assert aapl.address == AAPL and aapl.stock_token
        eth = await service.resolve_token(BASE, "eth")
        assert eth.native
        usdc = await service.resolve_token(BASE, USDC)
        assert usdc.symbol == "USDC" and usdc.decimals == 6
        with pytest.raises(TradingError, match="Unknown token symbol"):
            await service.resolve_token(BASE, "NOPE")
        with pytest.raises(TradingError, match="required"):
            await service.resolve_token(BASE, "")

    async def test_search_and_on_chain_metadata_fallback(
        self, service: TradingService, base_chain: FakeChain
    ) -> None:
        rows = await service.search_tokens(BASE, "USDC")
        assert rows[0]["address"] == USDC and rows[0]["priceUsd"] == 1.0
        unknown = "0x9999000000000000000000000000000000000009"
        base_chain.tokens[unknown] = ("XYZ", "Xyz Coin", 9)
        meta = await service.token_meta(BASE, unknown)
        assert (meta.symbol, meta.decimals, meta.verified) == ("XYZ", 9, False)
        assert service.ledger.get_token(8453, unknown)["symbol"] == "XYZ"

    async def test_search_by_address_reads_an_unindexed_token_off_chain(
        self, service: TradingService, base_chain: FakeChain
    ) -> None:
        """No token list, no pool: the contract is the only one who knows."""
        unknown = "0x6eda83fc299c10d474068a7e69771c809bcbbba3"
        base_chain.tokens[unknown] = ("MOG", "Mog Coin", 6)

        rows = await service.search_tokens(BASE, unknown)

        assert len(rows) == 1
        # Not the blank row with a guessed 18 decimals it used to be.
        assert rows[0]["symbol"] == "MOG"
        assert rows[0]["name"] == "Mog Coin"
        assert rows[0]["decimals"] == 6
        assert rows[0]["verified"] is False
        # And it is remembered, so the next read does not pay for the calls.
        stored = service.ledger.get_token(8453, unknown)
        assert (stored["symbol"], stored["decimals"]) == ("MOG", 6)

    async def test_an_address_from_another_chain_is_not_a_token_here(
        self, service: TradingService, base_chain: FakeChain
    ) -> None:
        """No code at it here, so neither path may invent one."""
        elsewhere = "0x6eda83fc299c10d474068a7e69771c809bcbbba3"
        assert elsewhere not in base_chain.tokens

        assert await service.search_tokens(BASE, elsewhere) == []
        with pytest.raises(TradingError, match="No contract at"):
            await service.resolve_token(BASE, elsewhere)
        # Nothing phantom was written to the ledger on the way out.
        assert service.ledger.get_token(8453, elsewhere) is None

    async def test_a_requested_sync_reads_every_token(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``trading.sync`` is someone asking: it reads what the ordinary pass skips."""
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        await service.sync_all()
        service._hidden_sync_at = service._now()  # the hourly pass is not due
        stray = "0x9999000000000000000000000000000000000002"
        service.ledger.upsert_token(8453, stray, symbol="S", name="Stray", decimals=18)
        service.ledger.set_balance(8453, wallet.lower(), stray, 0)
        base_chain.tokens[stray] = ("S", "Stray", 18)
        base_chain.set_erc20(stray, wallet, 4)

        await service.sync_all()
        assert service.ledger.get_balance(8453, wallet, stray) == 0

        service.request_sync(wallet=wallet)
        await asyncio.gather(*service._confirm_tasks)
        assert service.ledger.get_balance(8453, wallet, stray) == 4

    async def test_balances_and_portfolio(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        await service.sync_all()
        balances = await service.balances()
        by_token = {b["token"]["address"]: b for b in balances if b["chainId"] == 8453}
        assert by_token[NATIVE_ADDRESS]["amount"] == "1" and by_token[NATIVE_ADDRESS][
            "valueUsd"
        ] == pytest.approx(2000.0)
        assert by_token[USDC]["amount"] == "1000" and by_token[USDC]["valueUsd"] == pytest.approx(
            1000.0
        )
        # Both balances were first seen by the sync -> opening lots at spot.
        portfolio = await service.portfolio()
        assert portfolio["totals"]["valueUsd"] == pytest.approx(3000.0)
        holdings = {h["token"]["address"]: h for h in portfolio["holdings"]}
        assert holdings[NATIVE_ADDRESS]["costUsd"] == pytest.approx(2000.0)
        assert holdings[USDC]["costUsd"] == pytest.approx(1000.0)
        assert holdings[USDC]["unrealizedUsd"] == pytest.approx(0.0)
        assert portfolio["totals"]["costUsd"] == pytest.approx(3000.0)
        assert sum(h["allocationPct"] for h in portfolio["holdings"]) == pytest.approx(100.0)
        assert portfolio["wallets"][0]["wallet"]["address"] == wallet
        assert (await service.portfolio(wallet))["totals"]["valueUsd"] == pytest.approx(3000.0)

    async def test_balances_read_the_ledger_and_refresh_is_throttled(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        """A client asking for balances never makes the engine read the chain.

        Only the sync loop, a settled swap and an explicit refresh do — and a
        refresh at most once per wallet per MANUAL_SYNC_MIN_S.
        """
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        await service.sync_all()
        by_token = {b["token"]["address"]: b for b in await service.balances(wallet)}
        assert by_token[USDC]["amount"] == "1000"
        assert by_token[USDC]["updatedAt"] > 0
        reads = service.chain_reads(wallet)
        assert {r["chainId"]: r["status"] for r in reads} == {8453: "ok", 4663: "ok"}

        # The wallet moved on-chain; a plain read still shows the ledger.
        base_chain.set_erc20(USDC, wallet, 2_000 * 10**6)
        base_chain.calls.clear()
        by_token = {b["token"]["address"]: b for b in await service.balances(wallet)}
        assert by_token[USDC]["amount"] == "1000"
        assert base_chain.calls == []

        # refresh=True runs the real sync path and sees the change.
        by_token = {b["token"]["address"]: b for b in await service.balances(wallet, refresh=True)}
        assert by_token[USDC]["amount"] == "2000"
        assert any(c["method"] == "eth_call" for c in base_chain.calls)

        # ...but not again within the throttle window.
        base_chain.set_erc20(USDC, wallet, 3_000 * 10**6)
        base_chain.calls.clear()
        by_token = {b["token"]["address"]: b for b in await service.balances(wallet, refresh=True)}
        assert by_token[USDC]["amount"] == "2000"
        assert base_chain.calls == []

    async def test_junk_airdrop_is_hidden_until_someone_means_it(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        """An unlisted, unpooled token that just arrived stays out of sight.

        Its ledger rows are real (the balance is), so it still reconciles;
        it is simply not shown, counted, priced or scanned every tick.
        """
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        junk = "0x9999000000000000000000000000000000000077"
        base_chain.tokens[junk] = ("CLAIM", "Visit site to claim", 18)
        base_chain.set_erc20(junk, wallet, 5 * 10**18)
        base_chain.add_transfer(token=junk, sender=OTHER, recipient=wallet, amount=5 * 10**18)
        await service.sync_all()

        # Out of balances, portfolio and history; counted; back on request.
        assert not any(b["token"]["address"] == junk for b in await service.balances(wallet))
        assert service.hidden_balance_count(wallet) == 1
        portfolio = await service.portfolio(wallet)
        assert portfolio["hiddenCount"] == 1
        assert not any(h["token"]["address"] == junk for h in portfolio["holdings"])
        assert portfolio["totals"]["valueUsd"] == pytest.approx(3000.0)
        deposits = service.history(wallet=wallet, kind="deposit")["entries"]
        assert not any((e["tokenOut"] or {}).get("address") == junk for e in deposits)
        assert any(
            (e["tokenOut"] or {}).get("address") == junk
            for e in service.history(wallet=wallet, kind="deposit", include_hidden=True)["entries"]
        )
        shown = await service.portfolio(wallet, include_hidden=True)
        junk_row = next(h for h in shown["holdings"] if h["token"]["address"] == junk)
        assert junk_row["hidden"] is True and junk_row["allocationPct"] == 0.0
        assert shown["totals"]["valueUsd"] == pytest.approx(3000.0)

        # The fast lane no longer reads it; the hourly pass does.
        base_chain.calls.clear()
        await service.sync_all()
        reads = base_chain.balance_reads()
        assert junk not in reads
        service._hidden_sync_at = 0.0
        base_chain.calls.clear()
        await service.sync_all()
        reads = base_chain.balance_reads()
        assert junk in reads

        # Quoting it is meaning it: it comes back, for good.
        await service.quote(chain=BASE, wallet=None, token_in=junk, token_out="ETH", amount_in="1")
        assert any(b["token"]["address"] == junk for b in await service.balances(wallet))
        assert service.hidden_balance_count(wallet) == 0

        # The user's own hide is final: the classifier does not argue.
        token = await service.set_token_hidden(BASE, junk, True)
        assert token["hidden"] is True and token["hiddenBy"] == "user"
        await service.curator.review(BASE, junk, force=True)
        assert service.hidden_balance_count(wallet) == 1
        with pytest.raises(TradingError, match="cannot be hidden"):
            await service.set_token_hidden(BASE, NATIVE_ADDRESS, True)
        assert _events(service, "trading.changed")[-1]["reason"] == "token"

    async def test_unreachable_node_marks_the_chain_failed(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        await service.sync_all()
        base_chain.fail_methods.add("eth_blockNumber")
        base_chain.set_erc20(USDC, wallet, 0)
        await service.sync_all()
        read = {r["chainId"]: r for r in service.chain_reads(wallet)}
        assert read[8453]["status"] == "failed" and "node unavailable" in read[8453]["reason"]
        assert read[4663]["status"] == "ok"
        # Nothing on the failed chain was rewritten from a read that never happened.
        by_token = {b["token"]["address"]: b for b in await service.balances(wallet, 8453)}
        assert by_token[USDC]["amount"] == "1000"

    async def test_indexer_holdings_reach_the_ledger(
        self, funded_service: TradingService, base_chain: FakeChain, fake_indexer: FakeIndexer
    ) -> None:
        """A token the sweep never saw shows up because Blockscout listed it."""
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        hidden = "0x9999000000000000000000000000000000000002"
        base_chain.tokens[hidden] = ("HID", "Hidden", 18)
        base_chain.set_erc20(hidden, wallet, 4 * 10**18)
        fake_indexer.hold(wallet, hidden, 4 * 10**18)
        await service.sync_all()
        # It reached the ledger — and, having no pool anywhere, was judged junk:
        # out of the default view, in the count, back on request.
        assert not any(b["token"]["address"] == hidden for b in await service.balances(wallet))
        assert service.hidden_balance_count(wallet, 8453) == 1
        shown = await service.balances(wallet, 8453, include_hidden=True)
        by_token = {b["token"]["address"]: b for b in shown}
        assert by_token[hidden]["amount"] == "4" and by_token[hidden]["token"]["symbol"] == "HID"
        assert by_token[hidden]["hidden"] is True and by_token[USDC]["hidden"] is False
        # The indexer was asked once (Base only; Robinhood has none), and every
        # number came from the RPC.
        assert len(fake_indexer.requests) == 1
        assert any(c["method"] == "eth_call" for c in base_chain.calls)


class TestQuote:
    async def test_quote_shape_and_guard(
        self, funded_service: TradingService, fake_aggregator: FakeAggregator
    ) -> None:
        service = funded_service
        quote = await service.quote(
            chain=BASE, wallet=None, token_in="USDC", token_out="ETH", amount_in="10"
        )
        assert quote["provider"] == "aggregator"
        # Neither number comes from the aggregator: the engine derives both
        # from its own price feed (see TradingService._priced).
        assert quote["priceImpactPct"] == pytest.approx(0.0)
        assert quote["gasUsd"] == pytest.approx(0.02)  # 0.00001 ETH at $2,000
        assert quote["amountIn"] == "10" and quote["amountInRaw"] == str(10 * 10**6)
        assert quote["amountOut"] == "0.005" and quote["valueUsd"] == pytest.approx(10.0)
        # A rate is an amount, so it is a decimal string like every other one.
        # It used to be a float, which the desktop fed to a string formatter
        # and crashed on, and which lost digits on very small prices.
        assert quote["rate"] == "0.0005"
        assert quote["guard"]["decision"] == "allow"
        # "$5 of ETH": the engine reads the price (ETH = $2,000 in the fake) and sizes it.
        usd_quote = await service.quote(
            chain=BASE, wallet=None, token_in="ETH", token_out="USDC", amount_usd=5
        )
        assert usd_quote["amountIn"] == "0.0025"
        with pytest.raises(TradingError, match="exactly one of amountIn or amountUsd"):
            await service.quote(chain=BASE, wallet=None, token_in="ETH", token_out="USDC")
        with pytest.raises(TradingError, match="exactly one of amountIn or amountUsd"):
            await service.quote(
                chain=BASE,
                wallet=None,
                token_in="ETH",
                token_out="USDC",
                amount_in="1",
                amount_usd=1,
            )
        with pytest.raises(TradingError, match="greater than zero"):
            await service.quote(
                chain=BASE, wallet=None, token_in="ETH", token_out="USDC", amount_usd=0
            )
        unpriced = "0x9999000000000000000000000000000000000099"
        service.ledger.upsert_token(8453, unpriced, symbol="NOPX", name="No price", decimals=18)
        with pytest.raises(TradingError, match="No USD price for NOPX"):
            await service.quote(
                chain=BASE, wallet=None, token_in=unpriced, token_out="USDC", amount_usd=5
            )
        agent_quote = await service.quote(
            chain=BASE,
            wallet=None,
            token_in="USDC",
            token_out="ETH",
            amount_in="250",
            initiator="agent",
        )
        assert agent_quote["guard"]["decision"] == "needs_approval"
        with pytest.raises(TradingError, match="greater than zero"):
            await service.quote(
                chain=BASE, wallet=None, token_in="USDC", token_out="ETH", amount_in="0"
            )
        fake_aggregator.liquidity = False
        with pytest.raises(TradingError) as info:
            await service.quote(
                chain=BASE, wallet=None, token_in="USDC", token_out="ETH", amount_in="1"
            )
        assert info.value.code == "trading.no_route"


class TestSwapFlow:
    async def test_manual_swap_end_to_end(
        self, funded_service: TradingService, base_chain: FakeChain, fake_uniswap: FakeUniswap
    ) -> None:
        """The Uniswap path, end to end — it is the fallback, not the default.

        The aggregator's own end-to-end lives in ``test_aggregator.py``.
        """
        service = funded_service
        service.config.provider = "uniswap"
        wallet = service.test_wallet  # type: ignore[attr-defined]
        fake_uniswap.approval_needed = True
        _wire_swap_effects(base_chain, wallet)
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=0.5,
            initiator="manual",
            session_key=None,
            note="test",
            wait=True,
        )
        order = orders[0]
        assert order["status"] == "confirmed", order
        assert (
            order["approvalTxHash"]
            and order["txHash"]
            and order["explorerUrl"].startswith("https://basescan.org/tx/")
        )
        assert order["receivedOut"] == "0.0005" and order["deliveredToken"]["address"] == WETH
        assert order["valueUsd"] == pytest.approx(10.0) and order["initiator"] == "manual"
        # Two txs went out: approval then swap, both from the wallet, EIP-1559.
        assert len(base_chain.sent) == 2
        swap_tx = decode_fake_raw(base_chain.sent[1])
        assert (
            swap_tx["to"].lower() == ROUTER and swap_tx["type"] == 2 and swap_tx["chainId"] == 8453
        )
        assert swap_tx["nonce"] == 1 and swap_tx["gas"] == 250_000
        # The /swap body is the whole quote response, not a {quote: …} wrapper.
        swap_request = [r for r in fake_uniswap.requests if r.url.path.endswith("/swap")][-1]
        body = json.loads(swap_request.content)
        assert body["routing"] == "CLASSIC" and body["requestId"].startswith("req-")
        assert "permitData" not in body and body["simulateTransaction"] is True and body["deadline"]
        assert (
            json.loads(swap_request.headers["x-agent-info"])["decision_origin"] == "human_mediated"
        )
        # Ledger: swap entry + approval entry, lots moved, manual swaps do not count toward the cap.
        kinds = [e["kind"] for e in service.ledger.list_entries(wallet=wallet)]
        assert "swap" in kinds and "approval" in kinds
        history = service.history(wallet=wallet)["entries"]
        swap_entry = next(e for e in history if e["kind"] == "swap")
        assert (
            swap_entry["amountIn"] == "10"
            and swap_entry["amountOut"] == "0.0005"
            and swap_entry["initiator"] == "manual"
        )
        assert swap_entry["gasUsd"] == pytest.approx(0.02)
        positions = {p.token: p for p in service.ledger.positions(wallet)}
        assert positions[WETH].amount_raw == 5 * 10**14 and positions[
            WETH
        ].cost_usd == pytest.approx(10.0)
        assert service.limits(wallet)["spentTodayUsd"] == 0.0
        finished = _events(service, "trading.order.finished")
        assert finished and finished[-1]["order"]["status"] == "confirmed"

    async def test_confirm_reads_erc20_legs_from_receipt_logs_when_balances_lag(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        """A load-balanced RPC may answer post-swap balances from a node that has
        not seen the block yet; the Transfer logs in the receipt are the truth."""
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        inner = base_chain.on_send

        def lagging_send(raw: str) -> str:
            before_out = base_chain.get_erc20(WETH, wallet)
            tx_hash = inner(raw)
            # The receipt carries the Transfer log, but "latest" balances lag.
            base_chain.set_erc20(WETH, wallet, before_out)
            return tx_hash

        base_chain.on_send = lagging_send
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=0.5,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
        )
        assert orders[0]["status"] == "confirmed"
        assert orders[0]["receivedOut"] == "0.0005"
        positions = {p.token: p for p in service.ledger.positions(wallet)}
        assert positions[WETH].amount_raw == 5 * 10**14

    async def test_agent_swap_within_limits_counts_toward_cap(
        self, funded_service: TradingService, base_chain: FakeChain, fake_uniswap: FakeUniswap
    ) -> None:
        service = funded_service
        service.config.provider = "uniswap"  # this asserts Uniswap's own headers
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="agent",
            session_key="agent:main:x",
            note=None,
            wait=True,
        )
        assert orders[0]["status"] == "confirmed" and orders[0]["sessionKey"] == "agent:main:x"
        assert service.limits(wallet)["spentTodayUsd"] == pytest.approx(10.0)
        quote_request = [r for r in fake_uniswap.requests if r.url.path.endswith("/quote")][-1]
        assert json.loads(quote_request.headers["x-agent-info"]) == {
            "integration_name": "agentos",
            "decision_origin": "autonomous",
            "version": json.loads(quote_request.headers["x-agent-info"])["version"],
        }

    async def test_agent_swap_above_threshold_waits_then_approves(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="250",
            amount_pct=None,
            slippage_pct=None,
            initiator="agent",
            session_key=None,
            note=None,
        )
        order = orders[0]
        assert order["status"] == "awaiting_approval" and "threshold" in order["reason"]
        assert order["expiresAt"] and base_chain.sent == []
        assert (
            _events(service, "trading.approval.requested")[-1]["order"]["orderId"]
            == order["orderId"]
        )
        assert service.list_orders()["pendingApprovals"] == 1
        # Waiting from the agent side returns once the human decides.
        waiter = asyncio.create_task(service.wait_order(order["orderId"], timeout_s=5))
        await asyncio.sleep(0.01)
        approved = await service.approve(order["orderId"], wait=True)
        assert approved["status"] == "confirmed"
        assert (await waiter)["status"] == "confirmed"
        assert service.limits(wallet)["spentTodayUsd"] == pytest.approx(250.0)
        with pytest.raises(TradingError, match="is confirmed"):
            await service.approve(order["orderId"])

    async def test_reject_and_expire(self, funded_service: TradingService) -> None:
        service = funded_service
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="250",
            amount_pct=None,
            slippage_pct=None,
            initiator="agent",
            session_key=None,
            note=None,
        )
        rejected = await service.reject(orders[0]["orderId"], "not now")
        assert rejected["status"] == "rejected" and rejected["reason"] == "user: not now"
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="250",
            amount_pct=None,
            slippage_pct=None,
            initiator="agent",
            session_key=None,
            note=None,
        )
        service._now = lambda: __import__("time").time() + 1000  # past the 900 s TTL
        expired = await service.expire_orders()
        assert [o["orderId"] for o in expired] == [orders[0]["orderId"]]
        assert service.get_order(orders[0]["orderId"])["status"] == "expired"
        with pytest.raises(TradingError, match="is expired"):
            await service.reject(orders[0]["orderId"])

    async def test_daily_cap_rejects_outright(self, funded_service: TradingService) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        service.ledger.add_daily_spend(wallet.lower(), 995.0)
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="agent",
            session_key=None,
            note=None,
        )
        assert orders[0]["status"] == "rejected" and "daily cap" in orders[0]["reason"]

    async def test_batch_one_wallet_fails_others_proceed(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        empty = await service.import_wallet("Empty", private_key="0x" + "33" * 32)
        _wire_swap_effects(base_chain, wallet)
        orders = await service.swap(
            chain=BASE,
            wallets="all",
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
        )
        by_wallet = {o["wallet"]: o for o in orders}
        assert by_wallet[wallet]["status"] == "confirmed"
        assert by_wallet[empty["address"]]["status"] == "failed"
        assert "trading.insufficient_balance" in by_wallet[empty["address"]]["reason"]

    async def test_amount_pct_and_native_gas_reserve(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        fake_aggregator.tx_value = str(10**18 - 10**15)
        _wire_swap_effects(base_chain, wallet, out_token=USDC, out_amount=1990 * 10**6)
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="ETH",
            token_out="USDC",
            amount_in=None,
            amount_pct=100,
            slippage_pct=None,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
        )
        assert orders[0]["status"] == "confirmed"
        assert orders[0]["amountIn"] == "0.999"  # 1 ETH minus the 0.001 gas reserve
        tx = decode_fake_raw(base_chain.sent[-1])
        assert tx["value"] == 10**18 - 10**15

    async def test_reverted_swap_is_failed_with_gas_entry(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet, status=0)
        base_chain.on_send = (lambda inner: lambda raw: _revert(base_chain, inner, raw))(
            base_chain.on_send
        )
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
        )
        assert orders[0]["status"] == "failed" and "reverted" in orders[0]["reason"]
        assert service.ledger.list_entries(wallet=wallet, kind="gas")

    async def test_simulation_failure_blocks_broadcast(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        base_chain.revert_calls = True
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
        )
        assert orders[0]["status"] == "failed" and "simulation" in orders[0]["reason"]
        assert base_chain.sent == []

    async def test_weth_delivered_instead_of_eth_and_unwrap(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet, out_token=WETH, out_amount=5 * 10**14)
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="ETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
        )
        order = orders[0]
        assert order["status"] == "confirmed"
        assert order["tokenOut"]["native"] is True
        assert order["deliveredToken"]["address"] == WETH and order["receivedOut"] == "0.0005"
        positions = {p.token: p for p in service.ledger.positions(wallet)}
        assert positions[WETH].amount_raw == 5 * 10**14

        def unwrap_send(raw: str) -> str:
            tx = decode_fake_raw(raw)
            assert tx["to"].lower() == WETH and tx["data"].startswith("0x2e1a7d4d")
            amount = int(tx["data"][10:], 16)
            base_chain.set_erc20(WETH, wallet, base_chain.get_erc20(WETH, wallet) - amount)
            base_chain.native[wallet.lower()] += amount - 100_000 * 10**8
            tx_hash = "0x" + "77" * 32
            base_chain.receipt(tx_hash, status=1)
            return tx_hash

        base_chain.on_send = unwrap_send
        result = await service.unwrap(BASE, None)
        assert result["txHash"] == "0x" + "77" * 32 and result["amount"] == "0.0005"
        positions = {p.token: p for p in service.ledger.positions(wallet)}
        assert WETH not in positions
        assert (
            positions[NATIVE_ADDRESS].amount_raw == 10**18 + 5 * 10**14
        )  # opening lot + unwrapped
        assert positions[NATIVE_ADDRESS].cost_usd == pytest.approx(2000.0 + 10.0)
        assert service.ledger.list_entries(wallet=wallet, kind="unwrap")
        with pytest.raises(TradingError, match="nothing to unwrap"):
            await service.unwrap(BASE, None)

    async def test_swap_validation_errors(self, funded_service: TradingService) -> None:
        service = funded_service
        common = dict(
            chain=BASE,
            wallets=None,
            slippage_pct=None,
            initiator="manual",
            session_key=None,
            note=None,
        )
        with pytest.raises(TradingError, match="same token"):
            await service.swap(
                token_in="USDC", token_out=USDC, amount_in="1", amount_pct=None, **common
            )
        with pytest.raises(TradingError, match="amountIn, amountPct or amountUsd"):
            await service.swap(
                token_in="USDC", token_out="WETH", amount_in=None, amount_pct=None, **common
            )
        with pytest.raises(TradingError, match="between 0 and 100"):
            await service.swap(
                token_in="USDC", token_out="WETH", amount_in=None, amount_pct=150, **common
            )
        # Only Uniswap needs a key; the default route has none to lose.
        service.config.provider = "uniswap"
        service.config.uniswap_api_key = ""
        with pytest.raises(TradingError, match="No Uniswap API key"):
            await service.swap(
                token_in="USDC", token_out="WETH", amount_in="1", amount_pct=None, **common
            )
        service.config.uniswap_api_key = "test-key"
        service.vault.lock()
        service.vault.unlock_path.unlink()
        with pytest.raises(TradingError, match="locked"):
            await service.swap(
                token_in="USDC", token_out="WETH", amount_in="1", amount_pct=None, **common
            )

    async def test_chart_short_range_is_served_from_local_snapshots(
        self, funded_service: TradingService, fake_prices: FakePrices
    ) -> None:
        """The cheap path: 1h/6h/1d read sqlite and never ask GeckoTerminal."""
        service = funded_service
        fake_prices.candles = [[1, 1, 2, 0.5, 1.5, 9]]
        now = service._now()
        for i in range(40):
            service.ledger.add_snapshot(BASE.chain_id, WETH, now - 3_000 + i * 60, 100.0 + i)

        before = len([r for r in fake_prices.requests if "geckoterminal" in str(r.url)])
        chart = await service.chart(BASE, WETH, "1h")
        after = len([r for r in fake_prices.requests if "geckoterminal" in str(r.url)])

        assert chart["source"] == "snapshots"
        assert after == before, "a local-first range must not hit GeckoTerminal"
        assert chart["points"][-1]["c"] == 139.0
        # Change is measured across the range that was asked for, not a fixed 24h.
        assert chart["stats"]["changePct"] == pytest.approx(39.0)

    async def test_chart_thins_a_dense_snapshot_run(self, funded_service: TradingService) -> None:
        """A day of 60-second snapshots is ~1,440 points; the wire carries 180."""
        service = funded_service
        now = service._now()
        for i in range(2_000):
            service.ledger.add_snapshot(BASE.chain_id, WETH, now - 80_000 + i * 40, float(i))

        chart = await service.chart(BASE, WETH, "1d")
        assert chart["source"] == "snapshots"
        assert len(chart["points"]) == 180
        # Striding keeps the ends, so the range change stays honest.
        assert chart["points"][0]["c"] == 0.0
        assert chart["points"][-1]["c"] == 1_999.0

    async def test_chart_and_lot_cost_override(
        self, funded_service: TradingService, fake_prices: FakePrices
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        fake_prices.candles = [[1, 1, 2, 0.5, 1.5, 9]]
        # No local history yet, so even a local-first range reaches for candles
        # — and reduces them to closes, because the desk draws a line.
        chart = await service.chart(BASE, WETH, "1d")
        assert chart["source"] == "geckoterminal"
        assert chart["points"][0] == {"t": 1.0, "c": 1.5}
        # The strip's figures come out of the same price response.
        assert chart["stats"]["marketCapUsd"] is not None
        assert chart["stats"]["market"] == "Uniswap v4"
        assert chart["stats"]["quoteSymbol"] == "USDC"
        await service.sync_all()
        snaps = await service.chart(ROBINHOOD, AAPL, "1w")
        assert snaps["source"] == "snapshots"
        entry = next(
            e
            for e in service.history(wallet=wallet, kind="deposit")["entries"]
            if e["tokenOut"]["native"]
        )
        repriced = service.set_lot_cost(entry["id"], 1234.0)
        assert repriced["costBasisSource"] == "manual" and repriced["valueUsd"] == pytest.approx(
            1234.0
        )
        portfolio = await service.portfolio(wallet)
        eth = next(h for h in portfolio["holdings"] if h["token"]["address"] == NATIVE_ADDRESS)
        assert eth["costUsd"] == pytest.approx(1234.0)


def _revert(chain: FakeChain, inner, raw: str) -> str:
    tx = decode_fake_raw(raw)
    if tx["to"].lower() in SWAP_TARGETS:
        chain._seq += 1
        tx_hash = "0x" + format(0xBAD000 + chain._seq, "x").rjust(64, "0")
        chain.native[tx["from"].lower() if "from" in tx else next(iter(chain.native))] -= (
            100_000 * 10**8
        )
        chain.receipt(tx_hash, status=0)
        return tx_hash
    return inner(raw)


class TestBackgroundLoop:
    async def test_tick_and_stop(self, funded_service: TradingService) -> None:
        service = funded_service
        service._background = True
        service.ensure_started()
        assert service._task is not None
        await service.tick()
        assert service.last_sync_at is not None
        await service.stop()
        assert service._task is None

    async def test_housekeeping_keeps_its_clock_while_a_sync_runs(
        self, funded_service: TradingService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wallet sync that takes minutes must not hold the DCA buys and trigger checks.

        Seen 2026-11-04: a wallet four days behind on one chain synced for
        eight minutes, and two due DCA mandates waited the whole time.
        """
        service = funded_service
        service._background = True
        real = service.config

        class Quick:  # the real config, with the loop's clock sped up
            sync_interval_seconds = 0.01

            def __getattr__(self, name: str) -> object:
                return getattr(real, name)

        monkeypatch.setattr(service, "config", Quick())
        passes = 0
        sync_started = asyncio.Event()
        sync_release = asyncio.Event()

        async def slow_sync(**_: object) -> int:
            sync_started.set()
            await sync_release.wait()
            return 0

        async def count_pass() -> None:
            nonlocal passes
            passes += 1

        monkeypatch.setattr(service, "sync_all", slow_sync)
        monkeypatch.setattr(service, "dca_run_due", count_pass)
        service.ensure_started()
        try:
            await asyncio.wait_for(sync_started.wait(), 2)
            first = passes
            # The sync is still running; housekeeping goes on ticking beside it.
            await asyncio.sleep(0.1)
            assert passes > first + 2
            # And only one sync pass is in flight, however many ticks went by.
            assert service._sync_task is not None and not service._sync_task.done()
        finally:
            sync_release.set()
            await service.stop()
        assert service._sync_task is None


class TestRealSigning:
    def test_sign_tx_recovers_sender(self) -> None:
        from eth_account import Account

        from agentos.trading.service import _sign_permit, _sign_tx

        key = bytes.fromhex("11" * 32)
        raw = _sign_tx(
            {
                "chainId": 8453,
                "nonce": 0,
                "to": "0x" + "22" * 20,
                "value": 1,
                "data": "0x",
                "gas": 21_000,
                "maxFeePerGas": 10**9,
                "maxPriorityFeePerGas": 10**6,
                "type": 2,
            },
            key,
        )
        assert raw.startswith("0x")
        assert Account.recover_transaction(raw) == Account.from_key(key).address
        permit = {
            "domain": {"name": "Permit2", "chainId": 8453, "verifyingContract": "0x" + "33" * 20},
            "types": {
                "EIP712Domain": [{"name": "name", "type": "string"}],
                "PermitSingle": [
                    {"name": "spender", "type": "address"},
                    {"name": "nonce", "type": "uint256"},
                ],
            },
            "values": {"spender": "0x" + "44" * 20, "nonce": 1},
        }
        assert _sign_permit(permit, key).startswith("0x") and len(_sign_permit(permit, key)) == 132


class TestSafetyRails:
    """The engine-side rails that do not depend on what the prompt says."""

    async def _park(self, service: TradingService, amount: str = "250") -> dict:
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in=amount,
            amount_pct=None,
            slippage_pct=None,
            initiator="agent",
            session_key=None,
            note=None,
        )
        assert orders[0]["status"] == "awaiting_approval", orders[0]
        return orders[0]

    async def test_two_concurrent_approvals_send_one_transaction(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        order = await self._park(service)
        results = await asyncio.gather(
            service.approve(order["orderId"], wait=True),
            service.approve(order["orderId"], wait=True),
            return_exceptions=True,
        )
        confirmed = [r for r in results if isinstance(r, dict) and r["status"] == "confirmed"]
        refused = [r for r in results if isinstance(r, TradingError)]
        assert len(confirmed) == 1 and len(refused) == 1
        # The loser is told the order moved on, whichever check caught it.
        assert "no longer awaiting approval" in str(refused[0]) or "is confirmed" in str(refused[0])
        # Exactly one swap went out (no approval tx: USDC was pre-approved).
        assert len(base_chain.sent) == 1
        assert service.limits(wallet)["spentTodayUsd"] == pytest.approx(250.0)

    async def test_reject_racing_approve_loses_cleanly(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        order = await self._park(service)
        approved = await service.approve(order["orderId"], wait=True)
        assert approved["status"] == "confirmed"
        with pytest.raises(TradingError, match="is confirmed"):
            await service.reject(order["orderId"])

    async def test_expiry_never_flips_an_approved_order(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        order = await self._park(service)
        approved = await service.approve(order["orderId"], wait=True)
        assert approved["status"] == "confirmed"
        service._now = lambda: __import__("time").time() + 1000
        assert await service.expire_orders() == []
        assert service.get_order(order["orderId"])["status"] == "confirmed"

    async def test_open_orders_count_toward_the_cap(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        # 950 USD parks (above the threshold) and is now in flight.
        await self._park(service, "950")
        # Reported as committed already: the guard and the screen agree.
        assert service.limits(wallet)["spentTodayUsd"] == pytest.approx(950.0)
        # A burst cannot slip under the cap by racing its own confirmations.
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="60",
            amount_pct=None,
            slippage_pct=None,
            initiator="agent",
            session_key=None,
            note=None,
        )
        assert orders[0]["status"] == "rejected" and "daily cap" in orders[0]["reason"]
        # Manual orders are the user's own and are not capped.
        manual = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="60",
            amount_pct=None,
            slippage_pct=None,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
        )
        assert manual[0]["status"] == "confirmed"

    async def test_agent_slippage_above_ceiling_is_refused(
        self, funded_service: TradingService
    ) -> None:
        service = funded_service
        with pytest.raises(TradingError, match="slippage") as exc:
            await service.quote(
                chain=BASE,
                wallet=None,
                token_in="USDC",
                token_out="WETH",
                amount_in="10",
                slippage_pct=12.0,
                initiator="agent",
            )
        assert exc.value.code == "trading.slippage_too_high"
        with pytest.raises(TradingError, match="slippage"):
            await service.swap(
                chain=BASE,
                wallets=None,
                token_in="USDC",
                token_out="WETH",
                amount_in="10",
                amount_pct=None,
                slippage_pct=12.0,
                initiator="agent",
                session_key=None,
                note=None,
            )
        assert service.list_orders()["orders"] == []
        # The user may ask for it.
        quote = await service.quote(
            chain=BASE,
            wallet=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            slippage_pct=12.0,
            initiator="manual",
        )
        assert quote["guard"]["decision"] == "allow"

    async def test_price_impact_above_ceiling_parks_an_agent_order(
        self, funded_service: TradingService, fake_aggregator: FakeAggregator
    ) -> None:
        service = funded_service
        # The aggregator publishes no impact figure, so the engine derives it:
        # $10 of USDC in, 0.004625 WETH ($9.25) out is a 7.5% haircut.
        fake_aggregator.amount_out = 4_625_000_000_000_000
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="agent",
            session_key=None,
            note=None,
        )
        assert orders[0]["status"] == "awaiting_approval"
        assert "impact" in orders[0]["reason"]

    async def test_zero_cap_switches_agent_swaps_off(self, funded_service: TradingService) -> None:
        service = funded_service
        service.config.daily_cap_usd = 0.0
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="1",
            amount_pct=None,
            slippage_pct=None,
            initiator="agent",
            session_key=None,
            note=None,
        )
        assert orders[0]["status"] == "rejected" and "switched off" in orders[0]["reason"]

    async def test_approval_to_an_untrusted_spender_is_never_signed(
        self, funded_service: TradingService, base_chain: FakeChain, fake_uniswap: FakeUniswap
    ) -> None:
        service = funded_service
        # Uniswap's approval is a separate call, so a rogue spender reaches
        # this service-level check. The aggregator refuses one layer earlier,
        # in the provider — see test_aggregator.py.
        service.config.provider = "uniswap"
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        fake_uniswap.approval_needed = True
        fake_uniswap.approval_spender = "0x000000000000000000000000000000000000dEaD"
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
        )
        assert orders[0]["status"] == "failed" and "not trusted" in orders[0]["reason"]
        assert base_chain.sent == []

    def test_approval_calldata_checks(self) -> None:
        from agentos.trading.providers import PERMIT2
        from tests.test_trading.fakes import approve_calldata

        check = TradingService._check_approval_tx
        spenders = frozenset({PERMIT2.lower()})
        ok = {"to": USDC, "data": approve_calldata(PERMIT2, 10), "value": "0"}
        assert check(ok, token=USDC, amount_raw=10, spenders=spenders) == PERMIT2.lower()
        # An unlimited allowance used to be waved through as "conventional";
        # the desk now signs exactly the order's amount and nothing else.
        unlimited = {"to": USDC, "data": approve_calldata(PERMIT2, 2**256 - 1), "value": "0"}
        with pytest.raises(TradingError, match="exceeds"):
            check(unlimited, token=USDC, amount_raw=10, spenders=spenders)
        with pytest.raises(TradingError, match="exceeds"):
            check(
                {"to": USDC, "data": approve_calldata(PERMIT2, 11), "value": "0"},
                token=USDC,
                amount_raw=10,
                spenders=spenders,
            )
        with pytest.raises(TradingError, match="not on the token"):
            check({**ok, "to": WETH}, token=USDC, amount_raw=10, spenders=spenders)
        with pytest.raises(TradingError, match="native value"):
            check({**ok, "value": "1"}, token=USDC, amount_raw=10, spenders=spenders)
        with pytest.raises(TradingError, match="approve\\(address,uint256\\)"):
            check({**ok, "data": "0xdeadbeef"}, token=USDC, amount_raw=10, spenders=spenders)

    async def test_swap_transaction_value_must_match_the_order(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        # An ERC-20 sale whose transaction suddenly asks for ETH on top.
        fake_aggregator.tx_value = str(10**17)
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
        )
        assert orders[0]["status"] == "failed" and "carries" in orders[0]["reason"]
        assert base_chain.sent == []

    def test_swap_transaction_envelope_checks(self, funded_service: TradingService) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        record = service.vault.get(wallet)
        from agentos.trading.prices import TokenMeta

        usdc = TokenMeta(8453, USDC, "USDC", "USD Coin", 6)
        eth = TokenMeta(8453, NATIVE_ADDRESS, "ETH", "Ether", 18, native=True)
        base = {"from": wallet, "to": ROUTER, "value": "0", "chainId": 8453}
        targets = frozenset({ROUTER})

        def check(tx: dict, *, meta_in=usdc, amount_raw: int = 10, targets=targets) -> None:
            service._check_swap_tx(
                tx,
                chain=BASE,
                record=record,
                meta_in=meta_in,
                amount_raw=amount_raw,
                provider="uniswap",
                targets=targets,
            )

        check(base)
        check({**base, "value": str(10**15)}, meta_in=eth, amount_raw=10**15)
        with pytest.raises(TradingError, match="wei"):
            check({**base, "value": str(10**15 + 1)}, meta_in=eth, amount_raw=10**15)
        with pytest.raises(TradingError, match="chain"):
            check({**base, "chainId": 1})
        with pytest.raises(TradingError, match="not from this wallet"):
            check({**base, "from": ROUTER})
        with pytest.raises(TradingError, match="implausible"):
            check({**base, "to": USDC})
        # The target is pinned per provider and chain; anything else, and
        # a chain with nothing pinned, is refused — checksummed or not.
        with pytest.raises(TradingError, match="not a contract this desk trusts"):
            check({**base, "to": OTHER})
        with pytest.raises(TradingError, match="no swap contract is pinned"):
            check(base, targets=frozenset())
        check({**base, "to": ROUTER.upper().replace("0X", "0x")})

    async def test_submitted_order_is_recovered_after_a_restart(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        # Send for real, then pretend the process died before confirming:
        # forget the confirm task and roll the row back to ``submitted``.
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="agent",
            session_key=None,
            note=None,
            wait=True,
        )
        order_id = orders[0]["orderId"]
        assert orders[0]["status"] == "confirmed"
        service.ledger.update_order(
            order_id, status="submitted", received_out_raw=None, spent_in_raw=None
        )
        # A process that died before confirming never counted the spend.
        service.ledger.add_daily_spend(wallet, -10.0)
        assert service.limits(wallet)["spentTodayUsd"] == pytest.approx(10.0)  # in flight
        await service.recover_submitted()
        recovered = service.get_order(order_id)
        assert recovered["status"] == "confirmed"
        assert recovered["receivedOut"] == "0.0005" and recovered["txHash"] == orders[0]["txHash"]
        # Settled from the receipt: legs booked once, spend counted once.
        assert service.limits(wallet)["spentTodayUsd"] == pytest.approx(10.0)
        swaps = [e for e in service.ledger.list_entries(wallet=wallet) if e["kind"] == "swap"]
        assert len(swaps) == 1

    async def test_no_receipt_keeps_the_order_submitted(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        # The node accepts the broadcast but never produces a receipt.
        base_chain.on_send = lambda raw: "0x" + "ab" * 32  # type: ignore[attr-defined]
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="manual",
            session_key=None,
            note=None,
        )
        assert orders[0]["status"] == "submitted"
        await service._confirm(orders[0]["orderId"], orders[0]["txHash"], None, timeout_s=0.01)
        order = service.get_order(orders[0]["orderId"])
        assert order["status"] == "submitted" and "mined" in (order["reason"] or "")
        # Six hours later with still nothing, it is given up as dropped.
        service._now = lambda: __import__("time").time() + 7 * 3600
        await service._confirm(orders[0]["orderId"], orders[0]["txHash"], None, timeout_s=0.01)
        assert service.get_order(orders[0]["orderId"])["status"] == "failed"
        assert service.get_order(orders[0]["orderId"])["reason"].startswith("transaction never")
        assert wallet


class TestSettlementSurvivesTheNode:
    """Once a swap is mined, nothing the RPC does afterwards may un-mine it."""

    async def test_an_error_after_the_receipt_leaves_the_order_submitted(
        self, funded_service: TradingService, base_chain: FakeChain, monkeypatch
    ) -> None:
        """A transient error while settling must not flip a mined swap to failed.

        Measured live: a dRPC hiccup right after the receipt marked a
        successful swap ``failed``; ``recover_submitted`` never looks at
        failed rows, so the legs were never booked and the agent's daily
        spend never counted. The row stays ``submitted`` and the next pass
        finishes the job.
        """
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        # Deliver what the aggregator quoted, so the recovered row carries
        # no short-fill note and its reason is genuinely None.
        _wire_swap_effects(base_chain, wallet, out_amount=5 * 10**15)
        real_settle = service._settle
        blown = 0

        async def settle_once_broken(*args, **kwargs):
            nonlocal blown
            if blown == 0:
                blown += 1
                raise service_module.EvmTransportError("node hiccup after the receipt")
            return await real_settle(*args, **kwargs)

        monkeypatch.setattr(service, "_settle", settle_once_broken)
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="agent",
            session_key=None,
            note=None,
            wait=True,
        )
        order = orders[0]
        assert order["status"] == "submitted", order
        assert order["txHash"] and (order["reason"] or "").startswith("settling:")
        assert not [
            e
            for e in _events(service, "trading.order.finished")
            if e["order"]["status"] == "failed"
        ]
        assert service.limits(wallet)["spentTodayUsd"] == pytest.approx(10.0)  # still in flight
        # A later housekeeping pass owns the stray and books it once.
        await service.recover_submitted()
        recovered = service.get_order(order["orderId"])
        assert recovered["status"] == "confirmed" and recovered["receivedOut"] == "0.005"
        assert recovered["reason"] is None
        swaps = [e for e in service.ledger.list_entries(wallet=wallet) if e["kind"] == "swap"]
        assert len(swaps) == 1
        assert service.limits(wallet)["spentTodayUsd"] == pytest.approx(10.0)  # counted once

    async def test_post_receipt_reads_that_fail_are_worked_around(
        self, funded_service: TradingService, base_chain: FakeChain, monkeypatch
    ) -> None:
        """The block timestamp and the balance cache reads are conveniences.

        A node that refuses them after the receipt costs a stale cache row
        (the next sync fixes it) and a booking stamped "now" — never a
        failed order.
        """
        monkeypatch.setattr(service_module, "CHAIN_CATCHUP_POLL_S", 0.0)
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        inner = base_chain.on_send

        def send_then_outage(raw: str) -> str:
            tx_hash = inner(raw)
            base_chain.fail_methods.update({"eth_getBlockByNumber", "eth_call"})
            return tx_hash

        base_chain.on_send = send_then_outage
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
        )
        assert orders[0]["status"] == "confirmed", orders[0]
        assert orders[0]["receivedOut"] == "0.0005"  # from the receipt's Transfer log

    async def test_native_out_swap_is_recovered_after_a_restart(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
    ) -> None:
        """A swap into ETH settled by a recovery pass has no pre-send snapshot.

        With nothing to diff against, the old code booked ``received = 0``.
        There is no Transfer log for the gas coin, so the balance is read
        pinned to the receipt's block and the block before it instead.
        """
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        received = 20_000_000_000_000  # 0.00002 ETH
        fake_aggregator.amount_out = received
        _wire_swap_effects(base_chain, wallet, out_token=NATIVE_ADDRESS, out_amount=received)
        before_swap = base_chain.native.get(wallet.lower(), 0)
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="ETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
        )
        order_id = orders[0]["orderId"]
        assert orders[0]["status"] == "confirmed" and orders[0]["receivedOut"] == "0.00002"
        # The process died before confirming: forget the settlement.
        service.ledger.update_order(
            order_id, status="submitted", received_out_raw=None, spent_in_raw=None
        )
        mined_block = base_chain.block
        asked: list[str] = []

        def balance_at(address: str, block: str) -> int:
            asked.append(block)
            if address == wallet.lower() and block == hex(mined_block - 1):
                return before_swap
            return base_chain.native.get(address, 0)

        base_chain.balance_at = balance_at
        await service.recover_submitted()
        recovered = service.get_order(order_id)
        assert recovered["status"] == "confirmed", recovered
        assert recovered["receivedOut"] == "0.00002"
        assert hex(mined_block - 1) in asked and hex(mined_block) in asked

    async def test_native_out_recovery_on_a_lagging_node_books_the_expected_amount(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
    ) -> None:
        """Pinned reads that still show nothing arrived are not believed either.

        A swap that mined without reverting delivered at least ``minOut``;
        below that the read is stale, and the order's expected amount is
        booked (with a warning) rather than a zero.
        """
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        received = 20_000_000_000_000
        fake_aggregator.amount_out = received
        _wire_swap_effects(base_chain, wallet, out_token=NATIVE_ADDRESS, out_amount=received)
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="ETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
        )
        order_id = orders[0]["orderId"]
        service.ledger.update_order(
            order_id, status="submitted", received_out_raw=None, spent_in_raw=None
        )
        # Every read, pinned or not, answers from before the swap's block.
        frozen = base_chain.native.get(wallet.lower(), 0) - received
        base_chain.balance_at = lambda address, block: frozen
        await service.recover_submitted()
        recovered = service.get_order(order_id)
        assert recovered["status"] == "confirmed"
        assert recovered["receivedOut"] == recovered["expectedOut"] == "0.00002"

    async def test_wait_order_does_not_return_a_quoted_row(
        self, funded_service: TradingService
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        now = service._now()
        service.ledger.insert_order(
            {
                "order_id": "ord_waiting",
                "created_at": now,
                "updated_at": now,
                "chain_id": 8453,
                "wallet": wallet.lower(),
                "token_in": USDC,
                "token_out": WETH,
                "amount_raw": "1",
                "amount_human": "0.000001",
                "status": "quoted",
                "initiator": "manual",
            }
        )

        async def finish_later() -> None:
            await asyncio.sleep(0.05)
            service.ledger.update_order("ord_waiting", status="failed", reason="x")
            service._wake("ord_waiting")

        task = asyncio.get_running_loop().create_task(finish_later())
        order = await service.wait_order("ord_waiting", timeout_s=5)
        await task
        assert order["status"] == "failed"

    async def test_full_allowance_rescan_stops_the_running_pass_first(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        record = service.vault.resolve(wallet)
        key = (BASE.chain_id, record.key)
        service.ledger.set_allowance_scan(BASE.chain_id, record.key, last_block=50)

        async def never_done() -> None:
            await asyncio.sleep(3600)

        stale = asyncio.get_running_loop().create_task(never_done())
        service._allowance_scans[key] = stale
        result = await service.allowances(BASE, wallet, full=True, wait=True)
        assert stale.cancelled()
        assert service._allowance_scans[key] is not stale
        assert result["scannedTo"] == base_chain.block


class TestUnsellableHoldings:
    async def test_an_unverified_token_in_a_dry_pool_is_not_valued(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_prices: FakePrices,
        monkeypatch,
    ) -> None:
        """Live case: an airdropped SEED lookalike priced at $0.678 by a pool
        holding five cents made a fifty-cent wallet show +$12 unrealised.
        Below the spam liquidity floor an unlisted token is shown unpriced."""
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        lookalike = "0x350cc56c00000000000000000000000000000001"
        base_chain.tokens[lookalike] = ("SEED", "Seed", 18)
        base_chain.set_erc20(lookalike, wallet, 18 * 10**18)
        base_chain.add_transfer(token=lookalike, sender=OTHER, recipient=wallet, amount=18 * 10**18)
        fake_prices.spot[("base", lookalike)] = 0.678
        liquidity = {"usd": 0.05}
        real_pair = fake_prices._pair

        def thin_pair(slug: str, token: str, price: float) -> dict:
            pair = real_pair(slug, token, price)
            if token == lookalike:
                pair["liquidity"] = dict(liquidity)
            return pair

        monkeypatch.setattr(fake_prices, "_pair", thin_pair)
        await service.sync_all()
        # The user deliberately shows it: still not worth $12.
        await service.set_token_hidden(BASE, lookalike, False)
        portfolio = await service.portfolio(wallet)
        row = next(h for h in portfolio["holdings"] if h["token"]["address"] == lookalike)
        assert row["amount"] == "18" and row["token"]["verified"] is False
        assert row["priceUsd"] is None and row["valueUsd"] is None
        assert row["unrealizedUsd"] is None and row["unrealizedPct"] is None
        assert portfolio["totals"]["valueUsd"] == pytest.approx(3000.0)
        # Its opening lot was booked at the same fictional price: neither a
        # +$12 gain nor a -$12 loss reaches the hero totals.
        assert portfolio["totals"]["costUsd"] == pytest.approx(3000.0)
        assert portfolio["totals"]["unrealizedUsd"] == pytest.approx(0.0)
        balance = next(
            b for b in await service.balances(wallet) if b["token"]["address"] == lookalike
        )
        assert balance["priceUsd"] is None and balance["valueUsd"] is None
        # A real pool behind the price makes it a value again.
        liquidity["usd"] = 250_000.0
        portfolio = await service.portfolio(wallet)
        row = next(h for h in portfolio["holdings"] if h["token"]["address"] == lookalike)
        assert row["priceUsd"] == pytest.approx(0.678)
        assert row["valueUsd"] == pytest.approx(18 * 0.678)
        # Listed tokens keep their price whatever pool the source answers with.
        liquidity["usd"] = 0.05
        by_token = {b["token"]["address"]: b for b in await service.balances(wallet)}
        assert by_token[USDC]["valueUsd"] == pytest.approx(1000.0)


class TestUnwrapGuards:
    async def test_unwrap_respects_the_enabled_switch_and_the_wallet_lock(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        base_chain.set_erc20(WETH, wallet, 5 * 10**14)
        service.config.enabled = False
        with pytest.raises(TradingError, match="disabled"):
            await service.unwrap(BASE, None)
        service.config.enabled = True
        record = service.vault.resolve(wallet)
        lock = service._wallet_lock(record.key)
        held_during_send: list[bool] = []

        def unwrap_send(raw: str) -> str:
            held_during_send.append(lock.locked())
            tx = decode_fake_raw(raw)
            amount = int(tx["data"][10:], 16)
            base_chain.set_erc20(WETH, wallet, base_chain.get_erc20(WETH, wallet) - amount)
            base_chain.native[wallet.lower()] += amount - 100_000 * 10**8
            tx_hash = "0x" + "78" * 32
            base_chain.receipt(tx_hash, status=1)
            return tx_hash

        base_chain.on_send = unwrap_send
        result = await service.unwrap(BASE, None)
        assert result["amount"] == "0.0005" and held_during_send == [True]
        assert not lock.locked()
        cached = service.ledger.get_balance(BASE.chain_id, record.key, NATIVE_ADDRESS)
        assert cached == base_chain.native[wallet.lower()]


class TestClientQuoteIsTheExecutedQuote:
    """A1: the number the user confirmed is the number that is enforced."""

    async def _swap(self, service: TradingService, **extra):
        return await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            session_key=None,
            note=None,
            wait=True,
            **extra,
        )

    async def test_manual_requote_far_under_the_confirmed_quote_is_refused(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        # The client confirmed ~3% more than the engine now quotes; with the
        # aggregator's 0.5% slippage the floor is 1% under.
        confirmed = fake_aggregator.amount_out * 100 // 97
        orders = await self._swap(
            service,
            initiator="manual",
            expected_out_raw=confirmed,
            min_out_raw=confirmed * 995 // 1000,
            quote_id="q-desktop-1",
        )
        assert orders[0]["status"] == "failed"
        assert "trading.price_moved" in orders[0]["reason"]
        assert "since you confirmed" in orders[0]["reason"]
        assert "quote again" in orders[0]["reason"]
        assert base_chain.sent == []
        stored = json.loads(service.ledger.get_order(orders[0]["orderId"])["quote_json"])
        assert stored == {
            "quoteId": "q-desktop-1",
            "expectedOutRaw": str(confirmed),
            "minOutRaw": str(confirmed * 995 // 1000),
        }

    async def test_manual_requote_within_the_floor_executes_on_the_engine_quote(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        confirmed = fake_aggregator.amount_out * 1005 // 1000  # 0.5% over: inside 2x slippage
        orders = await self._swap(service, initiator="manual", expected_out_raw=confirmed)
        assert orders[0]["status"] == "confirmed"
        row = service.ledger.get_order(orders[0]["orderId"])
        # The engine's own quote is what was recorded and signed, not the client's.
        assert row["expected_out_raw"] == str(fake_aggregator.amount_out)
        assert json.loads(row["quote_json"])["expectedOutRaw"] == str(confirmed)

    async def test_agent_requote_far_under_the_confirmed_quote_is_parked(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        confirmed = fake_aggregator.amount_out * 100 // 97
        orders = await self._swap(service, initiator="agent", expected_out_raw=confirmed)
        order = orders[0]
        assert order["status"] == "awaiting_approval"
        assert "since you confirmed" in order["reason"]
        assert order["expiresAt"] and base_chain.sent == []
        assert (
            _events(service, "trading.approval.requested")[-1]["order"]["orderId"]
            == order["orderId"]
        )

    async def test_without_a_client_quote_nothing_changes(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        orders = await self._swap(service, initiator="manual")
        assert orders[0]["status"] == "confirmed"
        assert service.ledger.get_order(orders[0]["orderId"])["quote_json"] is None

    async def test_negative_client_quote_is_refused(self, funded_service: TradingService) -> None:
        with pytest.raises(TradingError, match="expectedOutRaw must not be negative"):
            await self._swap(funded_service, initiator="manual", expected_out_raw=-1)

    async def test_quote_dict_carries_min_out_raw(
        self, funded_service: TradingService, fake_aggregator: FakeAggregator
    ) -> None:
        quote = await funded_service.quote(
            chain=BASE, wallet=None, token_in="USDC", token_out="WETH", amount_in="10"
        )
        assert quote["minOutRaw"] == str(fake_aggregator.amount_out * 995 // 1000)
        assert quote["amountOutRaw"] == str(fake_aggregator.amount_out)


class TestFloorAfterBuildRequote:
    """A11: a provider that re-quotes inside ``build`` is judged by the same floor."""

    async def test_build_requote_below_the_floor_is_refused(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        monkeypatch,
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        real_build = service_module.AggregatorProvider.build

        async def build_with_a_worse_quote(self, quote, **kwargs):
            quote.amount_out_raw = quote.amount_out_raw * 95 // 100
            quote.min_out_raw = quote.min_out_raw * 95 // 100
            quote.fetched_at = quote.fetched_at + 1.0
            return await real_build(self, quote, **kwargs)

        monkeypatch.setattr(service_module.AggregatorProvider, "build", build_with_a_worse_quote)
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
        )
        assert orders[0]["status"] == "failed"
        assert "trading.price_moved" in orders[0]["reason"]
        assert base_chain.sent == []

    async def test_build_requote_within_the_floor_rewrites_the_row(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
        monkeypatch,
    ) -> None:
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        _wire_swap_effects(base_chain, wallet)
        real_build = service_module.AggregatorProvider.build
        slightly_less = fake_aggregator.amount_out * 998 // 1000

        async def build_with_a_slightly_worse_quote(self, quote, **kwargs):
            quote.amount_out_raw = slightly_less
            quote.fetched_at = quote.fetched_at + 1.0
            return await real_build(self, quote, **kwargs)

        monkeypatch.setattr(
            service_module.AggregatorProvider, "build", build_with_a_slightly_worse_quote
        )
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
        )
        assert orders[0]["status"] == "confirmed"
        assert service.ledger.get_order(orders[0]["orderId"])["expected_out_raw"] == str(
            slightly_less
        )


class TestPortfolioTotals:
    LISTED = "0x5555000000000000000000000000000000000055"

    def _list(self, fake_prices: FakePrices, base_chain: FakeChain) -> None:
        base_chain.tokens[self.LISTED] = ("LST", "Listed Coin", 18)
        fake_prices.lists["base"].append(
            {
                "chainId": 8453,
                "address": self.LISTED,
                "symbol": "LST",
                "name": "Listed Coin",
                "decimals": 18,
            }
        )

    async def test_realized_pnl_survives_closing_the_position(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
        fake_prices: FakePrices,
    ) -> None:
        """A3: sell every last USDC; it leaves the holdings, not the realised total."""
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        await service.sync_all()  # opening lots at spot: 1,000 USDC costs $1,000
        fake_prices.spot[("base", USDC)] = 1.2  # then USDC "rallies": selling it all is +$200
        out_amount = 6 * 10**17  # 0.6 WETH = $1,200
        fake_aggregator.amount_out = out_amount

        def on_send(raw: str) -> str:
            tx = decode_fake_raw(raw)
            base_chain._seq += 1
            tx_hash = "0x" + format(0xFEED00 + base_chain._seq, "x").rjust(64, "0")
            base_chain.nonces[wallet.lower()] = tx.get("nonce", 0) + 1
            base_chain.native[wallet.lower()] -= 100_000 * 10**8
            if tx["to"].lower() in SWAP_TARGETS:
                base_chain.set_erc20(USDC, wallet, 0)
                base_chain.set_erc20(WETH, wallet, out_amount)
                logs = [
                    transfer_log(
                        tx_hash=tx_hash,
                        log_index=3,
                        block=base_chain.block,
                        token=WETH,
                        sender=ROUTER,
                        recipient=wallet,
                        amount=out_amount,
                    )
                ]
                base_chain.receipt(tx_hash, status=1, gas_used=100_000, gas_price=10**8, logs=logs)
            else:
                base_chain.apply_approve(wallet, tx)
                base_chain.receipt(tx_hash, status=1, gas_used=100_000, gas_price=10**8)
            return tx_hash

        base_chain.on_send = on_send
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in=None,
            amount_pct=100,
            slippage_pct=None,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
        )
        assert orders[0]["status"] == "confirmed"
        realized = service.ledger.realized_by_position(wallet.lower())
        usdc_realized = realized[(8453, wallet.lower(), USDC)]
        assert usdc_realized == pytest.approx(200.0)
        portfolio = await service.portfolio()
        assert not any(h["token"]["address"] == USDC for h in portfolio["holdings"])
        assert portfolio["totals"]["realizedUsd"] == pytest.approx(usdc_realized)
        assert portfolio["wallets"][0]["totals"]["realizedUsd"] == pytest.approx(usdc_realized)
        # And per-wallet selection agrees with the whole.
        assert (await service.portfolio(wallet))["totals"]["realizedUsd"] == pytest.approx(
            usdc_realized
        )

    async def test_an_unpriced_holding_keeps_its_cost_out_of_the_totals(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_prices: FakePrices,
    ) -> None:
        """A9: no price, no value — and no cost in the total either, or it reads as a loss."""
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        self._list(fake_prices, base_chain)
        fake_prices.spot[("base", self.LISTED)] = 3.0
        base_chain.set_erc20(self.LISTED, wallet, 100 * 10**18)
        base_chain.add_transfer(
            token=self.LISTED, sender=OTHER, recipient=wallet, amount=100 * 10**18
        )
        await service.sync_all()  # opening lot: 100 LST at $3 = $300 of cost
        priced = await service.portfolio(wallet)
        assert priced["totals"]["costUsd"] == pytest.approx(3300.0)
        assert priced["unpricedCount"] == 0

        del fake_prices.spot[("base", self.LISTED)]
        portfolio = await service.portfolio(wallet)
        row = next(h for h in portfolio["holdings"] if h["token"]["address"] == self.LISTED)
        assert row["valueUsd"] is None and row["costUsd"] == pytest.approx(300.0)
        assert portfolio["totals"]["costUsd"] == pytest.approx(3000.0)
        assert portfolio["totals"]["valueUsd"] == pytest.approx(3000.0)
        assert portfolio["totals"]["unrealizedUsd"] == pytest.approx(0.0)
        assert portfolio["unpricedCount"] == 1
        assert portfolio["wallets"][0]["unpricedCount"] == 1

    async def test_a_free_lot_with_a_price_is_an_unrealised_gain(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_prices: FakePrices,
    ) -> None:
        """X2: an airdrop costs 0; once it has a price that is a gain, not nothing."""
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        self._list(fake_prices, base_chain)
        base_chain.set_erc20(self.LISTED, wallet, 100 * 10**18)
        base_chain.add_transfer(
            token=self.LISTED, sender=OTHER, recipient=wallet, amount=100 * 10**18
        )
        await service.sync_all()  # no price yet: the lot is booked free
        fake_prices.spot[("base", self.LISTED)] = 3.0
        portfolio = await service.portfolio(wallet)
        row = next(h for h in portfolio["holdings"] if h["token"]["address"] == self.LISTED)
        assert row["valueUsd"] == pytest.approx(300.0) and row["costUsd"] is None
        assert portfolio["totals"]["valueUsd"] == pytest.approx(3300.0)
        assert portfolio["totals"]["costUsd"] == pytest.approx(3000.0)
        assert portfolio["totals"]["unrealizedUsd"] == pytest.approx(300.0)
        assert portfolio["wallets"][0]["totals"]["unrealizedUsd"] == pytest.approx(300.0)
        assert portfolio["unpricedCount"] == 0


class TestApprovalGasInitiator:
    async def test_agent_erc20_sell_books_the_approval_under_the_agent(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
    ) -> None:
        """A15: the approval leg's gas entry names who caused it."""
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        fake_aggregator.approval_needed = True
        _wire_swap_effects(base_chain, wallet)
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="agent",
            session_key="agent:main:x",
            note=None,
            wait=True,
        )
        assert orders[0]["status"] == "confirmed"
        approvals = service.ledger.list_entries(wallet=wallet, kind="approval")
        assert len(approvals) == 1
        assert approvals[0]["initiator"] == "agent"
        assert approvals[0]["order_id"] == orders[0]["orderId"]


class TestGasReserveMessage:
    async def test_selling_all_of_a_dust_balance_says_why(
        self, funded_service: TradingService, base_chain: FakeChain
    ) -> None:
        """B7: 0.0005 ETH minus the 0.001 reserve is not 'amount must be greater than zero'."""
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        base_chain.set_native(wallet, 5 * 10**14)
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="ETH",
            token_out="USDC",
            amount_in=None,
            amount_pct=100,
            slippage_pct=None,
            initiator="manual",
            session_key=None,
            note=None,
        )
        assert orders[0]["status"] == "failed"
        assert (
            "balance 0.0005 ETH is below the 0.001 ETH gas reserve; nothing to sell"
            in orders[0]["reason"]
        )
        assert base_chain.sent == []


class TestLedgerRepairStatus:
    async def test_status_surfaces_what_the_ledger_needs(self, service: TradingService) -> None:
        """A2: ``ledgerRepair`` is None until the ledger says otherwise."""
        ledger = service.ledger
        if not hasattr(type(ledger), "repair_pending"):
            assert (await service.status())["ledgerRepair"] is None
        ledger.repair_pending = lambda: None  # type: ignore[method-assign]
        assert (await service.status())["ledgerRepair"] is None
        ledger.repair_pending = lambda: "full sync required"  # type: ignore[method-assign]
        assert (await service.status())["ledgerRepair"] == "full sync required"
