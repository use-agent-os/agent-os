from __future__ import annotations

import pytest

import agentos.gateway.rpc_config  # noqa: F401  ensures registration
from agentos.gateway.auth import Principal
from agentos.gateway.config import GatewayConfig
from agentos.gateway.rpc import RpcContext, get_dispatcher


def _admin_ctx(config: GatewayConfig) -> RpcContext:
    return RpcContext(
        conn_id="t",
        config=config,
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
    )


def _memory_remote_secret_config(config_path) -> GatewayConfig:
    return GatewayConfig(
        config_path=str(config_path),
        memory={
            "embedding": {
                "provider": "openai",
                "remote": {
                    "api_key": "mem-secret",
                    "headers": {
                        "Authorization": "Bearer header-secret",
                        "x-api-key": "header-key",
                    },
                },
            }
        },
    )


def _assert_memory_remote_secrets_preserved(
    cfg: GatewayConfig,
    config_path,
) -> None:
    assert cfg.memory.embedding.remote.api_key == "mem-secret"
    headers = cfg.memory.embedding.remote.headers
    assert headers["Authorization"] == "Bearer header-secret"
    assert headers["x-api-key"] == "header-key"
    persisted = config_path.read_text(encoding="utf-8")
    assert "[redacted]" not in persisted


@pytest.mark.asyncio
async def test_config_patch_memory_embedding_reports_restart_required(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"memory.embedding.provider": "none"}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True


@pytest.mark.asyncio
async def test_config_patch_same_memory_embedding_does_not_report_restart_required(tmp_path):
    cfg = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        memory={"embedding": {"provider": "none"}},
    )
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"memory.embedding.provider": "none"}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is False


@pytest.mark.asyncio
async def test_config_patch_permissions_default_reports_restart_required(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"permissions.default_mode": "full"}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True


@pytest.mark.asyncio
async def test_config_patch_same_permissions_default_does_not_report_restart_required(
    tmp_path,
):
    cfg = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        permissions={"default_mode": "bypass"},
    )
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"permissions.default_mode": "bypass"}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is False


@pytest.mark.asyncio
async def test_config_apply_sandbox_posture_reports_restart_required(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    payload = cfg.model_dump(mode="python")
    payload["sandbox"]["sandbox"] = True
    payload["sandbox"]["security_grading"] = True
    payload["permissions"]["default_mode"] = "off"

    res = await get_dispatcher().dispatch(
        "r1",
        "config.apply",
        {"config": payload},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True


@pytest.mark.asyncio
async def test_config_patch_memory_retrieval_mode_reports_restart_required(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"memory.retrieval_mode": "fts_only"}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True


@pytest.mark.asyncio
async def test_config_apply_memory_retrieval_mode_reports_restart_required(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    payload = cfg.model_dump(mode="python")
    payload["memory"]["retrieval_mode"] = "fts_only"

    res = await get_dispatcher().dispatch(
        "r1",
        "config.apply",
        {"config": payload},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True


@pytest.mark.asyncio
async def test_config_patch_auth_mode_reports_restart_required(tmp_path):
    """auth.mode is applied to AuthMiddleware live, but the startup guard and
    the captured bind posture only re-evaluate on restart — so a hot change
    must be flagged restart-required so the operator isn't misled."""
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))  # mode defaults to "none"
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"auth.mode": "token"}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True


@pytest.mark.asyncio
async def test_config_patch_host_reports_restart_required(tmp_path):
    """host changes do NOT rebind the live socket, so hot-applying host must be
    flagged restart-required (the process still listens on the old address)."""
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))  # host defaults to 127.0.0.1
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"host": "0.0.0.0"}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True


@pytest.mark.asyncio
async def test_config_patch_same_auth_and_host_does_not_report_restart_required(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"prompt_cache.mode": "auto"}},  # unrelated, non-restart change
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is False


@pytest.mark.asyncio
async def test_config_apply_auth_mode_reports_restart_required(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    payload = cfg.model_dump(mode="python")
    payload["auth"]["mode"] = "token"

    res = await get_dispatcher().dispatch(
        "r1",
        "config.apply",
        {"config": payload},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True


@pytest.mark.asyncio
async def test_config_get_redacts_memory_remote_api_key(tmp_path):
    cfg = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        memory={"embedding": {"remote": {"api_key": "mem-secret"}}},
    )

    res = await get_dispatcher().dispatch("r1", "config.get", {}, _admin_ctx(cfg))

    assert res.error is None, res.error
    assert res.payload["memory"]["embedding"]["remote"]["api_key"] == "[redacted]"


@pytest.mark.asyncio
async def test_config_get_does_not_redact_empty_memory_remote_api_key(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))

    res = await get_dispatcher().dispatch("r1", "config.get", {}, _admin_ctx(cfg))

    assert res.error is None, res.error
    assert res.payload["memory"]["embedding"]["remote"]["api_key"] is None


@pytest.mark.asyncio
async def test_config_get_redacts_memory_remote_header_api_key(tmp_path):
    cfg = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        memory={
            "embedding": {
                "remote": {
                    "headers": {
                        "Authorization": "Bearer secret",
                        "x-api-key": "header-secret",
                    }
                }
            }
        },
    )

    res = await get_dispatcher().dispatch("r1", "config.get", {}, _admin_ctx(cfg))

    assert res.error is None, res.error
    headers = res.payload["memory"]["embedding"]["remote"]["headers"]
    assert headers["Authorization"] == "[redacted]"
    assert headers["x-api-key"] == "[redacted]"


@pytest.mark.asyncio
async def test_config_apply_preserves_redacted_memory_remote_secrets(tmp_path):
    config_path = tmp_path / "c.toml"
    cfg = _memory_remote_secret_config(config_path)
    dispatcher = get_dispatcher()

    get_res = await dispatcher.dispatch("r1", "config.get", {}, _admin_ctx(cfg))
    assert get_res.error is None, get_res.error
    assert get_res.payload["memory"]["embedding"]["remote"]["api_key"] == "[redacted]"

    apply_res = await dispatcher.dispatch(
        "r2",
        "config.apply",
        {"config": get_res.payload},
        _admin_ctx(cfg),
    )

    assert apply_res.error is None, apply_res.error
    _assert_memory_remote_secrets_preserved(cfg, config_path)


@pytest.mark.asyncio
async def test_config_patch_preserves_redacted_memory_object_secrets(tmp_path):
    config_path = tmp_path / "c.toml"
    cfg = _memory_remote_secret_config(config_path)
    dispatcher = get_dispatcher()

    get_res = await dispatcher.dispatch("r1", "config.get", {}, _admin_ctx(cfg))
    assert get_res.error is None, get_res.error

    memory_payload = get_res.payload["memory"]
    assert memory_payload["embedding"]["remote"]["api_key"] == "[redacted]"
    patch_res = await dispatcher.dispatch(
        "r2",
        "config.patch",
        {"patches": {"memory": memory_payload}},
        _admin_ctx(cfg),
    )

    assert patch_res.error is None, patch_res.error
    _assert_memory_remote_secrets_preserved(cfg, config_path)


@pytest.mark.asyncio
async def test_config_apply_keeps_literal_redacted_for_non_secret_memory_fields(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    payload = cfg.model_dump(mode="python")
    payload["memory"]["embedding"]["remote"]["model"] = "[redacted]"

    res = await get_dispatcher().dispatch(
        "r1",
        "config.apply",
        {"config": payload},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert cfg.memory.embedding.remote.model == "[redacted]"


@pytest.mark.asyncio
async def test_config_patch_keeps_literal_redacted_for_non_secret_memory_fields(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))

    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"memory.embedding.remote.model": "[redacted]"}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert cfg.memory.embedding.remote.model == "[redacted]"


@pytest.mark.asyncio
async def test_config_patch_rejects_direct_redacted_secret_marker(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))

    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"memory.embedding.remote.api_key": "[redacted]"}},
        _admin_ctx(cfg),
    )

    assert res.error is not None
    assert res.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_config_set_rejects_direct_redacted_secret_marker(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))

    res = await get_dispatcher().dispatch(
        "r1",
        "config.set",
        {"path": "memory.embedding.remote.api_key", "value": "[redacted]"},
        _admin_ctx(cfg),
    )

    assert res.error is not None
    assert res.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_config_set_rejects_redacted_secret_marker_inside_object(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))

    res = await get_dispatcher().dispatch(
        "r1",
        "config.set",
        {
            "path": "memory.embedding.remote",
            "value": {"api_key": "[redacted]"},
        },
        _admin_ctx(cfg),
    )

    assert res.error is not None
    assert res.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_config_set_preserves_redacted_memory_remote_object_secrets(tmp_path):
    config_path = tmp_path / "c.toml"
    cfg = _memory_remote_secret_config(config_path)
    dispatcher = get_dispatcher()

    get_res = await dispatcher.dispatch("r1", "config.get", {}, _admin_ctx(cfg))
    assert get_res.error is None, get_res.error

    remote_payload = get_res.payload["memory"]["embedding"]["remote"]
    remote_payload["base_url"] = "https://embeddings.example/v1"
    set_res = await dispatcher.dispatch(
        "r2",
        "config.set",
        {"path": "memory.embedding.remote", "value": remote_payload},
        _admin_ctx(cfg),
    )

    assert set_res.error is None, set_res.error
    assert cfg.memory.embedding.remote.base_url == "https://embeddings.example/v1"
    _assert_memory_remote_secrets_preserved(cfg, config_path)


@pytest.mark.asyncio
async def test_config_patch_rejects_explicit_remote_without_memory_api_key(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))

    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"memory.embedding.provider": "openai"}},
        _admin_ctx(cfg),
    )

    assert res.error is not None
    assert res.error.code == "INVALID_REQUEST"
    assert "memory.embedding.remote.api_key" in res.error.message


@pytest.mark.asyncio
async def test_config_apply_rejects_explicit_remote_without_memory_api_key(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    payload = cfg.model_dump(mode="python")
    payload["memory"]["embedding"]["provider"] = "openai"

    res = await get_dispatcher().dispatch(
        "r1",
        "config.apply",
        {"config": payload},
        _admin_ctx(cfg),
    )

    assert res.error is not None
    assert res.error.code == "INVALID_REQUEST"
    assert "memory.embedding.remote.api_key" in res.error.message


def _slack_channel_entry(name: str = "work", token: str = "xoxb-test-token") -> dict:
    return {
        "type": "slack",
        "name": name,
        "enabled": True,
        "agent_id": "main",
        "debounce_window_s": 0.0,
        "status_reactions_enabled": False,
        "token": token,
        "slack_channel_id": "C001",
        "signing_secret": None,
        "reply_in_thread": False,
    }


@pytest.mark.asyncio
async def test_config_patch_channels_reports_restart_required(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"channels.channels": [_slack_channel_entry()]}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True


@pytest.mark.asyncio
async def test_config_patch_same_channels_does_not_report_restart_required(tmp_path):
    cfg = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        channels={"channels": [_slack_channel_entry()]},
    )
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"channels.channels": [_slack_channel_entry()]}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is False


@pytest.mark.asyncio
async def test_config_apply_channels_token_change_reports_restart_required(tmp_path):
    cfg = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        channels={"channels": [_slack_channel_entry(token="xoxb-old")]},
    )
    payload = cfg.model_dump(mode="python")
    payload["channels"]["channels"][0]["token"] = "xoxb-new"

    res = await get_dispatcher().dispatch(
        "r1",
        "config.apply",
        {"config": payload},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True


@pytest.mark.asyncio
async def test_config_apply_unchanged_channels_does_not_report_restart_required(tmp_path):
    cfg = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        channels={"channels": [_slack_channel_entry()]},
    )
    payload = cfg.model_dump(mode="python")

    res = await get_dispatcher().dispatch(
        "r1",
        "config.apply",
        {"config": payload},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is False


@pytest.mark.asyncio
async def test_config_set_channel_token_rotation_reports_restart_required(tmp_path):
    cfg = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        channels={"channels": [_slack_channel_entry(token="xoxb-old")]},
    )
    res = await get_dispatcher().dispatch(
        "r1",
        "config.set",
        {
            "path": "channels.channels",
            "value": [_slack_channel_entry(token="xoxb-new")],
        },
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True


@pytest.mark.asyncio
async def test_config_patch_disabling_channel_reports_restart_required(tmp_path):
    cfg = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        channels={"channels": [_slack_channel_entry()]},
    )
    disabled_entry = {**_slack_channel_entry(), "enabled": False}
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"channels.channels": [disabled_entry]}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True


@pytest.mark.asyncio
async def test_config_patch_accepts_curated_memory_budget_fields(tmp_path):
    """Setup UI's Memory card patches these three MemoryConfig fields directly.

    They gate prompt assembly per-turn (read fresh via ``getattr`` in
    ``engine/runtime.py`` and ``tools/builtin/memory_tools.py`` on the same
    in-memory config object ``config.patch`` mutates in place), not anything
    constructed once at boot -- so, unlike ``memory.embedding``/
    ``memory.retrieval_mode``, they should NOT require a gateway restart.
    """
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {
            "patches": {
                "memory.curated_memory_char_limit": 5000,
                "memory.curated_user_char_limit": 2500,
                "memory.inject_limit": 7000,
            }
        },
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is False
    assert cfg.memory.curated_memory_char_limit == 5000
    assert cfg.memory.curated_user_char_limit == 2500
    assert cfg.memory.inject_limit == 7000


@pytest.mark.asyncio
async def test_config_patch_selecting_memory_provider_reports_restart_required(tmp_path):
    """The external memory-provider manager is built once at boot, so selecting a
    provider (or retuning its mem0 sub-settings) requires a gateway restart.
    """
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"memory.provider.name": "mem0"}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True
    assert cfg.memory.provider.name == "mem0"


@pytest.mark.asyncio
async def test_config_patch_resetting_memory_provider_to_null_reports_restart_required(
    tmp_path,
):
    """Round-trip: selecting a provider then patching name back to null (disabled)
    must also succeed and require a restart, same as selecting one in the first place.
    """
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    selected = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"memory.provider.name": "mem0"}},
        _admin_ctx(cfg),
    )
    assert selected.error is None, selected.error
    assert cfg.memory.provider.name == "mem0"

    res = await get_dispatcher().dispatch(
        "r2",
        "config.patch",
        {"patches": {"memory.provider.name": None}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True
    assert cfg.memory.provider.name is None


@pytest.mark.asyncio
async def test_config_patch_dotted_leaf_preserves_sibling_keys(tmp_path):
    """The Form view flattens nested config into dotted leaves and saves each
    touched leaf as one dot-path patch. Setting a single leaf must not wipe its
    siblings under the same parent object.
    """
    cfg = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        memory={"curated_memory_char_limit": 4000, "curated_user_char_limit": 1500},
    )
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"memory.provider.name": "mem0"}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert cfg.memory.provider.name == "mem0"
    # Sibling scalars under memory survive the single-leaf patch.
    assert cfg.memory.curated_memory_char_limit == 4000
    assert cfg.memory.curated_user_char_limit == 1500


@pytest.mark.asyncio
async def test_config_patch_nested_dict_deep_merges_without_wiping_siblings(tmp_path):
    """A nested single-leaf merge patch {"memory": {"provider": {"name": "mem0"}}}
    must deep-merge, leaving unrelated memory/provider fields intact.
    """
    cfg = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        memory={
            "curated_memory_char_limit": 4000,
            "provider": {"mem0": {"llm_model": "qwen3:8b"}},
        },
    )
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patch": {"memory": {"provider": {"name": "mem0"}}}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert cfg.memory.provider.name == "mem0"
    # Deep merge keeps the sibling scalar and the nested mem0 sub-object.
    assert cfg.memory.curated_memory_char_limit == 4000
    assert cfg.memory.provider.mem0.llm_model == "qwen3:8b"


@pytest.mark.asyncio
async def test_config_patch_retuning_mem0_settings_reports_restart_required(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"memory.provider.mem0.llm_model": "qwen3:8b"}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True
    assert cfg.memory.provider.mem0.llm_model == "qwen3:8b"
