"""``POST /api/v1/tools/invoke`` (#3695)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from starlette.testclient import TestClient

from agentos.gateway.app import create_gateway_app
from agentos.gateway.config import AuthConfig, GatewayConfig, ToolsConfig, ToolsInvokeConfig
from agentos.gateway.tools_invoke import HARD_DENY_TOOLS, IdempotencyCache, is_hard_denied
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import ToolSpec, UnsupportedSurfaceError, current_tool_context

TOKEN = "test-token"


def _registry(calls: list[dict[str, Any]]) -> ToolRegistry:
    registry = ToolRegistry()

    async def lookup(query: str = "", action: str | None = None) -> str:
        ctx = current_tool_context.get()
        calls.append(
            {
                "query": query,
                "action": action,
                "interaction_mode": str(ctx.interaction_mode) if ctx else None,
                "elevated": ctx.elevated if ctx else "missing",
                "session_key": ctx.session_key if ctx else None,
            }
        )
        return json.dumps({"answer": f"found {query}"})

    async def needs_human(**_: Any) -> str:
        return json.dumps(
            {"status": "approval_pending", "approval_id": "abc123", "message": "Approve it."}
        )

    async def boom(**_: Any) -> str:
        raise RuntimeError("secret detail sk-should-not-leak")

    async def refuses_unattended(**_: Any) -> str:
        # What exec_command does on an unattended run that needs approval:
        # it refuses before queuing anything.
        raise UnsupportedSurfaceError(
            "Tool 'refuses_unattended' requires human approval, but this run is unattended."
        )

    async def no_surface(**_: Any) -> str:
        raise UnsupportedSurfaceError("This tool needs an interactive chat.")

    async def never(**_: Any) -> str:
        raise AssertionError("must not run")

    registry.register(
        ToolSpec(
            name="lookup",
            description="read-only lookup",
            parameters={"query": {"type": "string"}, "action": {"type": "string"}},
            read_only=True,
        ),
        lookup,
    )
    registry.register(
        ToolSpec(
            name="needs_human",
            description="",
            parameters={},
            requires_approval=True,
            read_only=True,
        ),
        needs_human,
    )
    registry.register(ToolSpec(name="boom", description="", parameters={}, read_only=True), boom)
    registry.register(
        ToolSpec(name="refuses_unattended", description="", parameters={}, read_only=True),
        refuses_unattended,
    )
    registry.register(
        ToolSpec(name="no_surface", description="", parameters={}, read_only=True), no_surface
    )
    for name in (
        "exec_command",
        "write_file",
        "sessions_spawn",
        "cron",
        "gateway",
        "trading_swap",
        "memory",
        "http_request",
        "projects_create",
        "session_rename",
        "image_generate",
        "tts",
        "browser",
        "router_control",
    ):
        registry.register(ToolSpec(name=name, description="", parameters={}), never)
    # Declared read-only but on the name floor all the same.
    registry.register(
        ToolSpec(name="music_generate", description="", parameters={}, read_only=True), never
    )
    # Not on any list, but its registration does not say it is read-only.
    registry.register(ToolSpec(name="mcp__notes__append", description="", parameters={}), never)
    return registry


def _client(
    registry: ToolRegistry,
    *,
    tools: ToolsConfig | None = None,
    auth: bool = True,
) -> TestClient:
    config = GatewayConfig(
        auth=AuthConfig(mode="token", token=TOKEN),
        tools=tools or ToolsConfig(),
    )
    headers = {"authorization": f"Bearer {TOKEN}"} if auth else {}
    return TestClient(
        create_gateway_app(config, tool_registry=registry),
        base_url="http://localhost",
        headers=headers,
    )


def _invoke(client: TestClient, body: dict[str, Any], **headers: str) -> Any:
    return client.post("/api/v1/tools/invoke", json=body, headers=headers)


class TestInvoke:
    def test_allowed_tool_executes(self) -> None:
        calls: list[dict[str, Any]] = []
        resp = _invoke(_client(_registry(calls)), {"tool": "lookup", "args": {"query": "agentos"}})
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["ok"] is True
        assert payload["result"] == {"answer": "found agentos"}
        # Unattended and sandboxed, exactly like a background run.
        assert calls == [
            {
                "query": "agentos",
                "action": None,
                "interaction_mode": "unattended",
                "elevated": None,
                "session_key": None,
            }
        ]

    def test_action_and_session_key_reach_the_tool(self) -> None:
        calls: list[dict[str, Any]] = []
        resp = _invoke(
            _client(_registry(calls)),
            {"tool": "lookup", "action": "list", "session_key": "agent:main:webchat:x"},
        )
        assert resp.status_code == 200
        assert calls[0]["action"] == "list"
        assert calls[0]["session_key"] == "agent:main:webchat:x"

    def test_arguments_that_do_not_match_the_schema_are_a_bad_request(self) -> None:
        calls: list[dict[str, Any]] = []
        resp = _invoke(_client(_registry(calls)), {"tool": "lookup", "args": {"query": 42}})
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "INVALID_REQUEST"
        assert body["details"]["error_class"] == "ToolArgumentsRejected"
        assert "query: expected string, got integer 42" in body["error"]
        assert calls == []

    @pytest.mark.parametrize(
        "tool",
        [
            "exec_command",
            "write_file",
            "sessions_spawn",
            "cron",
            "gateway",
            "trading_swap",
            "memory",
            "http_request",
            "projects_create",
            "session_rename",
            "image_generate",
            "tts",
            "browser",
            "router_control",
            "music_generate",
            "mcp__notes__append",
        ],
    )
    def test_hard_denied_tool_is_404_like_an_unknown_one(self, tool: str) -> None:
        client = _client(_registry([]))
        denied = _invoke(client, {"tool": tool})
        unknown = _invoke(client, {"tool": "no_such_tool"})
        assert denied.status_code == unknown.status_code == 404
        assert denied.json() == unknown.json()

    def test_hard_deny_holds_even_when_config_allows_it(self) -> None:
        tools = ToolsConfig(allow=["exec_command", "lookup"], also_allow=["exec_command"])
        assert (
            _invoke(_client(_registry([]), tools=tools), {"tool": "exec_command"}).status_code
            == 404
        )

    def test_invoke_allow_lifts_one_tool(self) -> None:
        tools = ToolsConfig(invoke=ToolsInvokeConfig(allow=["write_file"]))
        client = _client(_registry([]), tools=tools)
        dry = _invoke(client, {"tool": "write_file", "dry_run": True})
        assert dry.status_code == 200
        assert dry.json()["allowed"] is True
        assert dry.json()["policy"]["invokeAllowOverride"] is True
        assert _invoke(client, {"tool": "exec_command"}).status_code == 404

    def test_a_tool_not_declared_read_only_needs_an_allow_entry(self) -> None:
        client = _client(_registry([]))
        assert _invoke(client, {"tool": "mcp__notes__append", "dry_run": True}).status_code == 404
        tools = ToolsConfig(invoke=ToolsInvokeConfig(allow=["mcp__notes__append"]))
        lifted = _invoke(
            _client(_registry([]), tools=tools), {"tool": "mcp__notes__append", "dry_run": True}
        )
        assert lifted.status_code == 200
        assert lifted.json()["allowed"] is True

    @pytest.mark.parametrize("arg", ["output_path", "save_path", "output_file", "destination"])
    def test_file_writing_arguments_are_refused(self, arg: str) -> None:
        calls: list[dict[str, Any]] = []
        resp = _invoke(
            _client(_registry(calls)), {"tool": "lookup", "args": {"query": "q", arg: "x.bin"}}
        )
        assert resp.status_code == 400
        assert arg in resp.json()["error"]
        assert calls == []

    def test_policy_denied_tool_is_403(self) -> None:
        tools = ToolsConfig(deny=["lookup"])
        resp = _invoke(_client(_registry([]), tools=tools), {"tool": "lookup"})
        assert resp.status_code == 403
        body = resp.json()
        # Typed shape: the category on top, the reason in details.
        assert body["code"] == "FORBIDDEN"
        assert body["details"]["code"] == "POLICY_DENIED"
        assert body["details"]["error_class"] == "PolicyDenied"

    def test_profile_allowlist_applies(self) -> None:
        tools = ToolsConfig(profile="minimal")
        resp = _invoke(_client(_registry([]), tools=tools), {"tool": "lookup"})
        assert resp.status_code == 403

    def test_dry_run_returns_the_policy_and_runs_nothing(self) -> None:
        calls: list[dict[str, Any]] = []
        resp = _invoke(_client(_registry(calls)), {"tool": "lookup", "dry_run": True})
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["dryRun"] is True
        assert payload["allowed"] is True
        assert payload["policy"]["readOnly"] is True
        assert payload["policy"]["interactionMode"] == "unattended"
        assert payload["policy"]["sandboxed"] is True
        assert "result" not in payload
        assert calls == []

    def test_dry_run_reports_a_policy_refusal(self) -> None:
        tools = ToolsConfig(deny=["lookup"])
        resp = _invoke(_client(_registry([]), tools=tools), {"tool": "lookup", "dryRun": True})
        assert resp.status_code == 200
        assert resp.json()["allowed"] is False
        assert resp.json()["errorClass"] == "PolicyDenied"

    def test_approval_requirement_is_403_with_the_approval_id(self) -> None:
        resp = _invoke(_client(_registry([])), {"tool": "needs_human"})
        assert resp.status_code == 403
        body = resp.json()
        assert body["code"] == "FORBIDDEN"
        assert body["details"] == {"code": "APPROVAL_REQUIRED", "approval_id": "abc123"}

    def test_a_tool_refusing_an_unattended_approval_is_403(self) -> None:
        """The shell refuses before queuing on an unattended run; that is an
        approval requirement too, without an id."""
        resp = _invoke(_client(_registry([])), {"tool": "refuses_unattended"})
        assert resp.status_code == 403, resp.text
        body = resp.json()
        assert body["code"] == "FORBIDDEN"
        assert body["details"] == {"code": "APPROVAL_REQUIRED"}
        assert "requires human approval" in body["error"]

    def test_another_unsupported_surface_is_a_tool_failure(self) -> None:
        resp = _invoke(_client(_registry([])), {"tool": "no_surface"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is False

    def test_tool_failure_is_reported_without_internals(self) -> None:
        resp = _invoke(_client(_registry([])), {"tool": "boom"})
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["ok"] is False
        assert payload["isError"] is True
        assert "sk-should-not-leak" not in resp.text

    def test_bearer_auth_is_required(self) -> None:
        resp = _invoke(_client(_registry([]), auth=False), {"tool": "lookup"})
        assert resp.status_code == 401

    def test_an_agent_bound_connection_is_refused(self) -> None:
        from agentos.gateway import agent_surface

        surface = agent_surface.get_agent_surface()
        token = surface.mint_token("agent:main:main", "main")
        resp = _invoke(
            _client(_registry([])), {"tool": "lookup"}, **{"x-agentos-agent-token": token}
        )
        assert resp.status_code == 403

    @pytest.mark.parametrize(
        "body",
        [{}, {"tool": ""}, {"tool": "lookup", "args": []}, {"tool": "lookup", "session_key": ""}],
    )
    def test_invalid_bodies(self, body: dict[str, Any]) -> None:
        assert _invoke(_client(_registry([])), body).status_code == 400


class TestIdempotency:
    def test_replay_returns_the_first_response(self) -> None:
        calls: list[dict[str, Any]] = []
        client = _client(_registry(calls))
        body = {"tool": "lookup", "args": {"query": "q"}, "idempotency_key": "k-1"}
        first = _invoke(client, body)
        second = _invoke(client, body)
        assert first.status_code == second.status_code == 200
        assert second.json()["idempotentReplay"] is True
        assert second.json()["result"] == first.json()["result"]
        assert len(calls) == 1

    def test_reusing_a_key_for_another_request_conflicts(self) -> None:
        client = _client(_registry([]))
        _invoke(client, {"tool": "lookup", "args": {"query": "a"}, "idempotency_key": "k-2"})
        conflict = _invoke(
            client, {"tool": "lookup", "args": {"query": "b"}, "idempotency_key": "k-2"}
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "INVALID_REQUEST"
        assert conflict.json()["details"] == {"code": "REQUEST_CONFLICT"}

    def test_entries_expire(self) -> None:
        now = [0.0]
        cache = IdempotencyCache(ttl_seconds=10, clock=lambda: now[0])

        async def scenario() -> list[str]:
            verdicts = []
            verdict, future = cache.claim("k", "f1")
            future.set_result((200, {}))
            verdicts.append(verdict)
            verdicts.append(cache.claim("k", "f2")[0])
            now[0] = 11.0
            verdicts.append(cache.claim("k", "f2")[0])
            return verdicts

        assert asyncio.run(scenario()) == ["new", "conflict", "new"]


def test_shipped_registry_only_exposes_read_only_tools_by_default() -> None:
    """Against the real registry: every tool the endpoint would run is declared
    read-only, and the ones the floor names stay out even if a flag changes."""
    import agentos.tools.builtin  # noqa: F401 - registers the built-in tools
    from agentos.gateway.tools_invoke import invocable
    from agentos.tools.registry import get_default_registry

    registry = get_default_registry()
    runnable = {name for name in registry.list_names() if invocable(name, registry.get(name))}
    assert runnable, "expected some read-only tools"
    assert all(registry.get(name).spec.read_only for name in runnable)
    for name in ("memory", "http_request", "image_generate", "browser", "exec_command"):
        assert name not in runnable
    assert {"read_file", "list_dir", "web_search"} <= runnable


def test_hard_deny_list_covers_the_documented_classes() -> None:
    for tool in (
        "exec",
        "exec_command",
        "bash",
        "execute_code",
        "write_file",
        "edit_file",
        "apply_patch",
        "sessions_spawn",
        "sessions_send",
        "cron",
        "gateway",
    ):
        assert tool in HARD_DENY_TOOLS
    assert is_hard_denied("trading_buy")
    assert not is_hard_denied("web_search")
    assert not is_hard_denied("exec_command", ["exec_command"])


class TestDenyListIsFileOnly:
    """#3695 hardening: a config RPC must not lift tools off the deny list."""

    def _ctx(self, tmp_path: Any, via: str | None) -> Any:
        from agentos.gateway.access import ConnectionSurface
        from agentos.gateway.agent_surface import AgentBinding
        from agentos.gateway.auth import AccessContext
        from agentos.gateway.rpc import RpcContext

        config = GatewayConfig(config_path=str(tmp_path / "agentos.toml"))
        return RpcContext(
            conn_id="c1",
            access=AccessContext(
                surface=ConnectionSurface.CONTROL,
                admitted=True,
                credential_verified=True,
                agent=AgentBinding(via=via) if via else None,
            ),
            config=config,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("via", ["token", "window", "unbound", None])
    @pytest.mark.parametrize(
        ("method", "params"),
        [
            ("config.set", {"path": "tools.invoke.allow", "value": ["exec_command"]}),
            ("config.patch", {"patches": {"tools.invoke.allow": ["exec_command"]}}),
            ("config.patch", {"patch": {"tools": {"invoke": {"allow": ["exec_command"]}}}}),
        ],
    )
    async def test_adding_an_allow_entry_is_refused(
        self, tmp_path: Any, via: str | None, method: str, params: dict[str, Any]
    ) -> None:
        from agentos.gateway.rpc import get_dispatcher

        ctx = self._ctx(tmp_path, via)
        res = await get_dispatcher().dispatch("1", method, params, ctx)
        assert res.ok is False
        assert res.error.code == "INVALID_REQUEST"
        assert ctx.config.tools.invoke.allow == []

    @pytest.mark.asyncio
    async def test_an_agent_cannot_even_clear_it(self, tmp_path: Any) -> None:
        from agentos.gateway.rpc import get_dispatcher

        ctx = self._ctx(tmp_path, "token")
        res = await get_dispatcher().dispatch(
            "1", "config.set", {"path": "hooks.lifecycle.shutdown", "value": []}, ctx
        )
        assert res.error.code == "INVALID_REQUEST"
        assert "agent" in res.error.message
