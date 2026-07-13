from __future__ import annotations

import pytest

from agentos.engine.commands import (
    DEFAULT_REGISTRY,
    ExecutionKind,
    Surface,
    parse_surface,
)


@pytest.mark.parametrize(
    ("raw", "surface"),
    [
        ("web_chat", Surface.WEB_CHAT),
        ("web", Surface.WEB_CHAT),
        ("cli_gateway", Surface.CLI_GATEWAY),
        ("tui", Surface.CLI_GATEWAY),
        ("cli", Surface.CLI_GATEWAY),
        ("cli_standalone", Surface.CLI_STANDALONE),
        ("channel", Surface.CHANNEL),
    ],
)
def test_parse_surface_accepts_new_and_legacy_names(raw: str, surface: Surface) -> None:
    assert parse_surface(raw) is surface


def test_usage_execution_surfaces_and_methods() -> None:
    cmd = DEFAULT_REGISTRY.find("/usage")
    assert cmd is not None

    assert cmd.surfaces == frozenset(
        {Surface.WEB_CHAT, Surface.CLI_GATEWAY, Surface.CHANNEL}
    )
    assert cmd.execution_for(Surface.CLI_STANDALONE) is None

    for surface in (Surface.WEB_CHAT, Surface.CLI_GATEWAY, Surface.CHANNEL):
        execution = cmd.execution_for(surface)
        assert execution is not None
        assert execution.kind is ExecutionKind.RPC
        assert execution.action == "usage.status"
        assert execution.rpc_method == "usage.status"


def test_deprecated_rpc_properties_project_channel_execution() -> None:
    cmd = DEFAULT_REGISTRY.find("/history", Surface.CHANNEL)
    assert cmd is not None

    assert cmd.rpc_method == "chat.history"
    assert cmd.rpc_params is not None
    envelope = type("Envelope", (), {"session_key": "session-123"})()
    assert cmd.rpc_params(envelope) == {"sessionKey": "session-123"}


def test_file_is_cli_gateway_local_action_only() -> None:
    cmd = DEFAULT_REGISTRY.find("/file", Surface.CLI_GATEWAY)
    assert cmd is not None
    assert cmd.surfaces == frozenset({Surface.CLI_GATEWAY})

    execution = cmd.execution_for(Surface.CLI_GATEWAY)
    assert execution is not None
    assert execution.kind is ExecutionKind.LOCAL
    assert execution.action == "cli.file"
    assert execution.rpc_method is None

    assert DEFAULT_REGISTRY.find("/file", Surface.WEB_CHAT) is None
    assert DEFAULT_REGISTRY.find("/file", Surface.CHANNEL) is None
    assert DEFAULT_REGISTRY.find("/file", Surface.CLI_STANDALONE) is None


def test_aliases_resolve_for_visible_surface_only() -> None:
    assert DEFAULT_REGISTRY.find("/clear", Surface.WEB_CHAT).name == "/reset"  # type: ignore[union-attr]
    assert DEFAULT_REGISTRY.find("/clear", Surface.CHANNEL).name == "/reset"  # type: ignore[union-attr]
    assert DEFAULT_REGISTRY.find("/session", Surface.CHANNEL).name == "/status"  # type: ignore[union-attr]
    assert DEFAULT_REGISTRY.find("/session", Surface.WEB_CHAT) is None


def test_model_execution_surfaces_and_methods() -> None:
    cmd = DEFAULT_REGISTRY.find("/model")
    assert cmd is not None

    assert cmd.surfaces == frozenset(
        {Surface.WEB_CHAT, Surface.CLI_GATEWAY, Surface.CLI_STANDALONE, Surface.CHANNEL}
    )

    for surface in (Surface.WEB_CHAT, Surface.CHANNEL):
        execution = cmd.execution_for(surface)
        assert execution is not None
        assert execution.kind is ExecutionKind.RPC
        assert execution.rpc_method == "models.list"

    for surface in (Surface.CLI_GATEWAY, Surface.CLI_STANDALONE):
        execution = cmd.execution_for(surface)
        assert execution is not None
        assert execution.kind is ExecutionKind.LOCAL
        assert execution.action == "model.list"
