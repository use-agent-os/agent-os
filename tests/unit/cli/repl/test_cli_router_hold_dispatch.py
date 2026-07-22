"""Issue #46: /c0-/c3 and /auto dispatch on the CLI gateway + standalone surfaces.

These pin the dispatch behavior added to:

* ``slash_gateway.handle_gateway_slash_command`` — reuses the existing
  ``router.hold.set`` / ``router.hold.clear`` RPC over the wire.
* ``slash_standalone.handle_standalone_slash_command`` — mutates the
  in-process ``RouterControlHoldStore`` directly via the TurnRunner.

Both paths also update ``ChatSessionState.router_hold_tier`` so the
bottom-toolbar tier chip stays in sync.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agentos.cli.chat.session_state import ChatSessionState
from agentos.router_control import RouterControlHoldStore

# ---------------------------------------------------------------------------
# Gateway mode: /c3 / /auto call router.hold.set / router.hold.clear RPCs
# ---------------------------------------------------------------------------


class _RouterClient:
    """Minimal gateway client recording the public ``call`` RPC surface."""

    def __init__(
        self,
        *,
        hold_set_payload: dict[str, Any] | None = None,
        hold_set_error: Exception | None = None,
        hold_clear_error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self._hold_set_payload = hold_set_payload or {
            "tier": "c3",
            "model": "claude-opus-4-8",
            "provider": "openrouter",
            "targetId": "tier:c3",
            "ttlSeconds": 600,
        }
        self._hold_set_error = hold_set_error
        self._hold_clear_error = hold_clear_error

    async def call(self, method: str, params: dict | None = None) -> Any:
        self.calls.append((method, dict(params) if params else None))
        if method == "router.hold.set":
            if self._hold_set_error is not None:
                raise self._hold_set_error
            return dict(self._hold_set_payload)
        if method == "router.hold.clear":
            if self._hold_clear_error is not None:
                raise self._hold_clear_error
            return {"cleared": True}
        raise AssertionError(f"unexpected RPC: {method}")


def _gateway_context(client: _RouterClient, *, session_key: str = "agent:main:main"):
    from agentos.cli.tui.adapters.slash_gateway import GatewaySlashContext

    state = ChatSessionState(session_key=session_key, model="openai/gpt")
    return GatewaySlashContext(
        state=state,
        client=client,  # type: ignore[arg-type]
        elevated_state={"mode": None},
    )


@pytest.mark.asyncio
async def test_gateway_c3_calls_router_hold_set_and_sets_state_tier() -> None:
    from agentos.cli.tui.adapters.slash_gateway import handle_gateway_slash_command

    client = _RouterClient()
    context = _gateway_context(client)

    handled = await handle_gateway_slash_command("/c3", context)

    assert handled is True
    assert client.calls == [("router.hold.set", {"key": "agent:main:main", "tier": "c3"})]
    # The state mirrors the pin so the bottom toolbar shows "tier:c3".
    assert context.state.router_hold_tier == "c3"


@pytest.mark.asyncio
async def test_gateway_auto_calls_router_hold_clear_and_drops_state_tier() -> None:
    from agentos.cli.tui.adapters.slash_gateway import handle_gateway_slash_command

    client = _RouterClient()
    context = _gateway_context(client)
    context.state.router_hold_tier = "c3"  # pre-existing pin

    handled = await handle_gateway_slash_command("/auto", context)

    assert handled is True
    assert client.calls == [("router.hold.clear", {"key": "agent:main:main"})]
    assert context.state.router_hold_tier is None


@pytest.mark.asyncio
async def test_gateway_c3_surfaces_rpc_error_without_raising() -> None:
    """A router.disabled RPC error must surface as a readable message, not crash."""
    from agentos.cli.tui.adapters.slash_gateway import handle_gateway_slash_command

    client = _RouterClient(hold_set_error=RuntimeError("router.disabled: Pilot Router is disabled"))
    context = _gateway_context(client)

    handled = await handle_gateway_slash_command("/c3", context)

    assert handled is True
    # The error path must not flip the tier chip on.
    assert context.state.router_hold_tier is None


@pytest.mark.asyncio
async def test_gateway_auto_surfaces_rpc_error_without_raising() -> None:
    from agentos.cli.tui.adapters.slash_gateway import handle_gateway_slash_command

    client = _RouterClient(hold_clear_error=RuntimeError("router.unavailable"))
    context = _gateway_context(client)

    handled = await handle_gateway_slash_command("/auto", context)

    assert handled is True


# ---------------------------------------------------------------------------
# Standalone mode: /c3 / /auto mutate the in-process hold store
# ---------------------------------------------------------------------------


def _router_cfg(enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        enabled=enabled,
        tiers={
            "c0": {"model": "gemini-flash-lite", "provider": "openrouter"},
            "c3": {"model": "claude-opus-4-8", "provider": "openrouter"},
        },
    )


def _standalone_context(
    *,
    store: RouterControlHoldStore,
    cfg: object | None,
    session_key: str = "agent:main:standalone:abcd1234",
):
    from agentos.cli.tui.adapters.slash_standalone import StandaloneSlashContext

    runner = SimpleNamespace(
        router_control_hold_store=store,
        router_control_config=cfg,
    )
    state = ChatSessionState(session_key=session_key, model="openai/gpt")
    return StandaloneSlashContext(
        state=state,
        session_key=session_key,
        model=state.model,
        tool_ctx=object(),
        slash_services=SimpleNamespace(
            create_session=None,
            read_transcript=None,
            truncate_session=None,
            compact_session=None,
            compact_with_result=None,
            flush_transcript=None,
            config=None,
            provider_selector=None,
        ),
        turn_runner=runner,
        build_tool_ctx=lambda _key: object(),
        replace_session=lambda **_updates: None,
    )


@pytest.mark.asyncio
async def test_standalone_c3_pins_hold_in_process_and_sets_state_tier() -> None:
    from agentos.cli.tui.adapters.slash_standalone import handle_standalone_slash_command

    store = RouterControlHoldStore()
    context = _standalone_context(store=store, cfg=_router_cfg())

    handled = await handle_standalone_slash_command("/c3", context)

    assert handled is True
    hold = store.get_valid("agent:main:standalone:abcd1234")
    assert hold is not None
    assert hold.tier == "c3"
    assert hold.target_id == "tier:c3"
    assert context.state.router_hold_tier == "c3"


@pytest.mark.asyncio
async def test_standalone_auto_clears_hold_and_drops_state_tier() -> None:
    from agentos.cli.tui.adapters.slash_standalone import handle_standalone_slash_command

    store = RouterControlHoldStore()
    context = _standalone_context(store=store, cfg=_router_cfg())
    # Install a hold up front so /auto has something to clear.
    await handle_standalone_slash_command("/c3", context)
    assert store.get_valid("agent:main:standalone:abcd1234") is not None

    handled = await handle_standalone_slash_command("/auto", context)

    assert handled is True
    assert store.get_valid("agent:main:standalone:abcd1234") is None
    assert context.state.router_hold_tier is None


@pytest.mark.asyncio
async def test_standalone_c3_reports_disabled_router_without_raising() -> None:
    from agentos.cli.tui.adapters.slash_standalone import handle_standalone_slash_command

    store = RouterControlHoldStore()
    context = _standalone_context(store=store, cfg=_router_cfg(enabled=False))

    handled = await handle_standalone_slash_command("/c3", context)

    assert handled is True
    # No hold installed, no tier chip flipped.
    assert store.get_valid("agent:main:standalone:abcd1234") is None
    assert context.state.router_hold_tier is None


@pytest.mark.asyncio
async def test_standalone_unknown_tier_reports_without_raising() -> None:
    from agentos.cli.tui.adapters.slash_standalone import handle_standalone_slash_command

    store = RouterControlHoldStore()
    context = _standalone_context(store=store, cfg=_router_cfg())

    # /c2 is not in the configured tiers above; the LOCAL handler must
    # surface a readable error instead of raising.
    handled = await handle_standalone_slash_command("/c2", context)

    assert handled is True
    assert store.get_valid("agent:main:standalone:abcd1234") is None
    assert context.state.router_hold_tier is None
