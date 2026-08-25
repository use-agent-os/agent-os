"""persist_config distinguishes transient CLI runtime overrides from
explicit user changes by *provenance*, not by field name.

At boot, ``run_gateway`` records the ON-DISK original values of the fields it
overrides (for example ``host``/``port``/``debug`` or a transient ``auth.mode``)
in a process-global override map via ``set_runtime_overrides``. Every live
writer goes through ``persist_config``,
which restores each recorded field to its original on-disk value (or drops it
when the original was absent, so the load-time default applies) — UNLESS that
exact dotted path is in ``explicit_paths``, meaning the user explicitly changed
it this write and it must persist. This closes the whole class of "a one-off
--listen 0.0.0.0 --debug got frozen into config.toml" leaks (PR #25 review)
WITHOUT silently discarding a genuine UI edit of the same field.

The writer is also atomic and 0600 (delegated to the onboarding writer), so a
freshly generated bearer token is never world-readable.
"""

from __future__ import annotations

import os
import stat
import sys
import tomllib

import pytest
import tomli_w

from agentos.gateway.config import AuthConfig, GatewayConfig
from agentos.gateway.config_persist import (
    get_runtime_overrides,
    persist_config,
    set_runtime_overrides,
)


@pytest.fixture(autouse=True)
def _reset_overrides():
    """The override map is process-global; isolate every test."""
    set_runtime_overrides(None)
    yield
    set_runtime_overrides(None)


def _read(path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def test_runtime_overrides_are_restored_to_their_original_values(tmp_path):
    cfg_path = tmp_path / "config.toml"
    with open(cfg_path, "wb") as f:
        tomli_w.dump({"host": "127.0.0.1", "port": 18791, "debug": False}, f)

    # Boot records the on-disk originals of the fields the CLI overrode.
    set_runtime_overrides({"host": "127.0.0.1", "port": 18791, "debug": False})

    runtime = GatewayConfig(
        host="0.0.0.0",
        port=19999,
        debug=True,
        auth=AuthConfig(mode="token", token="keep-me"),
        config_path=str(cfg_path),
    )
    persist_config(runtime)

    saved = _read(cfg_path)
    assert saved["auth"]["mode"] == "token"
    assert saved["auth"]["token"] == "keep-me"
    assert saved["host"] == "127.0.0.1"
    assert saved["port"] == 18791
    assert saved["debug"] is False


def test_explicit_change_persists_even_for_an_overridden_field(tmp_path):
    """A UI 'Save' of debug=true names ``debug`` in ``explicit_paths``, so it
    must actually persist — not be silently restored to the boot original."""
    cfg_path = tmp_path / "config.toml"
    with open(cfg_path, "wb") as f:
        tomli_w.dump({"debug": False}, f)

    # CLI ran with --debug this session (boot recorded the on-disk False).
    set_runtime_overrides({"debug": False})

    runtime = GatewayConfig(debug=True, config_path=str(cfg_path))
    persist_config(runtime, explicit_paths={"debug"})  # user explicitly set it

    assert _read(cfg_path)["debug"] is True


def test_explicit_change_without_overrides_persists_verbatim(tmp_path):
    cfg_path = tmp_path / "config.toml"
    with open(cfg_path, "wb") as f:
        tomli_w.dump({"debug": False}, f)

    runtime = GatewayConfig(debug=True, config_path=str(cfg_path))
    persist_config(runtime)  # no overrides recorded -> explicit change

    assert _read(cfg_path)["debug"] is True


def test_local_auth_mode_override_is_restorable(tmp_path):
    cfg_path = tmp_path / "config.toml"
    with open(cfg_path, "wb") as f:
        tomli_w.dump({"auth": {"mode": "trusted-proxy"}}, f)

    # A local run forced mode=none; boot recorded the on-disk auth value so a
    # later writer cannot freeze the transient posture.
    set_runtime_overrides({"auth.mode": "trusted-proxy"})
    runtime = GatewayConfig(
        auth=AuthConfig(mode="none"),
        config_path=str(cfg_path),
    )
    persist_config(runtime)

    saved = _read(cfg_path)
    assert saved["auth"]["mode"] == "trusted-proxy"


def test_missing_original_drops_the_field_so_defaults_apply(tmp_path):
    """First run: no on-disk value existed for the overridden field, so it is
    dropped (load-time default applies), never the override."""
    cfg_path = tmp_path / "config.toml"  # does not exist

    set_runtime_overrides({"host": None, "debug": None})
    runtime = GatewayConfig(
        host="0.0.0.0",
        debug=True,
        auth=AuthConfig(mode="token", token="tok"),
        config_path=str(cfg_path),
    )
    persist_config(runtime)

    saved = _read(cfg_path)
    assert saved["auth"]["token"] == "tok"
    assert saved.get("host", "127.0.0.1") == "127.0.0.1"
    assert saved.get("debug", False) is False


def test_set_and_get_runtime_overrides_round_trip():
    assert get_runtime_overrides() == {}
    set_runtime_overrides({"host": "127.0.0.1", "debug": None})
    assert get_runtime_overrides() == {"host": "127.0.0.1", "debug": None}
    # Returned map is a copy — mutating it must not corrupt module state.
    got = get_runtime_overrides()
    got["host"] = "0.0.0.0"
    assert get_runtime_overrides()["host"] == "127.0.0.1"
    set_runtime_overrides(None)
    assert get_runtime_overrides() == {}


def test_read_raw_bind_overrides_reads_literal_toml_values(tmp_path):
    from agentos.gateway.config_persist import read_raw_bind_overrides

    cfg_path = tmp_path / "config.toml"
    with open(cfg_path, "wb") as f:
        tomli_w.dump({"host": "0.0.0.0", "port": 19999, "debug": True}, f)

    assert read_raw_bind_overrides(str(cfg_path)) == {
        "host": "0.0.0.0",
        "port": 19999,
        "debug": True,
    }


def test_read_raw_bind_overrides_absent_keys_map_to_none(tmp_path, monkeypatch):
    """A key absent from the TOML is None (dropped on persist) even when an env
    var would make the EFFECTIVE loaded value non-default — the raw file wins."""
    from agentos.gateway.config_persist import read_raw_bind_overrides

    monkeypatch.setenv("AGENTOS_GATEWAY_HOST", "0.0.0.0")
    cfg_path = tmp_path / "config.toml"
    with open(cfg_path, "wb") as f:
        tomli_w.dump({"debug": False}, f)  # NO host / port

    got = read_raw_bind_overrides(str(cfg_path))
    assert got["host"] is None  # absent in TOML -> None, NOT the env 0.0.0.0
    assert got["port"] is None
    assert got["debug"] is False


def test_read_raw_bind_overrides_missing_file_is_all_none(tmp_path):
    from agentos.gateway.config_persist import read_raw_bind_overrides

    assert read_raw_bind_overrides(str(tmp_path / "nope.toml")) == {
        "host": None,
        "port": None,
        "debug": None,
    }
    assert read_raw_bind_overrides(None) == {"host": None, "port": None, "debug": None}


def test_env_supplied_host_is_not_frozen_by_a_later_persist(tmp_path, monkeypatch):
    """End-to-end: AGENTOS_GATEWAY_HOST=0.0.0.0 makes the effective bind public,
    but because the raw TOML has no host, the override original is None, so a
    later persist DROPS host (default/env re-applies) instead of freezing
    0.0.0.0 into the file."""
    from agentos.gateway.config_persist import read_raw_bind_overrides

    monkeypatch.setenv("AGENTOS_GATEWAY_HOST", "0.0.0.0")
    cfg_path = tmp_path / "config.toml"
    with open(cfg_path, "wb") as f:
        tomli_w.dump({"debug": False}, f)

    # run_gateway records the RAW originals (host absent -> None).
    set_runtime_overrides(read_raw_bind_overrides(str(cfg_path)))
    # The running config carries the effective public bind (from env).
    runtime = GatewayConfig(host="0.0.0.0", config_path=str(cfg_path))
    persist_config(runtime)  # unrelated save, host not explicit

    saved = _read(cfg_path)
    assert saved.get("host", "127.0.0.1") == "127.0.0.1"  # dropped, NOT frozen


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode")
def test_written_config_is_owner_only_0600(tmp_path):
    cfg_path = tmp_path / "config.toml"
    runtime = GatewayConfig(
        auth=AuthConfig(mode="token", token="secret"), config_path=str(cfg_path)
    )
    persist_config(runtime)

    mode = stat.S_IMODE(os.stat(cfg_path).st_mode)
    assert mode == 0o600, f"config with a bearer token must be 0600, got {oct(mode)}"
