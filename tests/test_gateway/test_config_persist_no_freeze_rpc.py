"""A one-off CLI or transient in-memory config override recorded
in the process-global runtime-override map must NEVER be frozen into config.toml
by a later RPC write — through EITHER live writer path (rpc_config config.patch
AND rpc_onboarding). An explicit change of the overridden field still persists.
"""

from __future__ import annotations

import tomllib

import pytest

import agentos.gateway.rpc_config  # noqa: F401  ensures config.* registration
import agentos.gateway.rpc_onboarding  # noqa: F401  ensures onboarding.* registration
from agentos.gateway.config import AuthConfig, GatewayConfig
from agentos.gateway.config_persist import set_runtime_overrides
from agentos.gateway.rpc import RpcContext, get_dispatcher


@pytest.fixture(autouse=True)
def _reset_overrides():
    set_runtime_overrides(None)
    yield
    set_runtime_overrides(None)


def _control_ctx(config: GatewayConfig) -> RpcContext:
    return RpcContext(
        conn_id="t",
        config=config,
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
        "r1", "config.patch", {"patches": {"agentos_router.enabled": True}}, _control_ctx(cfg)
    )
    assert res.error is None, res.error

    saved = _read(cfg_path)
    assert saved.get("host", "127.0.0.1") == "127.0.0.1"  # NOT frozen to 0.0.0.0
    assert saved.get("debug", False) is False


@pytest.mark.asyncio
async def test_config_patch_echoing_readonly_host_does_not_freeze_bind(tmp_path):
    """[FIX 2 — rpc_config] A UI form can echo the display-only host/port in the
    same config.patch as a genuine edit. host/port are read-only paths (skipped
    when applied), but they used to leak into ``explicit_paths`` and short-circuit
    the runtime-override restore, freezing the transient public bind. Subtracting
    _READONLY_PATHS from explicit_paths keeps the bind restorable."""
    cfg_path = tmp_path / "config.toml"
    _seed_disk(cfg_path, {"host": "127.0.0.1", "port": 18791})

    set_runtime_overrides({"host": "127.0.0.1", "port": 18791, "debug": False})
    cfg = _running_config(cfg_path, host="0.0.0.0")

    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"host": "0.0.0.0", "port": 19999, "agentos_router.enabled": True}},
        _control_ctx(cfg),
    )
    assert res.error is None, res.error

    saved = _read(cfg_path)
    assert saved.get("host", "127.0.0.1") == "127.0.0.1"  # NOT frozen to 0.0.0.0
    assert saved.get("port", 18791) == 18791  # NOT frozen to 19999
    # The genuine (non-readonly) edit in the same call still persisted.
    assert saved["agentos_router"]["enabled"] is True


@pytest.mark.asyncio
async def test_config_patch_merge_echoing_readonly_host_does_not_freeze_bind(tmp_path):
    """[FIX 2 — merge path] The dict-merge branch of config.patch collects host
    from the merge payload into explicit_paths. It must be subtracted too."""
    cfg_path = tmp_path / "config.toml"
    _seed_disk(cfg_path, {"host": "127.0.0.1", "port": 18791})

    set_runtime_overrides({"host": "127.0.0.1", "port": 18791, "debug": False})
    cfg = _running_config(cfg_path, host="0.0.0.0")

    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patch": {"host": "0.0.0.0", "agentos_router": {"enabled": True}}},
        _control_ctx(cfg),
    )
    assert res.error is None, res.error

    saved = _read(cfg_path)
    assert saved.get("host", "127.0.0.1") == "127.0.0.1"  # NOT frozen
    assert saved["agentos_router"]["enabled"] is True


@pytest.mark.asyncio
async def test_config_patch_explicit_debug_persists_despite_override(tmp_path):
    """[finding b] An explicit config.patch of debug=true DOES persist — the
    explicit_paths exception wins over the runtime-override restore."""
    cfg_path = tmp_path / "config.toml"
    _seed_disk(cfg_path, {"host": "127.0.0.1", "debug": False})

    set_runtime_overrides({"host": "127.0.0.1", "port": 18791, "debug": False})
    cfg = _running_config(cfg_path, host="0.0.0.0", debug=True)

    res = await get_dispatcher().dispatch(
        "r1", "config.patch", {"patches": {"debug": True}}, _control_ctx(cfg)
    )
    assert res.error is None, res.error

    saved = _read(cfg_path)
    assert saved["debug"] is True  # explicit change persisted
    assert saved.get("host", "127.0.0.1") == "127.0.0.1"  # host still not frozen


@pytest.mark.asyncio
async def test_transient_auth_mode_not_frozen_by_later_patch_safe(tmp_path):
    """[finding c] a transient auth.mode=none in memory; a later
    config.patch.safe of an unrelated safe path must not freeze mode=none."""
    cfg_path = tmp_path / "config.toml"
    _seed_disk(cfg_path, {"auth": {"mode": "token", "token": "keep"}})

    # The runtime override map records the on-disk auth original.
    set_runtime_overrides(
        {
            "host": None,
            "port": None,
            "debug": None,
            "auth.mode": "token",
        }
    )
    cfg = _running_config(
        cfg_path,
        host="0.0.0.0",
        auth=AuthConfig(mode="none", token="keep"),
    )

    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch.safe",
        {"patches": {"agentos_router.enabled": True}},
        _control_ctx(cfg),
    )
    assert res.error is None, res.error

    saved = _read(cfg_path)
    assert saved["auth"]["mode"] == "token"  # transient mode NOT frozen


@pytest.mark.asyncio
async def test_config_apply_yaml_mode_does_not_freeze_transient_auth(tmp_path):
    """[verify finding] YAML-mode Save seeds the payload from the RUNNING config
    (which carries the transient mode=none), so config.apply must
    subtract the FULL override-map key set from explicit_paths — not just
    host/port — or it freezes the transient auth posture and disables
    auth on disk permanently."""
    cfg_path = tmp_path / "config.toml"
    _seed_disk(cfg_path, {"auth": {"mode": "token", "token": "keep"}})

    set_runtime_overrides({"auth.mode": "token"})
    cfg = _running_config(
        cfg_path,
        auth=AuthConfig(mode="none", token="keep"),
    )
    # YAML-mode Save: the payload IS the running config (echoes the overrides).
    payload = cfg.model_dump(mode="python")

    res = await get_dispatcher().dispatch(
        "r1", "config.apply", {"config": payload}, _control_ctx(cfg)
    )
    assert res.error is None, res.error

    saved = _read(cfg_path)
    assert saved["auth"]["mode"] == "token"  # transient mode NOT frozen


@pytest.mark.asyncio
async def test_config_apply_baseline_diff_persists_deliberate_auth_mode_edit(tmp_path):
    """[FIX 4] With a baseline_yaml, a field the user ACTUALLY changed vs the
    baseline they saw persists — even while that field has a runtime override.
    The round-2 blanket 'subtract the whole override keyset' made this
    impossible; the snapshot-baseline diff restores the ability to persist a
    deliberate YAML edit of an overridden field.

    Distinguishing setup: the on-disk original (trusted-proxy), the running echo
    (none, runtime override), and the deliberate edit (token) are all different, so
    the two strategies give different final states — round-2 restores trusted-proxy
    (wrong), baseline-diff persists token (correct)."""
    import yaml

    cfg_path = tmp_path / "config.toml"
    _seed_disk(cfg_path, {"auth": {"mode": "trusted-proxy", "token": "existing-token"}})

    # The runtime map recorded the on-disk auth original; running is mode=none.
    set_runtime_overrides({"auth.mode": "trusted-proxy"})
    cfg = _running_config(
        cfg_path,
        auth=AuthConfig(
            mode="none",
            token="existing-token",
        ),
    )
    # Baseline = what config.get showed (running config, auth echoed none).
    baseline = cfg.model_dump(mode="json")
    baseline["auth"]["token"] = "[redacted]"
    baseline_yaml = yaml.safe_dump(baseline)
    # User deliberately switches auth.mode none -> token in the YAML.
    edited = dict(baseline)
    edited["auth"] = dict(baseline["auth"])
    edited["auth"]["mode"] = "token"
    edited_yaml = yaml.safe_dump(edited)

    res = await get_dispatcher().dispatch(
        "r1",
        "config.apply",
        {"config_yaml": edited_yaml, "baseline_yaml": baseline_yaml},
        _control_ctx(cfg),
    )
    assert res.error is None, res.error

    saved = _read(cfg_path)
    # Deliberate edit (none in baseline -> token submitted) persisted; NOT
    # restored to the on-disk trusted-proxy original the way round-2 would.
    assert saved["auth"]["mode"] == "token"
    assert saved["auth"]["token"] == "existing-token"

    follow_up = await get_dispatcher().dispatch(
        "r2",
        "config.patch",
        {"patches": {"prompt_cache.mode": "on"}},
        _control_ctx(cfg),
    )
    assert follow_up.error is None, follow_up.error
    saved = _read(cfg_path)
    assert saved["auth"]["mode"] == "token"
    assert saved["auth"]["token"] == "existing-token"


@pytest.mark.asyncio
async def test_config_apply_baseline_diff_restores_unedited_runtime_echo(tmp_path):
    """[FIX 4] A field UNCHANGED vs the baseline is a runtime echo, so it is
    restored to its on-disk original — the transient mode=none the user
    never touched in the YAML must not freeze."""
    import yaml

    cfg_path = tmp_path / "config.toml"
    _seed_disk(cfg_path, {"auth": {"mode": "token", "token": "keep"}})

    set_runtime_overrides({"auth.mode": "token"})
    cfg = _running_config(
        cfg_path,
        auth=AuthConfig(mode="none", token="keep"),
    )
    baseline = cfg.model_dump(mode="json")
    baseline_yaml = yaml.safe_dump(baseline)
    # User edits an UNRELATED field, leaving auth untouched (still the echo).
    edited = dict(baseline)
    edited["debug"] = True
    edited_yaml = yaml.safe_dump(edited)

    res = await get_dispatcher().dispatch(
        "r1",
        "config.apply",
        {"config_yaml": edited_yaml, "baseline_yaml": baseline_yaml},
        _control_ctx(cfg),
    )
    assert res.error is None, res.error

    saved = _read(cfg_path)
    # Auth was NOT edited vs baseline, so the runtime echo is restored to disk.
    assert saved["auth"]["mode"] == "token"


@pytest.mark.asyncio
async def test_config_apply_baseline_diff_never_persists_readonly_host(tmp_path):
    """[FIX 4] Even if host somehow differs from the baseline, it is a read-only
    path and must never persist via config.apply."""
    import yaml

    cfg_path = tmp_path / "config.toml"
    _seed_disk(cfg_path, {"host": "127.0.0.1"})

    set_runtime_overrides({"host": "127.0.0.1", "port": 18791, "debug": False})
    cfg = _running_config(cfg_path, host="0.0.0.0")
    baseline = cfg.model_dump(mode="json")
    baseline["host"] = "127.0.0.1"  # baseline showed loopback
    baseline_yaml = yaml.safe_dump(baseline)
    edited = dict(baseline)
    edited["host"] = "0.0.0.0"  # user tries to change host in YAML
    edited_yaml = yaml.safe_dump(edited)

    res = await get_dispatcher().dispatch(
        "r1",
        "config.apply",
        {"config_yaml": edited_yaml, "baseline_yaml": baseline_yaml},
        _control_ctx(cfg),
    )
    assert res.error is None, res.error
    assert _read(cfg_path).get("host", "127.0.0.1") == "127.0.0.1"  # never persisted


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
        "r1", "config.apply", {"config": payload}, _control_ctx(cfg)
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
        _control_ctx(cfg),
    )
    assert res.error is None, res.error

    saved = _read(cfg_path)
    assert saved.get("host", "127.0.0.1") == "127.0.0.1"  # NOT frozen to 0.0.0.0
