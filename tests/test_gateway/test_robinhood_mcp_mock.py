from __future__ import annotations

from typing import Any

import pytest

from agentos.mcp.client import MCPClient
from agentos.mcp.types import MCPServerConfig, MCPToolDef, MCPToolResult
from agentos.tools.registry import ToolRegistry


class FakeRobinhoodMCPClient(MCPClient):
    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def list_tools(self) -> list[MCPToolDef]:
        return [
            MCPToolDef(
                name="get_accounts",
                description="List accounts and identify dedicated Agentic account",
                input_schema={"properties": {}, "required": []},
            ),
            MCPToolDef(
                name="get_balances",
                description="Get current buying power or account balances",
                input_schema={
                    "properties": {"account_id": {"type": "string"}},
                    "required": ["account_id"],
                },
            ),
            MCPToolDef(
                name="get_positions",
                description="Get current positions for an account",
                input_schema={
                    "properties": {"account_id": {"type": "string"}},
                    "required": ["account_id"],
                },
            ),
            MCPToolDef(
                name="place_order",
                description="Place a trade order in the dedicated Agentic account",
                input_schema={
                    "properties": {
                        "account_id": {"type": "string"},
                        "symbol": {"type": "string"},
                        "side": {"type": "string"},
                        "quantity": {"type": "number"},
                        "price": {"type": "number"},
                    },
                    "required": ["account_id", "symbol", "side", "quantity"],
                },
            ),
            MCPToolDef(
                name="cancel_order",
                description="Cancel a pending order",
                input_schema={
                    "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"],
                },
            ),
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        if name == "get_accounts":
            return MCPToolResult(
                content=(
                    '[{"account_id": "RH_AGENTIC_123", '
                    '"name": "Robinhood Agentic Account", "eligible": true}]'
                )
            )
        elif name == "get_balances":
            return MCPToolResult(content='{"buying_power": 5000.00, "cash": 5000.00}')
        elif name == "get_positions":
            return MCPToolResult(
                content='[{"symbol": "AAPL", "quantity": 10, "market_value": 1750.00}]'
            )
        elif name == "place_order":
            return MCPToolResult(
                content=(
                    '{"order_id": "ord_999", "status": "submitted", '
                    '"symbol": "AAPL", "side": "buy"}'
                )
            )
        elif name == "cancel_order":
            return MCPToolResult(content='{"order_id": "ord_999", "status": "canceled"}')
        return MCPToolResult(content="unknown")


@pytest.mark.asyncio
async def test_robinhood_mcp_mock_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentos.mcp import discovery

    config = MCPServerConfig(
        name="robinhood-trading",
        transport="streamable_http",
        url="https://agent.robinhood.com/mcp/trading",
        oauth=True,
    )
    client = FakeRobinhoodMCPClient(config)
    monkeypatch.setattr(discovery, "create_client", lambda _config: client)

    registry = ToolRegistry()
    names = await discovery.discover_and_register(config, registry, owner="gateway")

    # Assert that all expected tools are registered with prefix mcp_
    assert "mcp_get_accounts" in names
    assert "mcp_get_balances" in names
    assert "mcp_get_positions" in names
    assert "mcp_place_order" in names
    assert "mcp_cancel_order" in names

    # Call get_accounts and verify the response
    accounts_tool = registry.get("mcp_get_accounts")
    assert accounts_tool is not None
    accounts_res = await accounts_tool.handler()
    assert "RH_AGENTIC_123" in accounts_res

    # Call get_balances and verify the response
    balances_tool = registry.get("mcp_get_balances")
    assert balances_tool is not None
    balances_res = await balances_tool.handler(account_id="RH_AGENTIC_123")
    assert "5000.00" in balances_res

    # Call place_order and verify the response
    place_tool = registry.get("mcp_place_order")
    assert place_tool is not None
    place_res = await place_tool.handler(
        account_id="RH_AGENTIC_123", symbol="AAPL", side="buy", quantity=1.0
    )
    assert "ord_999" in place_res
    assert "submitted" in place_res

    # Call cancel_order and verify the response
    cancel_tool = registry.get("mcp_cancel_order")
    assert cancel_tool is not None
    cancel_res = await cancel_tool.handler(order_id="ord_999")
    assert "canceled" in cancel_res

    await discovery.close_active_clients(owner="gateway")
