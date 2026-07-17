"""persist_config never writes runtime-only overrides to disk.

host / port / debug / auth.allow_unauthenticated_public are set for a single
run (CLI flags, break-glass) and must NOT be frozen into config.toml by any
config writer — the CLI token prompt or any config RPC. Changing them
permanently is done by editing config.toml directly. This is the single rule
that prevents the whole class of "a one-off --listen 0.0.0.0 --debug got
persisted" leaks (PR #25 review, P1 #1/#3/#4).
"""

from __future__ import annotations

import tomllib

import tomli_w

from agentos.gateway.config import AuthConfig, GatewayConfig
from agentos.gateway.config_persist import persist_config


def _read(path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def test_runtime_overrides_are_not_written_over_on_disk_values(tmp_path):
    cfg_path = tmp_path / "config.toml"
    # On disk: a loopback, non-debug deployment.
    with open(cfg_path, "wb") as f:
        tomli_w.dump({"host": "127.0.0.1", "port": 18791, "debug": False}, f)

    # In memory: one-off CLI overrides plus a real change we DO want to keep.
    runtime = GatewayConfig(
        host="0.0.0.0",
        port=19999,
        debug=True,
        auth=AuthConfig(mode="token", token="keep-me", allow_unauthenticated_public=True),
        config_path=str(cfg_path),
    )

    persist_config(runtime)

    saved = _read(cfg_path)
    # The real change is persisted...
    assert saved["auth"]["mode"] == "token"
    assert saved["auth"]["token"] == "keep-me"
    # ...but every runtime-only field keeps its on-disk value.
    assert saved["host"] == "127.0.0.1"
    assert saved["port"] == 18791
    assert saved["debug"] is False
    assert saved["auth"].get("allow_unauthenticated_public") in (False, None)


def test_first_run_without_a_file_uses_safe_defaults_not_overrides(tmp_path):
    cfg_path = tmp_path / "config.toml"  # does NOT exist yet
    runtime = GatewayConfig(
        host="0.0.0.0",
        port=19999,
        debug=True,
        auth=AuthConfig(mode="token", token="tok", allow_unauthenticated_public=True),
        config_path=str(cfg_path),
    )

    persist_config(runtime)

    saved = _read(cfg_path)
    assert saved["auth"]["token"] == "tok"
    # No file existed, so runtime-only fields fall back to safe defaults, never
    # the wildcard/debug/opt-in override.
    assert saved.get("host", "127.0.0.1") == "127.0.0.1"
    assert saved.get("debug", False) is False
    assert saved["auth"].get("allow_unauthenticated_public") in (False, None)


def test_editing_the_file_directly_is_still_honored(tmp_path):
    """The escape hatch: a host deliberately written into config.toml stays."""
    cfg_path = tmp_path / "config.toml"
    with open(cfg_path, "wb") as f:
        tomli_w.dump({"host": "0.0.0.0", "port": 18791}, f)

    # A later write of an unrelated field must not clobber the on-disk host.
    runtime = GatewayConfig.load(cfg_path)
    runtime = runtime.model_copy(update={"debug": True})  # runtime-only override
    persist_config(runtime)

    saved = _read(cfg_path)
    assert saved["host"] == "0.0.0.0"  # the deliberately-edited value survives
    assert saved.get("debug", False) is False  # the runtime override is not frozen
