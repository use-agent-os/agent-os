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
    FakeDexBridge,
    FakePrices,
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
    bridge = FakeDexBridge()
    transport = make_transport(
        chains={"mainnet.base.org": base, "rpc.mainnet.chain.robinhood.com": robinhood},
        prices=prices,
        bridge=bridge,
    )
    config = GatewayConfig()
    config.trading = TradingConfig(price_ttl_seconds=1)
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
                "bridge": bridge,
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

    async def test_remove_batch(self, ctx: RpcContext, stack: dict[str, Any]) -> None:
        await call("wallet.setup", {"password": PASSWORD}, ctx)
        made = [
            (await call("wallet.create", {"label": f"W{i}"}, ctx)).payload["wallet"]["address"]
            for i in range(4)
        ]
        bad = await call("wallet.removeBatch", {"addresses": made[:2], "password": "nope"}, ctx)
        assert bad.ok is False
        # One unknown address refuses the whole list: nothing is removed.
        unknown = "0x" + "ab" * 20
        missing = await call(
            "wallet.removeBatch", {"addresses": [made[0], unknown], "password": PASSWORD}, ctx
        )
        assert missing.ok is False and missing.error.code == "wallet.not_found"
        assert len((await call("wallet.list", {}, ctx)).payload["wallets"]) == 4
        shape = await call("wallet.removeBatch", {"addresses": "x", "password": PASSWORD}, ctx)
        assert shape.ok is False
        empty = await call("wallet.removeBatch", {"addresses": [], "password": PASSWORD}, ctx)
        assert empty.ok is False
        # The primary (the first wallet made) goes too: the next one takes over.
        removed = await call(
            "wallet.removeBatch",
            {"addresses": [made[0], made[2], made[2].lower()], "password": PASSWORD},
            ctx,
        )
        assert removed.ok, removed.error
        assert sorted(a.lower() for a in removed.payload["removed"]) == sorted(
            [made[0].lower(), made[2].lower()]
        )
        listed = (await call("wallet.list", {}, ctx)).payload
        assert {w["address"] for w in listed["wallets"]} == {made[1], made[3]}
        assert listed["primary"] in {made[1], made[3]}

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
        assert status.ok and "apiKeyConfigured" not in status.payload
        assert status.payload["provider"] == "aggregator"
        assert [p["id"] for p in status.payload["providers"]] == ["aggregator"]
        assert status.payload["limits"]["approvalThresholdUsd"] == 100.0
        probe = await call("trading.probe", {}, ctx)
        assert probe.payload["ok"] is True
        assert probe.payload["provider"] == "aggregator"
        # One route and no key: a provider or a key an older client still
        # sends changes nothing about what is probed.
        legacy = await call("trading.probe", {"provider": "uniswap", "apiKey": "old"}, ctx)
        assert legacy.payload["ok"] is True and legacy.payload["provider"] == "aggregator"
        # The provider can no longer be switched.
        gone = await call("trading.setProvider", {"provider": "uniswap"}, ctx)
        assert gone.ok is False
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
            ("wallet.removeBatch", {"addresses": [address], "password": PASSWORD}),
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

    async def test_agent_probe_is_a_read_and_full_sync_is_operator_only(
        self, ctx: RpcContext, stack: dict[str, Any]
    ) -> None:
        await self._funded(ctx, stack)
        agent = _agent_ctx(stack)
        # The plain forms are reads any client may do.
        assert (await call("trading.probe", {}, agent)).ok
        assert (await call("trading.sync", {}, agent)).payload == {"started": True}
        # Rebuilding the ledger is not.
        res = await call("trading.sync", {"full": True}, agent)
        assert res.ok is False and res.error.code == "trading.operator_required"
        # The operator's connection still can.
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
        # One swap route and no key: neither is a setting any more.
        assert not hasattr(cfg, "provider") and not hasattr(cfg, "uniswap_api_key")
        with pytest.raises(ValueError):
            TradingConfig(provider="uniswap")  # type: ignore[call-arg]
        with pytest.raises(ValueError):
            TradingConfig(uniswap_api_key="k")  # type: ignore[call-arg]
        monkeypatch.setenv("AGENTOS_TRADING_DAILY_CAP_USD", "50")
        assert TradingConfig().daily_cap_usd == 50.0
        with pytest.raises(ValueError):
            TradingConfig(approval_ttl_seconds=1)

    def test_gateway_config_carries_trading_without_a_provider_or_a_key(self) -> None:
        cfg = GatewayConfig()
        cfg.trading = TradingConfig(daily_cap_usd=250.0)
        data = cfg.to_toml_dict()
        assert data["trading"]["daily_cap_usd"] == 250.0
        assert not {"provider", "uniswap_api_key", "uniswap_api_key_env"} & set(data["trading"])


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

    async def test_batch_approval_names_every_leg_and_echoes_its_digest(
        self, ctx: RpcContext, stack: dict[str, Any]
    ) -> None:
        """WAL-12 over the wire: a batch is approved only with the digest of all its legs."""
        await self._funded(ctx, stack)
        agent = _agent_ctx(stack)
        legs = [
            {"to": "0x2222222222222222222222222222222222222222", "amount": "1"},
            {"to": "0x3333333333333333333333333333333333333333", "amount": "450"},
        ]
        res = await call(
            "trading.send", {"chainId": 8453, "token": "USDC", "recipients": legs}, agent
        )
        assert res.ok, res.error
        first = res.payload["orders"][0]
        assert first["status"] == "awaiting_approval"
        assert first["approval"]["legCount"] == 2 and first["approval"]["digest"] is None
        bare = await call("trading.orders.approve", {"orderId": first["orderId"]}, ctx)
        assert bare.ok is False and bare.error.code == "trading.approval_binding_required"
        batch = await call("trading.orders.batch", {"batchId": first["batchId"]}, ctx)
        approval = batch.payload["approval"]
        assert [leg["recipient"] for leg in approval["legs"]] == [
            "0x2222222222222222222222222222222222222222",
            "0x3333333333333333333333333333333333333333",
        ]
        assert approval["totalRaw"] == str(451 * 10**6)
        stale = await call(
            "trading.orders.approve",
            {"orderId": first["orderId"], "approvalDigest": "f" * 64},
            ctx,
        )
        assert stale.ok is False and stale.error.code == "trading.approval_changed"
        assert stack["base"].sent == []
        done = await call(
            "trading.orders.approve",
            {"orderId": first["orderId"], "approvalDigest": approval["digest"]},
            ctx,
        )
        assert done.ok, done.error
        assert len(stack["base"].sent) == 2

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


def _unbound_ctx(stack: dict[str, Any]) -> RpcContext:
    """A local connection with no operator proof: it gets the agent's rules."""
    from agentos.gateway.access import ConnectionSurface
    from agentos.gateway.agent_surface import AgentBinding
    from agentos.gateway.auth import AccessContext

    access = AccessContext(
        surface=ConnectionSurface.CONTROL,
        admitted=True,
        credential_verified=False,
        agent=AgentBinding(via="unbound"),
    )
    return RpcContext(conn_id="unbound-conn", access=access, config=stack["config"])


class TestOperatorLockHolds:
    """WAL-22: in auto mode the next agent call used to read unlock.key and undo the lock."""

    async def test_no_agent_or_unbound_call_undoes_the_operators_lock(
        self, ctx: RpcContext, stack: dict[str, Any]
    ) -> None:
        await call("wallet.setup", {"password": PASSWORD}, ctx)  # auto, the default
        address = (await call("wallet.create", {"label": "Main"}, ctx)).payload["wallet"]["address"]
        stack["base"].set_native(address, 10**18)
        stack["base"].set_erc20(USDC, address, 1000 * 10**6)
        service = stack["service"]
        agent = _agent_ctx(stack)
        swap = {"chainId": 8453, "tokenIn": "USDC", "tokenOut": "WETH", "amountIn": "10"}

        assert (await call("wallet.lock", {}, ctx)).payload == {"unlocked": False}
        for method, params, who in [
            ("wallet.status", {}, agent),
            ("wallet.list", {}, agent),
            ("trading.status", {}, _unbound_ctx(stack)),
            ("wallet.balances", {}, agent),
            ("trading.quote", swap, agent),
            ("wallet.status", {}, ctx),  # the operator's own status read is no unlock either
        ]:
            await call(method, params, who)
            assert service.vault.unlocked is False, method
        status = await call("wallet.status", {}, agent)
        assert status.payload["unlocked"] is False and status.payload["lockedByOperator"] is True

        res = await call("trading.swap", swap, agent)
        assert res.ok is False and res.error.code == "wallet.locked"
        assert stack["base"].sent == [] and service.vault.unlocked is False

        # Only the operator's unlock, with the password, ends it.
        assert (await call("wallet.unlock", {"password": PASSWORD}, ctx)).ok
        res = await call("trading.swap", swap, agent)
        assert res.ok, res.error
        assert res.payload["orders"][0]["status"] == "submitted"
        assert len(stack["base"].sent) == 1


class TestWalletBatchRpc:
    async def test_create_batch_then_download_with_password(self, ctx: RpcContext) -> None:
        await call("wallet.setup", {"password": PASSWORD}, ctx)
        made = await call("wallet.createBatch", {"count": 3}, ctx)
        assert made.ok, made.error
        wallets = made.payload["wallets"]
        assert len(wallets) == 3 and made.payload["batchId"]
        assert wallets[0]["primary"] is True
        assert all(w["label"] == f"{w['address'][:5]}_{w['address'][-4:]}" for w in wallets)
        # Nothing secret comes back with the wallets themselves.
        assert "privateKey" not in str(made.payload) and "mnemonic" not in str(made.payload)
        batch = made.payload["batchId"]
        # An empty password is not a password, and is not Touch ID either.
        empty = await call("wallet.exportBatch", {"batchId": batch, "password": ""}, ctx)
        assert empty.ok is False
        wrong = await call("wallet.exportBatch", {"batchId": batch, "password": "nope nope"}, ctx)
        assert wrong.ok is False and wrong.error.code == "wallet.bad_password"
        got = await call("wallet.exportBatch", {"batchId": batch, "password": PASSWORD}, ctx)
        assert got.ok, got.error
        rows = got.payload["wallets"]
        assert [r["address"] for r in rows] == [w["address"] for w in wallets]
        for row in rows:
            assert len(row["mnemonic"].split()) == 12
            exported = await call(
                "wallet.export",
                {"address": row["address"], "password": PASSWORD, "format": "privateKey"},
                ctx,
            )
            assert exported.payload["privateKey"] == row["privateKey"]
        touch = await call("wallet.exportBatch", {"batchId": batch, "touchId": True}, ctx)
        assert touch.ok and touch.payload["wallets"] == rows
        listed = await call("wallet.list", {}, ctx)
        assert len(listed.payload["wallets"]) == 3

    async def test_create_batch_bounds_and_locks(self, ctx: RpcContext) -> None:
        await call("wallet.setup", {"password": PASSWORD, "unlockMode": "manual"}, ctx)
        for count in (0, 31, "x"):
            res = await call("wallet.createBatch", {"count": count}, ctx)
            assert res.ok is False, count
        made = await call("wallet.createBatch", {"count": 2, "label": "Bot"}, ctx)
        assert [w["label"] for w in made.payload["wallets"]] == ["Bot 1", "Bot 2"]
        batch = made.payload["batchId"]
        # Locking forgets the batch's secrets.
        await call("wallet.lock", {}, ctx)
        await call("wallet.unlock", {"password": PASSWORD}, ctx)
        gone = await call("wallet.exportBatch", {"batchId": batch, "password": PASSWORD}, ctx)
        assert gone.ok is False and gone.error.code == "wallet.batch_expired"
        unknown = await call("wallet.exportBatch", {"batchId": "nope", "touchId": True}, ctx)
        assert unknown.error.code == "wallet.batch_expired"
        await call("wallet.lock", {}, ctx)
        locked = await call("wallet.createBatch", {"count": 2}, ctx)
        assert locked.ok is False and locked.error.code == "wallet.locked"

    async def test_batch_secrets_expire(
        self, ctx: RpcContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agentos.trading.service as service_mod

        await call("wallet.setup", {"password": PASSWORD}, ctx)
        monkeypatch.setattr(service_mod, "MINTED_TTL_S", -1.0)
        made = await call("wallet.createBatch", {"count": 1}, ctx)
        res = await call(
            "wallet.exportBatch", {"batchId": made.payload["batchId"], "password": PASSWORD}, ctx
        )
        assert res.ok is False and res.error.code == "wallet.batch_expired"

    async def test_import_batch_reports_each_row(self, ctx: RpcContext) -> None:
        await call("wallet.setup", {"password": PASSWORD}, ctx)
        await call("wallet.import", {"label": "Known", "privateKey": "0x" + "11" * 32}, ctx)
        keys = ["0x" + "22" * 32, "not a key", "11" * 32, "33" * 32, "0x" + "22" * 32]
        res = await call("wallet.importBatch", {"privateKeys": keys}, ctx)
        assert res.ok, res.error
        statuses = [(r["index"], r["status"]) for r in res.payload["results"]]
        assert statuses == [
            (0, "imported"),
            (1, "invalid"),
            (2, "exists"),
            (3, "imported"),
            (4, "exists"),
        ]
        assert res.payload["imported"] == 2
        # No row echoes a key back.
        assert "22" * 32 not in str(res.payload) and "33" * 32 not in str(res.payload)
        labels = [w["label"] for w in res.payload["wallets"]]
        assert labels == [f"{w['address'][:5]}_{w['address'][-4:]}" for w in res.payload["wallets"]]
        listed = await call("wallet.list", {}, ctx)
        assert len(listed.payload["wallets"]) == 3
        assert (await call("wallet.importBatch", {"privateKeys": []}, ctx)).ok is False
        assert (await call("wallet.importBatch", {"privateKeys": "abc"}, ctx)).ok is False
        too_many = await call("wallet.importBatch", {"privateKeys": ["x"] * 101}, ctx)
        assert too_many.ok is False

    async def test_batch_methods_are_operator_only(
        self, ctx: RpcContext, stack: dict[str, Any]
    ) -> None:
        await call("wallet.setup", {"password": PASSWORD}, ctx)
        made = await call("wallet.createBatch", {"count": 1}, ctx)
        agent = _agent_ctx(stack)
        for method, params in [
            ("wallet.createBatch", {"count": 2}),
            ("wallet.exportBatch", {"batchId": made.payload["batchId"], "touchId": True}),
            ("wallet.importBatch", {"privateKeys": ["0x" + "44" * 32]}),
        ]:
            res = await call(method, params, agent)
            assert res.ok is False, method
            assert res.error.code == "trading.operator_required", (method, res.error)
