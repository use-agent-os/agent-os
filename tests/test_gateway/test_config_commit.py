"""Regression coverage for transactional gateway config commits."""

from __future__ import annotations

import tomllib
from typing import Any

import pytest

import agentos.gateway.rpc_config  # noqa: F401  ensure registration
import agentos.gateway.rpc_onboarding  # noqa: F401  ensure registration
from agentos.gateway.auth import Principal
from agentos.gateway.config import GatewayConfig
from agentos.gateway.config_persist import (
    get_runtime_overrides,
    persist_config,
    set_runtime_overrides,
)
from agentos.gateway.rpc import RpcContext, get_dispatcher


class _CapturingSelector:
    def __init__(self) -> None:
        self.synced: list[Any] = []

    def sync_primary(self, config: Any) -> None:
        self.synced.append(config)


def _ctx(
    config: GatewayConfig,
    *,
    scope: str = "operator.admin",
    selector: Any = None,
) -> RpcContext:
    return RpcContext(
        conn_id="config-commit-test",
        config=config,
        provider_selector=selector,
        principal=Principal(
            role="operator",
            scopes=frozenset({scope}),
            is_owner=scope == "operator.admin",
            authenticated=True,
        ),
    )


@pytest.mark.asyncio
async def test_config_patch_does_not_touch_runtime_when_persistence_fails(
    tmp_path, monkeypatch
) -> None:
    target = tmp_path / "config.toml"
    config = GatewayConfig(
        config_path=str(target),
        llm={"provider": "openrouter", "model": "old-model", "api_key": "old-key"},
    )
    selector = _CapturingSelector()

    def fail_persist(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr("agentos.gateway.config_commit.persist_config", fail_persist)
    result = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"llm.model": "new-model"}},
        _ctx(config, selector=selector),
    )

    assert result.error is not None
    assert result.error.code == "INTERNAL_ERROR"
    assert config.llm.model == "old-model"
    assert selector.synced == []
    assert not target.exists()


@pytest.mark.asyncio
async def test_onboarding_does_not_touch_runtime_when_persistence_fails(
    tmp_path, monkeypatch
) -> None:
    target = tmp_path / "config.toml"
    config = GatewayConfig(
        config_path=str(target),
        llm={"provider": "openrouter", "model": "old-model", "api_key": "old-key"},
    )
    selector = _CapturingSelector()

    def fail_persist(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr("agentos.gateway.config_commit.persist_config", fail_persist)
    result = await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {"providerId": "deepseek", "model": "new-model", "apiKey": "new-key"},
        _ctx(config, selector=selector),
    )

    assert result.error is not None
    assert result.error.code == "INTERNAL_ERROR"
    assert config.llm.provider == "openrouter"
    assert config.llm.model == "old-model"
    assert selector.synced == []
    assert not target.exists()


@pytest.mark.asyncio
async def test_onboarding_explicit_key_replaces_runtime_env_secret(tmp_path) -> None:
    target = tmp_path / "config.toml"
    config = GatewayConfig(
        config_path=str(target),
        llm={"provider": "openrouter", "model": "old-model", "api_key": "from-env"},
    )
    config.mark_runtime_secret("llm.api_key")
    selector = _CapturingSelector()

    result = await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {"providerId": "openrouter", "model": "new-model", "apiKey": "stored-key"},
        _ctx(config, selector=selector),
    )

    assert result.error is None, result.error
    assert config.llm.api_key == "stored-key"
    assert "llm.api_key" not in config._runtime_secret_paths
    assert selector.synced[0].api_key == "stored-key"
    persisted = tomllib.loads(target.read_text(encoding="utf-8"))
    assert persisted["llm"]["api_key"] == "stored-key"


@pytest.mark.asyncio
async def test_config_snapshot_is_read_scoped_coherent_and_redacted(tmp_path) -> None:
    config = GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        llm={"provider": "openrouter", "model": "model", "api_key": "super-secret"},
    )

    result = await get_dispatcher().dispatch(
        "r1",
        "config.snapshot",
        {},
        _ctx(config, scope="operator.read"),
    )

    assert result.error is None, result.error
    assert set(result.payload) == {
        "config",
        "catalog",
        "status",
        "readiness",
        "revision",
        "configPath",
        "pendingRestart",
        "restartReasons",
        "diskDiverged",
        "writeBlocked",
    }
    assert result.payload["config"]["llm"]["api_key"] == "[redacted]"
    assert "super-secret" not in str(result.payload)
    assert "providers" in result.payload["catalog"]
    assert result.payload["readiness"]["coreReady"] is True
    assert result.payload["revision"].startswith("sha256:")
    assert len(result.payload["revision"]) == len("sha256:") + 64
    assert result.payload["pendingRestart"] is False
    assert result.payload["restartReasons"] == []
    assert result.payload["diskDiverged"] is False
    assert result.payload["writeBlocked"] is False

    catalog = await get_dispatcher().dispatch(
        "r2",
        "onboarding.catalog",
        {},
        _ctx(config, scope="operator.read"),
    )
    assert catalog.error is None, catalog.error
    assert result.payload["catalog"] == catalog.payload


@pytest.mark.asyncio
async def test_config_snapshot_derives_status_from_one_active_config(
    tmp_path, monkeypatch
) -> None:
    import agentos.gateway.rpc_onboarding as rpc_onboarding

    config = GatewayConfig(config_path=str(tmp_path / "config.toml"))
    calls = 0

    def active_config(_ctx: RpcContext) -> GatewayConfig:
        nonlocal calls
        calls += 1
        return config

    monkeypatch.setattr(rpc_onboarding, "_active_config", active_config)
    context = RpcContext(
        conn_id="config-snapshot-test",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.read"}),
            is_owner=False,
            authenticated=True,
        ),
    )

    result = await get_dispatcher().dispatch("r1", "config.snapshot", {}, context)

    assert result.error is None, result.error
    assert calls == 1
    assert result.payload["configPath"] == str(tmp_path / "config.toml")


@pytest.mark.asyncio
async def test_expected_revision_rejects_stale_config_write(tmp_path) -> None:
    config = GatewayConfig(config_path=str(tmp_path / "config.toml"))
    context = _ctx(config)
    snapshot = await get_dispatcher().dispatch("r1", "config.snapshot", {}, context)
    assert snapshot.error is None, snapshot.error
    revision = snapshot.payload["revision"]

    first = await get_dispatcher().dispatch(
        "r2",
        "config.patch",
        {
            "patches": {"prompt_cache.mode": "on"},
            "expectedRevision": revision,
        },
        context,
    )
    assert first.error is None, first.error
    assert config.prompt_cache.mode == "on"

    stale = await get_dispatcher().dispatch(
        "r3",
        "config.patch",
        {
            "patches": {"prompt_cache.mode": "off"},
            "expectedRevision": revision,
        },
        context,
    )
    assert stale.error is not None
    assert stale.error.code == "INVALID_REQUEST"
    assert "revision mismatch" in stale.error.message
    assert config.prompt_cache.mode == "on"


@pytest.mark.asyncio
async def test_external_disk_edit_blocks_stale_runtime_writes(tmp_path) -> None:
    target = tmp_path / "config.toml"
    runtime_config = GatewayConfig(config_path=str(target))
    persist_config(runtime_config)

    external_config = runtime_config.model_copy(deep=True)
    external_config.prompt_cache.mode = "on"
    persist_config(external_config)
    external_bytes = target.read_bytes()
    context = _ctx(runtime_config)

    snapshot = await get_dispatcher().dispatch("r1", "config.snapshot", {}, context)
    assert snapshot.error is None, snapshot.error
    assert snapshot.payload["config"]["prompt_cache"]["mode"] == "auto"
    assert snapshot.payload["revision"] is None
    assert snapshot.payload["diskDiverged"] is True
    assert snapshot.payload["writeBlocked"] is True

    write = await get_dispatcher().dispatch(
        "r2",
        "config.patch",
        {"patches": {"prompt_cache.mode": "off"}},
        context,
    )
    assert write.error is not None
    assert write.error.code == "INVALID_REQUEST"
    assert "diverged" in write.error.message
    assert runtime_config.prompt_cache.mode == "auto"
    assert target.read_bytes() == external_bytes


@pytest.mark.asyncio
async def test_external_edit_to_boot_override_field_is_not_masked(tmp_path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("debug = false\n", encoding="utf-8")
    set_runtime_overrides({"debug": False})
    try:
        runtime_config = GatewayConfig(debug=True, config_path=str(target))
        aligned = await get_dispatcher().dispatch(
            "r1",
            "config.snapshot",
            {},
            _ctx(runtime_config),
        )
        assert aligned.error is None, aligned.error
        assert aligned.payload["diskDiverged"] is False

        target.write_text("debug = true\n", encoding="utf-8")
        external_bytes = target.read_bytes()
        context = _ctx(runtime_config)
        snapshot = await get_dispatcher().dispatch("r2", "config.snapshot", {}, context)
        assert snapshot.error is None, snapshot.error
        assert snapshot.payload["revision"] is None
        assert snapshot.payload["diskDiverged"] is True
        assert snapshot.payload["writeBlocked"] is True

        write = await get_dispatcher().dispatch(
            "r3",
            "config.patch",
            {"patches": {"prompt_cache.mode": "on"}},
            context,
        )
        assert write.error is not None
        assert write.error.code == "INVALID_REQUEST"
        assert target.read_bytes() == external_bytes
    finally:
        set_runtime_overrides(None)


@pytest.mark.asyncio
async def test_explicit_override_edit_survives_later_unrelated_write(tmp_path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("debug = false\n", encoding="utf-8")
    set_runtime_overrides({"debug": False})
    try:
        runtime_config = GatewayConfig(debug=True, config_path=str(target))
        context = _ctx(runtime_config)

        explicit = await get_dispatcher().dispatch(
            "r1",
            "config.patch",
            {"patches": {"debug": True}},
            context,
        )
        assert explicit.error is None, explicit.error
        assert "debug" not in get_runtime_overrides()
        assert tomllib.loads(target.read_text(encoding="utf-8"))["debug"] is True

        unrelated = await get_dispatcher().dispatch(
            "r2",
            "config.patch",
            {"patches": {"prompt_cache.mode": "on"}},
            context,
        )
        assert unrelated.error is None, unrelated.error
        saved = tomllib.loads(target.read_text(encoding="utf-8"))
        assert saved["debug"] is True
        assert saved["prompt_cache"]["mode"] == "on"
    finally:
        set_runtime_overrides(None)


@pytest.mark.asyncio
async def test_disk_coherence_uses_gateway_environment_settings(
    tmp_path, monkeypatch
) -> None:
    target = tmp_path / "config.toml"
    target.write_text("", encoding="utf-8")
    monkeypatch.setenv("AGENTOS_GATEWAY_DEBUG", "true")
    runtime_config = GatewayConfig.load(target)
    assert runtime_config.debug is True

    snapshot = await get_dispatcher().dispatch(
        "r1",
        "config.snapshot",
        {},
        _ctx(runtime_config),
    )

    assert snapshot.error is None, snapshot.error
    assert snapshot.payload["diskDiverged"] is False
    assert snapshot.payload["writeBlocked"] is False
    assert isinstance(snapshot.payload["revision"], str)


@pytest.mark.asyncio
async def test_env_sourced_auth_credentials_are_never_frozen(tmp_path, monkeypatch) -> None:
    target = tmp_path / "config.toml"
    target.write_text('[auth]\nmode = "token"\n', encoding="utf-8")
    monkeypatch.setenv("AGENTOS_AUTH_TOKEN", "env-token")
    monkeypatch.setenv("AGENTOS_AUTH_PASSWORD", "env-password")
    runtime_config = GatewayConfig.load(target)
    assert runtime_config.auth.token == "env-token"
    assert runtime_config.auth.password == "env-password"
    assert {"auth.token", "auth.password"} <= runtime_config._runtime_secret_paths
    context = _ctx(runtime_config)

    snapshot = await get_dispatcher().dispatch("r1", "config.snapshot", {}, context)
    assert snapshot.error is None, snapshot.error
    assert snapshot.payload["diskDiverged"] is False

    write = await get_dispatcher().dispatch(
        "r2",
        "config.patch",
        {"patches": {"prompt_cache.mode": "on"}},
        context,
    )
    assert write.error is None, write.error
    saved = tomllib.loads(target.read_text(encoding="utf-8"))
    assert "token" not in saved["auth"]
    assert "password" not in saved["auth"]
    assert runtime_config.auth.token == "env-token"
    assert runtime_config.auth.password == "env-password"


@pytest.mark.asyncio
async def test_unmodeled_external_toml_key_blocks_writes(tmp_path) -> None:
    target = tmp_path / "config.toml"
    runtime_config = GatewayConfig(config_path=str(target))
    persist_config(runtime_config)
    with target.open("a", encoding="utf-8") as stream:
        stream.write('\nexternal_unmodeled_key = "preserve-me"\n')
    external_bytes = target.read_bytes()
    context = _ctx(runtime_config)

    snapshot = await get_dispatcher().dispatch("r1", "config.snapshot", {}, context)
    assert snapshot.error is None, snapshot.error
    assert snapshot.payload["diskDiverged"] is True
    assert snapshot.payload["writeBlocked"] is True
    assert snapshot.payload["revision"] is None

    write = await get_dispatcher().dispatch(
        "r2",
        "config.patch",
        {"patches": {"prompt_cache.mode": "on"}},
        context,
    )
    assert write.error is not None
    assert write.error.code == "INVALID_REQUEST"
    assert target.read_bytes() == external_bytes


@pytest.mark.asyncio
async def test_snapshot_reports_pending_restart_reasons(tmp_path) -> None:
    config = GatewayConfig(config_path=str(tmp_path / "config.toml"))
    context = _ctx(config)

    patched = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"memory.embedding.provider": "none"}},
        context,
    )
    assert patched.error is None, patched.error
    assert patched.payload["restartRequired"] is True

    snapshot = await get_dispatcher().dispatch("r2", "config.snapshot", {}, context)
    assert snapshot.error is None, snapshot.error
    assert snapshot.payload["pendingRestart"] is True
    assert snapshot.payload["restartReasons"] == ["memory"]


@pytest.mark.asyncio
async def test_boot_captured_task_runtime_change_requires_restart(tmp_path) -> None:
    config = GatewayConfig(config_path=str(tmp_path / "config.toml"))
    context = _ctx(config)

    patched = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"task_runtime.max_concurrency": 8}},
        context,
    )

    assert patched.error is None, patched.error
    assert patched.payload["restartRequired"] is True
    snapshot = await get_dispatcher().dispatch("r2", "config.snapshot", {}, context)
    assert snapshot.error is None, snapshot.error
    assert "task_runtime" in snapshot.payload["restartReasons"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        ("task_runtime.channel_inflight_cap", 16, "task_runtime"),
        ("state_dir", "state-next", "state_dir"),
        ("auth.allow_unauthenticated_public", True, "gateway_bind"),
    ],
)
async def test_additional_boot_captured_changes_require_restart(
    tmp_path, path: str, value: Any, reason: str
) -> None:
    config = GatewayConfig(config_path=str(tmp_path / "config.toml"))
    context = _ctx(config)

    patched = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {path: value}},
        context,
    )

    assert patched.error is None, patched.error
    assert patched.payload["restartRequired"] is True
    snapshot = await get_dispatcher().dispatch("r2", "config.snapshot", {}, context)
    assert snapshot.error is None, snapshot.error
    assert reason in snapshot.payload["restartReasons"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("memory.source", "state"),
        ("memory.repair_enabled", False),
        ("memory.repair_interval_seconds", 90.0),
        ("memory.repair_max_items_per_tick", 9),
        ("memory.dream.enabled", True),
        ("memory.dream.auto_schedule", True),
        ("memory.dream.interval_h", 12),
    ],
)
async def test_boot_built_memory_topology_changes_require_restart(
    tmp_path, path: str, value: Any
) -> None:
    config = GatewayConfig(config_path=str(tmp_path / "config.toml"))
    context = _ctx(config)

    patched = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {path: value}},
        context,
    )

    assert patched.error is None, patched.error
    assert patched.payload["restartRequired"] is True
    snapshot = await get_dispatcher().dispatch("r2", "config.snapshot", {}, context)
    assert snapshot.error is None, snapshot.error
    assert "memory" in snapshot.payload["restartReasons"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        ("tools.trusted_fake_ip_cidrs", ["198.18.0.0/24"], "tools_runtime"),
        ("skills.allow_bundled", False, "skill_loader"),
        ("skills.managed_dir", "managed-skills", "skill_loader"),
        ("skills.extra_dirs", ["extra-skills"], "skill_loader"),
    ],
)
async def test_boot_captured_tools_and_skill_loader_changes_require_restart(
    tmp_path, path: str, value: Any, reason: str
) -> None:
    config = GatewayConfig(config_path=str(tmp_path / "config.toml"))
    context = _ctx(config)

    patched = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {path: value}},
        context,
    )

    assert patched.error is None, patched.error
    assert patched.payload["restartRequired"] is True
    snapshot = await get_dispatcher().dispatch("r2", "config.snapshot", {}, context)
    assert snapshot.error is None, snapshot.error
    assert reason in snapshot.payload["restartReasons"]


@pytest.mark.asyncio
async def test_live_skill_filter_change_does_not_require_restart(tmp_path) -> None:
    config = GatewayConfig(config_path=str(tmp_path / "config.toml"))

    patched = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"skills.filter_enabled": False}},
        _ctx(config),
    )

    assert patched.error is None, patched.error
    assert patched.payload["restartRequired"] is False


@pytest.mark.asyncio
async def test_search_hot_apply_runs_after_persist(tmp_path, monkeypatch) -> None:
    target = tmp_path / "config.toml"
    config = GatewayConfig(config_path=str(target))
    observed: dict[str, Any] = {}

    def capture_search(**kwargs: Any) -> None:
        observed.update(kwargs)
        persisted = tomllib.loads(target.read_text(encoding="utf-8"))
        assert persisted["search_provider"] == "brave"

    monkeypatch.setattr("agentos.tools.builtin.web.configure_search", capture_search)
    result = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"search_provider": "brave"}},
        _ctx(config),
    )

    assert result.error is None, result.error
    assert observed["provider_name"] == "brave"
    assert config.search_provider == "brave"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["config.patch", "config.apply"])
async def test_config_writes_reject_direct_auth_credential_changes(tmp_path, method) -> None:
    target = tmp_path / "config.toml"
    config = GatewayConfig(
        config_path=str(target),
        auth={"mode": "token", "token": "keep-token", "password": "keep-password"},
    )
    persist_config(config)
    before = target.read_bytes()
    params = (
        {"patch": {"auth": {"token": "replace-token"}}}
        if method == "config.patch"
        else {"config": {"auth": {"mode": "token", "token": "replace-token"}}}
    )

    result = await get_dispatcher().dispatch("r1", method, params, _ctx(config))

    assert result.error is not None
    assert result.error.code == "INVALID_REQUEST"
    assert "auth.token" in result.error.message
    assert config.auth.token == "keep-token"
    assert target.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["config.set", "config.patch", "config.apply"])
async def test_config_writes_reject_unsafe_public_auth_mode(
    tmp_path, method: str
) -> None:
    target = tmp_path / "config.toml"
    config = GatewayConfig(
        config_path=str(target),
        host="0.0.0.0",
        auth={"mode": "token", "token": "keep-token"},
    )
    persist_config(config)
    before = target.read_bytes()
    if method == "config.set":
        params = {"path": "auth.mode", "value": "none"}
    elif method == "config.patch":
        params = {"patches": {"auth.mode": "none"}}
    else:
        payload = config.to_public_dict()
        payload["auth"]["mode"] = "none"
        params = {"config": payload}

    result = await get_dispatcher().dispatch("r1", method, params, _ctx(config))

    assert result.error is not None
    assert result.error.code == "INVALID_REQUEST"
    assert "non-loopback bind" in result.error.message.lower()
    assert config.auth.mode == "token"
    assert config.auth.token == "keep-token"
    assert target.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["config.set", "config.patch", "config.apply"])
async def test_config_writes_reject_token_mode_without_provisioned_token(
    tmp_path, method: str
) -> None:
    target = tmp_path / "config.toml"
    config = GatewayConfig(config_path=str(target), auth={"mode": "none"})
    persist_config(config)
    before = target.read_bytes()
    if method == "config.set":
        params = {"path": "auth.mode", "value": "token"}
    elif method == "config.patch":
        params = {"patches": {"auth.mode": "token"}}
    else:
        payload = config.to_public_dict()
        payload["auth"]["mode"] = "token"
        params = {"config": payload}

    result = await get_dispatcher().dispatch("r1", method, params, _ctx(config))

    assert result.error is not None
    assert result.error.code == "INVALID_REQUEST"
    assert "provision" in result.error.message.lower()
    assert config.auth.mode == "none"
    assert config.auth.token is None
    assert target.read_bytes() == before


@pytest.mark.asyncio
async def test_config_apply_accepts_redacted_auth_round_trip(tmp_path) -> None:
    target = tmp_path / "config.toml"
    config = GatewayConfig(
        config_path=str(target),
        auth={"mode": "token", "token": "keep-token", "password": "keep-password"},
    )
    payload = config.to_public_dict()
    payload["debug"] = True

    result = await get_dispatcher().dispatch(
        "r1",
        "config.apply",
        {"config": payload},
        _ctx(config),
    )

    assert result.error is None, result.error
    assert config.auth.token == "keep-token"
    assert config.auth.password == "keep-password"
    persisted = tomllib.loads(target.read_text(encoding="utf-8"))
    assert persisted["auth"]["token"] == "keep-token"
    assert persisted["auth"]["password"] == "keep-password"


@pytest.mark.asyncio
async def test_config_path_is_read_only_and_cannot_retarget_writes(tmp_path) -> None:
    target = tmp_path / "config.toml"
    attacker_target = tmp_path / "nested" / "attacker.toml"
    config = GatewayConfig(config_path=str(target))
    persist_config(config)

    set_result = await get_dispatcher().dispatch(
        "r0",
        "config.set",
        {"path": "config_path", "value": str(attacker_target)},
        _ctx(config),
    )
    assert set_result.error is not None
    assert set_result.error.code == "INVALID_REQUEST"
    assert "config_path" in set_result.error.message

    direct = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"config_path": str(attacker_target)}},
        _ctx(config),
    )
    assert direct.error is not None
    assert direct.error.code == "INVALID_REQUEST"
    assert "config_path" in direct.error.message

    merged = await get_dispatcher().dispatch(
        "r2",
        "config.patch",
        {"patch": {"config_path": str(attacker_target), "debug": True}},
        _ctx(config),
    )
    assert merged.error is None, merged.error
    assert config.config_path == str(target)
    assert config.debug is True
    assert not attacker_target.exists()
    assert tomllib.loads(target.read_text(encoding="utf-8"))["debug"] is True

    payload = config.to_public_dict()
    payload["config_path"] = str(attacker_target)
    payload["debug"] = False
    applied = await get_dispatcher().dispatch(
        "r3",
        "config.apply",
        {"config": payload},
        _ctx(config),
    )
    assert applied.error is None, applied.error
    assert config.config_path == str(target)
    assert not attacker_target.exists()
    assert tomllib.loads(target.read_text(encoding="utf-8"))["debug"] is False


@pytest.mark.asyncio
async def test_distribution_version_is_read_only_runtime_metadata(tmp_path) -> None:
    target = tmp_path / "config.toml"
    config = GatewayConfig(config_path=str(target))
    original_version = config.version
    persist_config(config)
    context = _ctx(config)

    set_result = await get_dispatcher().dispatch(
        "r1",
        "config.set",
        {"path": "version", "value": "operator-supplied"},
        context,
    )
    assert set_result.error is not None
    assert set_result.error.code == "INVALID_REQUEST"

    patch_result = await get_dispatcher().dispatch(
        "r2",
        "config.patch",
        {"patches": {"version": "operator-supplied"}},
        context,
    )
    assert patch_result.error is not None
    assert patch_result.error.code == "INVALID_REQUEST"

    merged = await get_dispatcher().dispatch(
        "r3",
        "config.patch",
        {"patch": {"version": "operator-supplied", "debug": True}},
        context,
    )
    assert merged.error is None, merged.error
    assert config.version == original_version

    payload = config.to_public_dict()
    payload["version"] = "operator-supplied"
    payload["debug"] = False
    applied = await get_dispatcher().dispatch(
        "r4",
        "config.apply",
        {"config": payload},
        context,
    )
    assert applied.error is None, applied.error
    assert config.version == original_version
    saved = tomllib.loads(target.read_text(encoding="utf-8"))
    assert saved["version"] == original_version
