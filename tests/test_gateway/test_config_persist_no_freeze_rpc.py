"""A one-off CLI ``--listen``/``--debug`` (or break-glass ``mode=none``) recorded
in the process-global runtime-override map must NEVER be frozen into config.toml
by a later RPC write — through EITHER live writer path (rpc_config config.patch
AND rpc_onboarding). An explicit change of the overridden field still persists.
"""

from __future__ import annotations

import tomllib

import pytest

import agentos.gateway.rpc_config  # noqa: F401  ensures config.* registration
import agentos.gateway.rpc_onboarding  # noqa: F401  ensures onboarding.* registration
from agentos.gateway.auth import Principal
from agentos.gateway.config import AuthConfig, GatewayConfig
from agentos.gateway.config_persist import set_runtime_overrides
from agentos.gateway.rpc import RpcContext, get_dispatcher


@pytest.fixture(autouse=True)
def _reset_overrides():
    set_runtime_overrides(None)
    yield
    set_runtime_overrides(None)


def _admin_ctx(config: GatewayConfig) -> RpcContext:
    return RpcContext(
        conn_id="t",
        config=config,
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin", "operator.write"}),
            is_owner=True,
            authenticated=True,
        ),
    )


def _seed_disk(path, data: dict) -> None:
    import tomli_w

    with open(path, "wb") as f:
        tomli_w.dump(data, f)


def _read(path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _running_config(path, **overrides) -> GatewayConfig:
    """A live config as run_gateway leaves it: host/debug overridden in memory,
    config_path set to the on-disk file."""
    base = {"config_path": str(path)}
    base.update(overrides)
    return GatewayConfig(**base)


# --- rpc_config path -------------------------------------------------------


@pytest.mark.asyncio
async def test_config_patch_unrelated_field_does_not_freeze_cli_host_override(tmp_path):
    """[finding a — rpc_config] --listen 0.0.0.0 recorded at boot; a config.patch
    of an UNRELATED field must not freeze the public bind into config.toml."""
    cfg_path = tmp_path / "config.toml"
    _seed_disk(cfg_path, {"host": "127.0.0.1", "debug": False})

    # run_gateway recorded the on-disk originals before overriding host in memory.
    set_runtime_overrides({"host": "127.0.0.1", "port": 18791, "debug": False})
    cfg = _running_config(cfg_path, host="0.0.0.0")

    res = await get_dispatcher().dispatch(
        "r1", "config.patch", {"patches": {"agentos_router.enabled": True}}, _admin_ctx(cfg)
    )
    assert res.error is None, res.error

    saved = _read(cfg_path)
    assert saved.get("host", "127.0.0.1") == "127.0.0.1"  # NOT frozen to 0.0.0.0
    assert saved.get("debug", False) is False


@pytest.mark.asyncio
async def test_config_patch_explicit_debug_persists_despite_override(tmp_path):
    """[finding b] An explicit config.patch of debug=true DOES persist — the
    explicit_paths exception wins over the runtime-override restore."""
    cfg_path = tmp_path / "config.toml"
    _seed_disk(cfg_path, {"host": "127.0.0.1", "debug": False})

    set_runtime_overrides({"host": "127.0.0.1", "port": 18791, "debug": False})
    cfg = _running_config(cfg_path, host="0.0.0.0", debug=True)

    res = await get_dispatcher().dispatch(
        "r1", "config.patch", {"patches": {"debug": True}}, _admin_ctx(cfg)
    )
    assert res.error is None, res.error

    saved = _read(cfg_path)
    assert saved["debug"] is True  # explicit change persisted
    assert saved.get("host", "127.0.0.1") == "127.0.0.1"  # host still not frozen


@pytest.mark.asyncio
async def test_break_glass_mode_none_not_frozen_by_later_patch_safe(tmp_path):
    """[finding c] break-glass set auth.mode=none in memory; a later
    config.patch.safe of an unrelated safe path must not freeze mode=none."""
    cfg_path = tmp_path / "config.toml"
    _seed_disk(cfg_path, {"auth": {"mode": "token", "token": "keep"}})

    # break-glass recorded the on-disk auth originals at the prompt.
    set_runtime_overrides(
        {"auth.mode": "token", "auth.allow_unauthenticated_public": False}
    )
    cfg = _running_config(
        cfg_path,
        host="0.0.0.0",
        auth=AuthConfig(mode="none", allow_unauthenticated_public=True, token="keep"),
    )

    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch.safe",
        {"patches": {"agentos_router.enabled": True}},
        _admin_ctx(cfg),
    )
    assert res.error is None, res.error

    saved = _read(cfg_path)
    assert saved["auth"]["mode"] == "token"  # break-glass mode NOT frozen
    assert saved["auth"].get("allow_unauthenticated_public") in (False, None)


@pytest.mark.asyncio
async def test_config_apply_yaml_mode_does_not_freeze_break_glass_auth(tmp_path):
    """[verify finding] YAML-mode Save seeds the payload from the RUNNING config
    (which carries the break-glass mode=none + opt-in), so config.apply must
    subtract the FULL override-map key set from explicit_paths — not just
    host/port — or it freezes the transient break-glass posture and disables
    auth on disk permanently."""
    cfg_path = tmp_path / "config.toml"
    _seed_disk(cfg_path, {"auth": {"mode": "token", "token": "keep"}})

    set_runtime_overrides(
        {"auth.mode": "token", "auth.allow_unauthenticated_public": False}
    )
    cfg = _running_config(
        cfg_path,
        auth=AuthConfig(mode="none", allow_unauthenticated_public=True, token="keep"),
    )
    # YAML-mode Save: the payload IS the running config (echoes the overrides).
    payload = cfg.model_dump(mode="python")

    res = await get_dispatcher().dispatch(
        "r1", "config.apply", {"config": payload}, _admin_ctx(cfg)
    )
    assert res.error is None, res.error

    saved = _read(cfg_path)
    assert saved["auth"]["mode"] == "token"  # break-glass mode NOT frozen
    assert saved["auth"].get("allow_unauthenticated_public") in (False, None)


@pytest.mark.asyncio
async def test_config_apply_yaml_mode_does_not_freeze_cli_debug(tmp_path):
    """Same class: a one-off --debug must not survive a YAML-mode Save of an
    unrelated field."""
    cfg_path = tmp_path / "config.toml"
    _seed_disk(cfg_path, {"debug": False})

    set_runtime_overrides({"debug": False})
    cfg = _running_config(cfg_path, debug=True)
    payload = cfg.model_dump(mode="python")

    res = await get_dispatcher().dispatch(
        "r1", "config.apply", {"config": payload}, _admin_ctx(cfg)
    )
    assert res.error is None, res.error
    assert _read(cfg_path).get("debug", False) is False


# --- rpc_onboarding path ---------------------------------------------------


@pytest.mark.asyncio
async def test_onboarding_configure_does_not_freeze_cli_host_override(tmp_path):
    """[finding a — onboarding] An onboarding mutation persists via the same
    override-aware writer, so a CLI --listen 0.0.0.0 is not frozen either."""
    cfg_path = tmp_path / "config.toml"
    _seed_disk(cfg_path, {"host": "127.0.0.1", "debug": False})

    set_runtime_overrides({"host": "127.0.0.1", "port": 18791, "debug": False})
    cfg = _running_config(cfg_path, host="0.0.0.0")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.search.configure",
        {"providerId": "duckduckgo"},
        _admin_ctx(cfg),
    )
    assert res.error is None, res.error

    saved = _read(cfg_path)
    assert saved.get("host", "127.0.0.1") == "127.0.0.1"  # NOT frozen to 0.0.0.0
