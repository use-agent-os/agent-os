"""persist_config distinguishes transient CLI/break-glass overrides from
explicit user changes by *provenance*, not by field name.

At boot, ``run_gateway`` records the ON-DISK original values of the fields it
overrides (``host``/``port``/``debug``; break-glass ``auth.mode`` /
``allow_unauthenticated_public``) in a process-global override map via
``set_runtime_overrides``. Every live writer goes through ``persist_config``,
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


def test_break_glass_mode_and_opt_in_are_restorable_overrides(tmp_path):
    cfg_path = tmp_path / "config.toml"
    with open(cfg_path, "wb") as f:
        tomli_w.dump({"auth": {"mode": "password"}}, f)

    # break-glass forced mode=none + opt-in for this run only; boot recorded the
    # on-disk auth values so a later writer cannot freeze the break-glass posture.
    set_runtime_overrides(
        {
            "auth.mode": "password",
            "auth.allow_unauthenticated_public": False,
        }
    )
    runtime = GatewayConfig(
        auth=AuthConfig(mode="none", allow_unauthenticated_public=True),
        config_path=str(cfg_path),
    )
    persist_config(runtime)

    saved = _read(cfg_path)
    assert saved["auth"]["mode"] == "password"  # break-glass mode not frozen
    assert saved["auth"].get("allow_unauthenticated_public") in (False, None)


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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode")
def test_written_config_is_owner_only_0600(tmp_path):
    cfg_path = tmp_path / "config.toml"
    runtime = GatewayConfig(
        auth=AuthConfig(mode="token", token="secret"), config_path=str(cfg_path)
    )
    persist_config(runtime)

    mode = stat.S_IMODE(os.stat(cfg_path).st_mode)
    assert mode == 0o600, f"config with a bearer token must be 0600, got {oct(mode)}"
