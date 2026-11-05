"""wallet.* / trading.* RPC handlers over a fully faked TradingService stack."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from agentos.gateway.config import GatewayConfig, TradingConfig
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.trading.ledger import Ledger
from agentos.trading.prices import PriceService
from agentos.trading.service import TradingService, set_trading_service
from agentos.trading.vault import Vault
from tests.test_trading.fakes import (
    USDC,
    WETH,
    FakeChain,
    FakePrices,
    FakeRelay,
    FakeUniswap,
    fake_sign_tx,
    make_transport,
)

PASSWORD = "correct horse battery"


@pytest_asyncio.fixture
async def stack(tmp_path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    monkeypatch.delenv("RPC_BASE_URL", raising=False)
    monkeypatch.delenv("RPC_ROBINHOOD_URL", raising=False)
    monkeypatch.delenv("UNISWAP_API_KEY", raising=False)
    base = FakeChain(chain_id=8453, block=100)
    base.tokens[USDC] = ("USDC", "USD Coin", 6)
    base.tokens[WETH] = ("WETH", "Wrapped Ether", 18)
    robinhood = FakeChain(chain_id=4663, block=100)
    uniswap = FakeUniswap()
    prices = FakePrices()
    prices.spot[("base", USDC)] = 1.0
    prices.spot[("base", WETH)] = 2000.0
    prices.lists["base"] = [
        {"chainId": 8453, "address": USDC, "symbol": "USDC", "name": "USD Coin", "decimals": 6},
        {
            "chainId": 8453,
            "address": WETH,
            "symbol": "WETH",
            "name": "Wrapped Ether",
            "decimals": 18,
        },
    ]
    relay = FakeRelay()
    transport = make_transport(
        chains={"mainnet.base.org": base, "rpc.mainnet.chain.robinhood.com": robinhood},
        uniswap=uniswap,
        prices=prices,
        relay=relay,
    )
    config = GatewayConfig()
    config.trading = TradingConfig(uniswap_api_key="test-key", price_ttl_seconds=1)
    events: list[tuple[str, dict[str, Any]]] = []

    async def broadcast(event: str, payload: dict[str, Any]) -> None:
        events.append((event, payload))

    async with httpx.AsyncClient(transport=transport) as http:
        ledger = Ledger(":memory:")
        service = TradingService(
            config,
            vault=Vault(tmp_path / "wallets", kdf="pbkdf2", kdf_iterations=1000),
            ledger=ledger,
            prices=PriceService(http=http, ttl_s=0),
            http=http,
            broadcast=broadcast,
            sign_tx=fake_sign_tx,
            background=False,
        )
        set_trading_service(service)
        try:
            yield {
                "service": service,
                "base": base,
                "robinhood": robinhood,
                "uniswap": uniswap,
                "relay": relay,
                "events": events,
                "config": config,
            }
        finally:
            set_trading_service(None)
            await service.stop()
            ledger.close()


@pytest.fixture
def ctx(stack: dict[str, Any]) -> RpcContext:
    return RpcContext(conn_id="test-conn", config=stack["config"])


async def call(method: str, params: dict[str, Any] | None, ctx: RpcContext):
    return await get_dispatcher().dispatch("r1", method, params, ctx)


class TestWalletRpc:
    async def test_status_setup_unlock_flow(self, ctx: RpcContext) -> None:
        res = await call("wallet.status", {}, ctx)
        assert res.ok and res.payload["initialized"] is False
        res = await call("wallet.setup", {"password": "short"}, ctx)
        assert res.ok is False
        res = await call("wallet.setup", {"password": PASSWORD, "unlockMode": "manual"}, ctx)
        assert (
            res.ok and res.payload["initialized"] is True and res.payload["unlockMode"] == "manual"
        )
        res = await call("wallet.setup", {"password": PASSWORD}, ctx)
        assert res.ok is False and res.error.code == "wallet.already_initialized"
        assert (await call("wallet.lock", {}, ctx)).payload == {"unlocked": False}
        res = await call("wallet.unlock", {"password": "wrong password!!"}, ctx)
        assert res.ok is False and res.error.code == "wallet.bad_password"
        assert (await call("wallet.unlock", {"password": PASSWORD}, ctx)).payload == {
            "unlocked": True
        }
        res = await call("wallet.setUnlockMode", {"mode": "auto", "password": PASSWORD}, ctx)
        assert res.payload == {"unlockMode": "auto"}
        res = await call(
            "wallet.changePassword", {"password": PASSWORD, "newPassword": "another long one"}, ctx
        )
        assert res.payload == {"changed": True}

    async def test_wallet_crud(self, ctx: RpcContext) -> None:
        await call("wallet.setup", {"password": PASSWORD}, ctx)
        created = await call("wallet.create", {"label": "Main"}, ctx)
        assert created.ok, created.error
        address = created.payload["wallet"]["address"]
        assert created.payload["wallet"]["primary"] is True
        imported = await call(
            "wallet.import", {"label": "Imp", "privateKey": "0x" + "55" * 32}, ctx
        )
        assert imported.ok and imported.payload["wallet"]["imported"] is True
        assert (await call("wallet.import", {"label": "x"}, ctx)).ok is False
        listed = await call("wallet.list", {}, ctx)
        assert [w["label"] for w in listed.payload["wallets"]] == ["Main", "Imp"]
        assert listed.payload["primary"] == address
        renamed = await call("wallet.rename", {"address": address, "label": "Primary"}, ctx)
        assert renamed.payload["wallet"]["label"] == "Primary"
        other = imported.payload["wallet"]["address"]
        assert (await call("wallet.setPrimary", {"address": other}, ctx)).payload["wallet"][
            "primary"
        ] is True
        exported = await call(
            "wallet.export", {"address": other, "password": PASSWORD, "format": "privateKey"}, ctx
        )
        assert exported.payload["privateKey"] == "0x" + "55" * 32
        keystore = await call("wallet.export", {"address": other, "password": PASSWORD}, ctx)
        assert "keystoreJson" in keystore.payload
        bad = await call(
            "wallet.export",
            {"address": other, "password": "nope nope nope", "format": "privateKey"},
            ctx,
        )
        assert bad.ok is False and bad.error.code == "wallet.bad_password"
        assert (
            await call(
                "wallet.export", {"address": other, "password": PASSWORD, "format": "seed"}, ctx
            )
        ).ok is False
        removed = await call("wallet.remove", {"address": other, "password": PASSWORD}, ctx)
        assert removed.payload == {"removed": True}
        missing = await call("wallet.remove", {"address": other, "password": PASSWORD}, ctx)
        assert missing.ok is False and missing.error.code == "wallet.not_found"
        balances = await call("wallet.balances", {"chainId": "base"}, ctx)
        assert balances.ok and "balances" in balances.payload
        assert (
            await call("wallet.balances", {"chainId": 1}, ctx)
        ).error.code == "trading.unsupported_chain"

    async def test_locked_vault_errors(self, ctx: RpcContext, stack: dict[str, Any]) -> None:
        await call("wallet.setup", {"password": PASSWORD, "unlockMode": "manual"}, ctx)
        await call("wallet.lock", {}, ctx)
        res = await call("wallet.create", {"label": "x"}, ctx)
        assert res.ok is False and res.error.code == "wallet.locked"


class TestTradingRpc:
    async def _funded(self, ctx: RpcContext, stack: dict[str, Any]) -> str:
        await call("wallet.setup", {"password": PASSWORD}, ctx)
        address = (await call("wallet.create", {"label": "Main"}, ctx)).payload["wallet"]["address"]
        stack["base"].set_native(address, 10**18)
        stack["base"].set_erc20(USDC, address, 1000 * 10**6)
        return address

    async def test_status_probe_search_resolve(self, ctx: RpcContext) -> None:
        status = await call("trading.status", {}, ctx)
        assert status.ok and status.payload["apiKeyConfigured"] is True
        assert status.payload["limits"]["approvalThresholdUsd"] == 100.0
        probe = await call("trading.probe", {}, ctx)
        assert probe.payload["ok"] is True
        assert probe.payload["provider"] == "aggregator"
        # A key is only ever checked for Uniswap; the default route has none.
        uniswap = await call("trading.probe", {"provider": "uniswap"}, ctx)
        assert uniswap.payload["ok"] is True
        bad = await call("trading.probe", {"apiKey": "bad", "provider": "uniswap"}, ctx)
        assert bad.payload["ok"] is False
        assert (await call("trading.probe", {"provider": "kyber"}, ctx)).ok is False
        search = await call("trading.tokens.search", {"chainId": 8453, "query": "USDC"}, ctx)
        assert search.payload["tokens"][0]["symbol"] == "USDC"
        assert (await call("trading.tokens.search", {"chainId": 8453}, ctx)).ok is False
        resolved = await call("trading.tokens.resolve", {"chainId": "base", "token": "WETH"}, ctx)
        assert resolved.payload["token"]["address"] == WETH
        assert (
            await call("trading.tokens.resolve", {"chainId": "base", "token": "NOPE"}, ctx)
        ).error.code == "trading.invalid"

    async def test_quote_swap_orders_history_portfolio(
        self, ctx: RpcContext, stack: dict[str, Any]
    ) -> None:
        address = await self._funded(ctx, stack)
        quote = await call(
            "trading.quote",
            {"chainId": 8453, "tokenIn": "USDC", "tokenOut": "WETH", "amountIn": "10"},
            ctx,
        )
        assert quote.ok, quote.error
        assert quote.payload["guard"]["decision"] == "allow" and quote.payload["wallet"] == address
        assert (
            await call(
                "trading.quote",
                {
                    "chainId": 8453,
                    "tokenIn": "USDC",
                    "tokenOut": "WETH",
                    "amountIn": "10",
                    "initiator": "robot",
                },
                ctx,
            )
        ).ok is False
        assert (
            await call("trading.quote", {"chainId": 8453, "tokenIn": "USDC", "amountIn": "10"}, ctx)
        ).ok is False

        # Agent swap above the threshold parks for approval.
        swap = await call(
            "trading.swap",
            {
                "chainId": "base",
                "tokenIn": "USDC",
                "tokenOut": "WETH",
                "amountIn": "250",
                "initiator": "agent",
                "sessionKey": "agent:main:s",
            },
            ctx,
        )
        assert swap.ok, swap.error
        order = swap.payload["orders"][0]
        assert order["status"] == "awaiting_approval"
        listed = await call("trading.orders.list", {"status": "awaiting_approval"}, ctx)
        assert (
            listed.payload["pendingApprovals"] == 1
            and listed.payload["orders"][0]["orderId"] == order["orderId"]
        )
        got = await call("trading.orders.get", {"orderId": order["orderId"]}, ctx)
        assert got.payload["order"]["initiator"] == "agent"
        assert (
            await call("trading.orders.get", {"orderId": "nope"}, ctx)
        ).error.code == "trading.invalid"
        waited = await call(
            "trading.orders.wait", {"orderId": order["orderId"], "timeoutSeconds": 0.01}, ctx
        )
        assert waited.payload["order"]["status"] == "awaiting_approval"
        rejected = await call(
            "trading.orders.reject", {"orderId": order["orderId"], "reason": "no"}, ctx
        )
        assert rejected.payload["order"]["status"] == "rejected"
        approve_again = await call("trading.orders.approve", {"orderId": order["orderId"]}, ctx)
        assert approve_again.ok is False

        limits = await call("trading.limits", {}, ctx)
        assert limits.payload["spentTodayUsd"] == 0.0 and limits.payload["wallet"] == address
        history = await call("trading.history", {"wallet": address, "limit": 10}, ctx)
        assert history.ok and "entries" in history.payload
        portfolio = await call("trading.portfolio", {}, ctx)
        assert portfolio.ok and "totals" in portfolio.payload
        chart = await call("trading.chart", {"chainId": 8453, "token": WETH, "range": "1d"}, ctx)
        assert chart.ok and chart.payload["source"] in ("geckoterminal", "snapshots")
        sync = await call("trading.sync", {"wallet": address}, ctx)
        assert sync.payload == {"started": True}
        await stack["service"].stop()
        assert (
            await call("trading.lot.setCost", {"entryId": 0, "costUsdPerToken": 1}, ctx)
        ).ok is False
        assert (
            await call("trading.unwrap", {"chainId": 8453}, ctx)
        ).error.code == "trading.invalid"

    async def test_swap_param_validation(self, ctx: RpcContext, stack: dict[str, Any]) -> None:
        await self._funded(ctx, stack)
        base = {"chainId": 8453, "tokenIn": "USDC", "tokenOut": "WETH", "amountIn": "1"}
        assert (await call("trading.swap", {**base, "wallets": 5}, ctx)).ok is False
        assert (await call("trading.swap", {**base, "initiator": "bot"}, ctx)).ok is False
        assert (await call("trading.swap", {**base, "amountIn": {"x": 1}}, ctx)).ok is False
        # Sizing is exactly one of amountIn / amountPct / amountUsd.
        both = {**base, "amountUsd": 5}
        assert (await call("trading.swap", both, ctx)).error.code == "trading.invalid"
        usd_quote = await call(
            "trading.quote",
            {"chainId": 8453, "tokenIn": "ETH", "tokenOut": "USDC", "amountUsd": 5},
            ctx,
        )
        assert usd_quote.ok, usd_quote.error
        assert usd_quote.payload["amountIn"] == "0.0025"
        assert (
            await call("trading.swap", {**base, "chainId": 999}, ctx)
        ).error.code == "trading.unsupported_chain"
        no_key = dict(stack["config"].trading)
        # Only the Uniswap route can be missing a key, so select it to test that.
        stack["config"].trading.provider = "uniswap"
        stack["config"].trading.uniswap_api_key = ""
        res = await call("trading.swap", base, ctx)
        assert res.ok is False and res.error.code == "trading.no_api_key"
        stack["config"].trading.uniswap_api_key = no_key["uniswap_api_key"]
        stack["config"].trading.provider = no_key["provider"]


def _agent_ctx(stack: dict[str, Any], session_key: str = "agent:trading:webchat:t") -> RpcContext:
    """A context the gateway admitted as an agent's (token or exec window)."""
    from agentos.gateway.access import ConnectionSurface
    from agentos.gateway.agent_surface import AgentSurface
    from agentos.gateway.auth import AccessContext

    surface = AgentSurface()
    token = surface.mint_token(session_key, "trading")
    access = surface.attach(
        AccessContext(surface=ConnectionSurface.CONTROL, admitted=True, credential_verified=True),
        {"agentToken": token},
    )
    return RpcContext(conn_id="agent-conn", access=access, config=stack["config"])


class TestAgentSurfaceRpc:
    """What an agent-bound connection may and may not do, whatever it declares."""

    async def _funded(self, ctx: RpcContext, stack: dict[str, Any]) -> str:
        await call("wallet.setup", {"password": PASSWORD}, ctx)
        address = (await call("wallet.create", {"label": "Main"}, ctx)).payload["wallet"]["address"]
        stack["base"].set_native(address, 10**18)
        stack["base"].set_erc20(USDC, address, 1000 * 10**6)
        return address

    async def test_agent_declaring_manual_is_still_an_agent(
        self, ctx: RpcContext, stack: dict[str, Any]
    ) -> None:
        await self._funded(ctx, stack)
        agent = _agent_ctx(stack)
        quote = await call(
            "trading.quote",
            {
                "chainId": 8453,
                "tokenIn": "USDC",
                "tokenOut": "WETH",
                "amountIn": "250",
                "initiator": "manual",
            },
            agent,
        )
        assert quote.ok, quote.error
        assert quote.payload["guard"]["decision"] == "needs_approval"
        swap = await call(
            "trading.swap",
            {
                "chainId": 8453,
                "tokenIn": "USDC",
                "tokenOut": "WETH",
                "amountIn": "250",
                "initiator": "manual",
                "sessionKey": "agent:main:spoofed",
            },
            agent,
        )
        assert swap.ok, swap.error
        order = swap.payload["orders"][0]
        assert order["initiator"] == "agent" and order["status"] == "awaiting_approval"
        # Filed under the chat the binding names, not the one the client typed.
        assert order["sessionKey"] == "agent:trading:webchat:t"

    async def test_agent_cannot_approve_reject_or_touch_the_vault(
        self, ctx: RpcContext, stack: dict[str, Any]
    ) -> None:
        address = await self._funded(ctx, stack)
        agent = _agent_ctx(stack)
        swap = await call(
            "trading.swap",
            {"chainId": 8453, "tokenIn": "USDC", "tokenOut": "WETH", "amountIn": "250"},
            agent,
        )
        order_id = swap.payload["orders"][0]["orderId"]
        for method, params in [
            ("trading.orders.approve", {"orderId": order_id}),
            ("trading.orders.reject", {"orderId": order_id}),
            ("wallet.export", {"address": address, "password": PASSWORD, "format": "privateKey"}),
            ("wallet.create", {"label": "Extra"}),
            ("wallet.remove", {"address": address, "password": PASSWORD}),
            ("wallet.setPrimary", {"address": address}),
            ("wallet.rename", {"address": address, "label": "x"}),
            ("wallet.lock", {}),
            ("wallet.unlock", {"password": PASSWORD}),
            ("wallet.setup", {"password": PASSWORD}),
            ("trading.lot.setCost", {"entryId": 1, "costUsdPerToken": 1}),
            ("trading.tokens.hide", {"chainId": 8453, "address": USDC, "hidden": True}),
            ("trading.unwrap", {"chainId": 8453}),
        ]:
            res = await call(method, params, agent)
            assert res.ok is False, method
            assert res.error.code == "trading.operator_required", (method, res.error)
        # Still parked: nothing above changed the order.
        got = await call("trading.orders.get", {"orderId": order_id}, agent)
        assert got.payload["order"]["status"] == "awaiting_approval"
        # The operator's own connection is unaffected.
        approved = await call("trading.orders.reject", {"orderId": order_id}, ctx)
        assert approved.ok and approved.payload["order"]["status"] == "rejected"

    async def test_agent_reads_are_fine(self, ctx: RpcContext, stack: dict[str, Any]) -> None:
        address = await self._funded(ctx, stack)
        agent = _agent_ctx(stack)
        for method, params in [
            ("wallet.list", {}),
            ("wallet.status", {}),
            ("wallet.balances", {"address": address, "chainId": 8453}),
            ("trading.status", {}),
            ("trading.orders.list", {}),
            ("trading.limits", {}),
            ("trading.portfolio", {}),
        ]:
            res = await call(method, params, agent)
            assert res.ok, (method, res.error)

    async def test_agent_probe_with_key_and_full_sync_are_operator_only(
        self, ctx: RpcContext, stack: dict[str, Any]
    ) -> None:
        await self._funded(ctx, stack)
        agent = _agent_ctx(stack)
        # The plain forms are reads any client may do.
        assert (await call("trading.probe", {}, agent)).ok
        assert (await call("trading.probe", {"provider": "uniswap"}, agent)).ok
        assert (await call("trading.sync", {}, agent)).payload == {"started": True}
        # Testing a key that is not in config, or rebuilding the ledger, is not.
        res = await call("trading.probe", {"apiKey": "guess", "provider": "uniswap"}, agent)
        assert res.ok is False and res.error.code == "trading.operator_required"
        assert "trading.probe(apiKey)" in res.error.message
        res = await call("trading.sync", {"full": True}, agent)
        assert res.ok is False and res.error.code == "trading.operator_required"
        # The operator's connection still can.
        assert (await call("trading.probe", {"apiKey": "k", "provider": "uniswap"}, ctx)).ok
        assert (await call("trading.sync", {"full": True}, ctx)).payload == {"started": True}

    async def test_agent_status_has_no_paths(self, ctx: RpcContext, stack: dict[str, Any]) -> None:
        await self._funded(ctx, stack)
        agent = _agent_ctx(stack)
        for method in ("wallet.status", "trading.status"):
            mine = (await call(method, {}, ctx)).payload
            theirs = (await call(method, {}, agent)).payload
            assert not [k for k in theirs if k.endswith("Path")], (method, theirs)
            assert theirs["unlocked"] is True and theirs["initialized"] is True
            assert set(mine) >= set(theirs)
        # The operator still sees where the vault lives.
        assert (await call("wallet.status", {}, ctx)).payload["vaultPath"]

    async def test_agent_cannot_raise_the_limits(
        self, ctx: RpcContext, stack: dict[str, Any]
    ) -> None:
        agent = _agent_ctx(stack)
        res = await call("config.set", {"path": "trading.daily_cap_usd", "value": 1e9}, agent)
        assert res.ok is False and "user's to change" in res.error.message
        res = await call(
            "config.patch", {"patches": {"trading.approval_threshold_usd": 1e9}}, agent
        )
        assert res.ok is False
        res = await call("config.patch", {"patch": {"trading": {"daily_cap_usd": 1e9}}}, agent)
        assert res.ok is False
        assert stack["config"].trading.daily_cap_usd == 1000.0


class TestNoteSanitising:
    """A note is the one thing an agent writes that a person reads before approving."""

    def test_note_helper(self) -> None:
        from agentos.gateway.rpc_trading import NOTE_MAX_CHARS, _note

        assert _note({}) is None
        assert _note({"note": "   "}) is None
        assert _note({"note": "DCA  tick\n\t#3"}) == "DCA tick #3"
        # Bidi overrides, isolates and C0/C1 controls are dropped, not escaped.
        spoofed = "send to \u202eB\u202c not A\u2066x\u2069\x00\x1b[31m\x85"
        assert _note({"note": spoofed}) == "send to B not Ax[31m"
        assert _note({"note": "\u200fhi\u200e"}) == "hi"
        # NFC so a decomposed accent does not render as a different word later.
        assert _note({"note": "café"}) == "café"
        long = _note({"note": "x" * 1000})
        assert long is not None and len(long) == NOTE_MAX_CHARS
        with pytest.raises(ValueError):
            _note({"note": 5})

    async def test_note_is_stored_sanitised(self, ctx: RpcContext, stack: dict[str, Any]) -> None:
        await call("wallet.setup", {"password": PASSWORD}, ctx)
        address = (await call("wallet.create", {"label": "Main"}, ctx)).payload["wallet"]["address"]
        stack["base"].set_native(address, 10**18)
        stack["base"].set_erc20(USDC, address, 1000 * 10**6)
        res = await call(
            "trading.swap",
            {
                "chainId": 8453,
                "tokenIn": "USDC",
                "tokenOut": "WETH",
                "amountIn": "1",
                "note": "\u202erent\u202c " + "y" * 1000,
            },
            ctx,
        )
        assert res.ok, res.error
        note = res.payload["orders"][0]["note"]
        assert note.startswith("rent y") and len(note) == 240 and "\u202e" not in note
        other = "0x2222222222222222222222222222222222222222"
        res = await call(
            "trading.send",
            {"chainId": 8453, "token": "USDC", "to": other, "amount": "1", "note": "\x07 pay\n"},
            ctx,
        )
        assert res.ok, res.error
        assert res.payload["orders"][0]["note"] == "pay"


class TestClientOrderId:
    """``clientOrderId`` reaches the engine as ``client_order_id``, or is refused; never dropped."""

    async def _funded(self, ctx: RpcContext, stack: dict[str, Any]) -> str:
        await call("wallet.setup", {"password": PASSWORD}, ctx)
        address = (await call("wallet.create", {"label": "Main"}, ctx)).payload["wallet"]["address"]
        stack["base"].set_native(address, 10**18)
        stack["base"].set_erc20(USDC, address, 1000 * 10**6)
        return address

    async def test_reaches_the_service(
        self, ctx: RpcContext, stack: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await self._funded(ctx, stack)
        seen: list[tuple[str, Any]] = []

        # The WS-B contract: ``client_order_id: str | None = None`` on both.
        async def fake_swap(
            self, *, client_order_id: str | None = None, **kwargs: Any
        ) -> list[dict[str, Any]]:
            seen.append(("swap", client_order_id))
            return [{"orderId": "ord_swap", "clientOrderId": client_order_id}]

        async def fake_send(
            self, *, client_order_id: str | None = None, **kwargs: Any
        ) -> list[dict[str, Any]]:
            seen.append(("send", client_order_id))
            return [{"orderId": "ord_send", "batchId": None}]

        monkeypatch.setattr(TradingService, "swap", fake_swap)
        monkeypatch.setattr(TradingService, "send", fake_send)
        base = {"chainId": 8453, "tokenIn": "USDC", "tokenOut": "WETH", "amountIn": "1"}
        res = await call("trading.swap", {**base, "clientOrderId": " dca-2026-09-20 "}, ctx)
        assert res.ok, res.error
        assert res.payload["orders"][0]["clientOrderId"] == "dca-2026-09-20"
        res = await call("trading.swap", base, ctx)
        assert res.ok, res.error
        res = await call(
            "trading.send",
            {
                "chainId": 8453,
                "token": "USDC",
                "to": "0x2222222222222222222222222222222222222222",
                "amount": "1",
                "clientOrderId": "rent-09",
            },
            ctx,
        )
        assert res.ok, res.error
        assert seen == [("swap", "dca-2026-09-20"), ("swap", None), ("send", "rent-09")]
        assert (await call("trading.swap", {**base, "clientOrderId": 7}, ctx)).ok is False

    async def test_refused_when_the_engine_cannot_honour_it(
        self, ctx: RpcContext, stack: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await self._funded(ctx, stack)
        called = False

        async def legacy_swap(self, **kwargs: Any) -> list[dict[str, Any]]:  # no client_order_id
            nonlocal called
            called = True
            return []

        monkeypatch.setattr(TradingService, "swap", legacy_swap)
        base = {"chainId": 8453, "tokenIn": "USDC", "tokenOut": "WETH", "amountIn": "1"}
        res = await call("trading.swap", {**base, "clientOrderId": "x"}, ctx)
        assert res.ok is False and res.error.code == "trading.invalid"
        assert "clientOrderId" in res.error.message and called is False


class TestConfig:
    def test_defaults_and_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = TradingConfig()
        assert cfg.approval_threshold_usd == 100.0 and cfg.daily_cap_usd == 1000.0
        assert cfg.approval_ttl_seconds == 900 and cfg.unlock_mode == "auto"
        assert cfg.resolved_uniswap_api_key() == ""
        monkeypatch.setenv("UNISWAP_API_KEY", "from-env")
        assert cfg.resolved_uniswap_api_key() == "from-env"
        assert TradingConfig(uniswap_api_key="explicit").resolved_uniswap_api_key() == "explicit"
        monkeypatch.setenv("AGENTOS_TRADING_DAILY_CAP_USD", "50")
        assert TradingConfig().daily_cap_usd == 50.0
        with pytest.raises(ValueError):
            TradingConfig(approval_ttl_seconds=1)

    def test_gateway_config_carries_trading_and_redacts_key(self) -> None:
        from agentos.gateway.config import redact_public_config

        cfg = GatewayConfig()
        cfg.trading = TradingConfig(uniswap_api_key="sekret")
        data = cfg.to_toml_dict()
        assert data["trading"]["uniswap_api_key"] == "sekret"
        assert redact_public_config(data)["trading"]["uniswap_api_key"] == "[redacted]"
        assert redact_public_config(data)["trading"]["uniswap_api_key_env"] == "UNISWAP_API_KEY"


class TestSendAndToolsRpc:
    """``trading.send``, batches, allowances, decode and network over the same fake stack."""

    async def _funded(self, ctx: RpcContext, stack: dict[str, Any]) -> str:
        await call("wallet.setup", {"password": PASSWORD}, ctx)
        address = (await call("wallet.create", {"label": "Main"}, ctx)).payload["wallet"]["address"]
        stack["base"].set_native(address, 10**18)
        stack["base"].set_erc20(USDC, address, 1000 * 10**6)
        return address

    async def test_send_shapes_and_validation(self, ctx: RpcContext, stack: dict[str, Any]) -> None:
        await self._funded(ctx, stack)
        other = "0x2222222222222222222222222222222222222222"
        third = "0x3333333333333333333333333333333333333333"
        # One-address form.
        res = await call(
            "trading.send", {"chainId": 8453, "token": "USDC", "to": other, "amount": "1"}, ctx
        )
        assert res.ok, res.error
        order = res.payload["orders"][0]
        assert order["kind"] == "send" and order["status"] == "submitted"
        assert order["recipient"] == "0x2222222222222222222222222222222222222222"
        assert res.payload["batchId"] is None
        # List form with mixed sizing.
        res = await call(
            "trading.send",
            {
                "chainId": "base",
                "token": "USDC",
                "recipients": [{"to": other, "amount": 2}, {"to": third, "amountUsd": 3}],
            },
            ctx,
        )
        assert res.ok, res.error
        batch_id = res.payload["batchId"]
        assert batch_id and len(res.payload["orders"]) == 2
        batch = await call("trading.orders.batch", {"batchId": batch_id}, ctx)
        assert [o["amountIn"] for o in batch.payload["orders"]] == ["2", "3"]
        sends = await call("trading.orders.list", {"kind": "send"}, ctx)
        assert len(sends.payload["orders"]) == 3
        assert (await call("trading.orders.list", {"kind": "swap"}, ctx)).payload["orders"] == []
        # Bad shapes are refused before the engine sees them.
        for params in (
            {"chainId": 8453, "token": "USDC"},
            {"chainId": 8453, "token": "USDC", "recipients": []},
            {"chainId": 8453, "token": "USDC", "recipients": ["x"]},
            {"chainId": 8453, "token": "USDC", "to": other, "amount": True},
            {"chainId": 8453, "to": other, "amount": "1"},
        ):
            assert (await call("trading.send", params, ctx)).ok is False, params
        res = await call(
            "trading.send", {"chainId": 8453, "token": "USDC", "to": other, "amount": "1e9"}, ctx
        )
        assert res.ok is False and res.error.code == "trading.insufficient_balance"
        assert (await call("trading.orders.batch", {"batchId": "bat_nope"}, ctx)).ok is False

    async def test_agent_send_and_revoke_park_for_the_user(
        self, ctx: RpcContext, stack: dict[str, Any]
    ) -> None:
        address = await self._funded(ctx, stack)
        agent = _agent_ctx(stack)
        other = "0x2222222222222222222222222222222222222222"
        res = await call(
            "trading.send",
            {
                "chainId": 8453,
                "token": "USDC",
                "to": other,
                "amount": "1",
                "initiator": "manual",
                "sessionKey": "agent:main:spoofed",
            },
            agent,
        )
        assert res.ok, res.error
        order = res.payload["orders"][0]
        assert order["initiator"] == "agent" and order["status"] == "awaiting_approval"
        assert order["sessionKey"] == "agent:trading:webchat:t"
        assert "for good" in order["reason"]
        # Only the user decides.
        denied = await call("trading.orders.approve", {"orderId": order["orderId"]}, agent)
        assert denied.ok is False and denied.error.code == "trading.operator_required"
        rejected = await call("trading.orders.reject", {"orderId": order["orderId"]}, ctx)
        assert rejected.ok and rejected.payload["order"]["status"] == "rejected"
        # A revoke proposed by the agent is an order for the user to approve.
        stack["base"].set_allowance(USDC, address, other, 5)
        res = await call(
            "trading.allowances.revoke",
            {"chainId": 8453, "token": USDC, "spender": other},
            agent,
        )
        assert res.ok, res.error
        assert res.payload["order"]["kind"] == "revoke"
        assert res.payload["order"]["status"] == "awaiting_approval"
        assert res.payload["order"]["recipient"] == "0x2222222222222222222222222222222222222222"
        missing = await call("trading.allowances.revoke", {"chainId": 8453, "token": USDC}, ctx)
        assert missing.ok is False

    async def test_allowances_decode_network_reads(
        self, ctx: RpcContext, stack: dict[str, Any]
    ) -> None:
        address = await self._funded(ctx, stack)
        agent = _agent_ctx(stack)
        # Nothing granted yet: an empty review on every chain, and per chain.
        res = await call("trading.allowances.list", {}, agent)
        assert res.ok, res.error
        assert res.payload["count"] == 0 and res.payload["wallet"] == address
        assert [c["chainId"] for c in res.payload["chains"]] == [8453, 4663]
        one = await call("trading.allowances.list", {"chainId": 8453, "full": True}, ctx)
        assert one.ok and one.payload["chainId"] == 8453 and one.payload["allowances"] == []
        decoded = await call(
            "trading.decode",
            {
                "chainId": 8453,
                "data": "0xa9059cbb"
                + "0" * 24
                + "2222222222222222222222222222222222222222"
                + format(10**6, "x").rjust(64, "0"),
                "to": USDC,
            },
            agent,
        )
        assert decoded.ok, decoded.error
        assert decoded.payload["call"]["function"] == "transfer"
        assert decoded.payload["decoded"]["amount"] == "1"
        assert decoded.payload["decoded"]["token"]["symbol"] == "USDC"
        assert (await call("trading.decode", {"chainId": 8453}, ctx)).ok is False
        missing = await call("trading.decode", {"chainId": 8453, "txHash": "0x" + "00" * 32}, ctx)
        assert missing.ok is False and missing.error.code == "trading.invalid"
        network = await call("trading.network", {}, agent)
        assert network.ok, network.error
        rows = network.payload["chains"]
        assert [r["chainId"] for r in rows] == [8453, 4663]
        assert rows[0]["blockNumber"] == 100 and rows[0]["latencyMs"] is not None


class TestClientQuote:
    """``expectedOutRaw`` / ``minOutRaw`` / ``quoteId`` reach the engine as ints and a string."""

    async def _funded(self, ctx: RpcContext, stack: dict[str, Any]) -> str:
        await call("wallet.setup", {"password": PASSWORD}, ctx)
        address = (await call("wallet.create", {"label": "Main"}, ctx)).payload["wallet"]["address"]
        stack["base"].set_native(address, 10**18)
        stack["base"].set_erc20(USDC, address, 1000 * 10**6)
        return address

    async def test_reaches_the_service(
        self, ctx: RpcContext, stack: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await self._funded(ctx, stack)
        seen: list[dict[str, Any]] = []

        async def fake_swap(
            self,
            *,
            expected_out_raw: int | None = None,
            min_out_raw: int | None = None,
            quote_id: str | None = None,
            **kwargs: Any,
        ) -> list[dict[str, Any]]:
            seen.append(
                {
                    "expected_out_raw": expected_out_raw,
                    "min_out_raw": min_out_raw,
                    "quote_id": quote_id,
                }
            )
            return [{"orderId": "ord_swap"}]

        monkeypatch.setattr(TradingService, "swap", fake_swap)
        base = {"chainId": 8453, "tokenIn": "USDC", "tokenOut": "WETH", "amountIn": "1"}
        res = await call(
            "trading.swap",
            {
                **base,
                "expectedOutRaw": "5000000000000000",
                "minOutRaw": " 4975000000000000 ",
                "quoteId": "q-1",
            },
            ctx,
        )
        assert res.ok, res.error
        # Integers are accepted too; a JSON client may send either.
        res = await call("trading.swap", {**base, "expectedOutRaw": 42}, ctx)
        assert res.ok, res.error
        res = await call("trading.swap", base, ctx)
        assert res.ok, res.error
        assert seen == [
            {
                "expected_out_raw": 5_000_000_000_000_000,
                "min_out_raw": 4_975_000_000_000_000,
                "quote_id": "q-1",
            },
            {"expected_out_raw": 42, "min_out_raw": None, "quote_id": None},
            {"expected_out_raw": None, "min_out_raw": None, "quote_id": None},
        ]
        # Not an integer string: refused, never silently dropped.
        for bad in ("0.005", "abc", 1.5, True, "-1"):
            res = await call("trading.swap", {**base, "expectedOutRaw": bad}, ctx)
            assert res.ok is False, bad
        assert len(seen) == 3
