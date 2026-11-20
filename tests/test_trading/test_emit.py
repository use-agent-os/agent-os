"""Trading events never fail the action that emits them.

``_emit`` runs after the work is done (the wallet exists, the order is
booked); the broadcast is a courtesy to whoever is listening. Both of its log
lines passed ``event=`` beside the positional event name, which structlog
refuses with ``TypeError`` at every log level — so with no transport wired
(library and CLI-only use) every emit raised, and so did any emit whose
transport failed.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
import structlog

from agentos.gateway.config import GatewayConfig
from agentos.trading.ledger import Ledger
from agentos.trading.prices import PriceService
from agentos.trading.service import TradingService
from agentos.trading.vault import Vault
from tests.test_trading.conftest import PASSWORD
from tests.test_trading.fakes import fake_sign_tx


@pytest.fixture(params=[logging.DEBUG, logging.WARNING, logging.CRITICAL])
def log_level(request: pytest.FixtureRequest) -> int:
    level = int(request.param)
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(level))
    return level


def _service(
    gateway_config: GatewayConfig,
    vault: Vault,
    ledger: Ledger,
    http: httpx.AsyncClient,
    **kwargs: Any,
) -> TradingService:
    return TradingService(
        gateway_config,
        vault=vault,
        ledger=ledger,
        prices=PriceService(http=http, ttl_s=0),
        http=http,
        sign_tx=fake_sign_tx,
        background=False,
        **kwargs,
    )


async def test_creating_a_wallet_with_no_transport_wired_succeeds(
    log_level: int,
    gateway_config: GatewayConfig,
    vault: Vault,
    ledger: Ledger,
    http: httpx.AsyncClient,
) -> None:
    service = _service(gateway_config, vault, ledger, http)
    try:
        service.vault.setup(PASSWORD, "auto")
        wallet = await service.create_wallet("Main")
        assert wallet["address"].startswith("0x")
        assert [record.address for record in service.vault.list()] == [wallet["address"]]
    finally:
        await service.stop()


async def test_a_transport_that_fails_does_not_fail_the_action(
    log_level: int,
    gateway_config: GatewayConfig,
    vault: Vault,
    ledger: Ledger,
    http: httpx.AsyncClient,
) -> None:
    calls: list[str] = []

    async def broken(event: str, payload: dict[str, Any]) -> None:
        calls.append(event)
        raise ConnectionError("no connections")

    service = _service(gateway_config, vault, ledger, http, broadcast=broken)
    try:
        service.vault.setup(PASSWORD, "auto")
        wallet = await service.create_wallet("Main")
        assert wallet["address"].startswith("0x")
        assert "trading.changed" in calls
    finally:
        await service.stop()
