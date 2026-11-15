"""One env policy for wallet secrets, whichever fix introduced the name.

WAL-18 holds an agent's ``env.*`` writes to an allowlist and refuses its
``env.reveal`` of any secret; WAL-10/WAL-04 keep the skill hot-wallet keys
away from everything the agent drives. Where they meet: a skill manifest is a
file an agent can write, so it must not be able to declare a hot-wallet key
agent-settable or non-secret and so set or read it through ``env.*``. The
vault's sealing key (WAL-01) stays writable by no surface at all.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos import env_catalog, env_policy, env_store
from agentos.gateway.access import ConnectionSurface
from agentos.gateway.agent_surface import AgentBinding
from agentos.gateway.auth import AccessContext
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.gateway.rpc_env import _reset_reveal_budget

KEY = "0x" + "5a" * 32


@pytest.fixture(autouse=True)
def env_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    state = tmp_path / "state"
    state.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(state))
    monkeypatch.chdir(work)
    snapshot = dict(os.environ)
    _reset_reveal_budget()
    yield state
    os.environ.clear()
    os.environ.update(snapshot)
    _reset_reveal_budget()


def _loader(secret: bool | None) -> Any:
    """A skill (possibly the agent's own) declaring every hot-wallet key."""
    declared = [
        SimpleNamespace(name=n, description="", url="", secret=secret, required=True)
        for n in sorted(env_policy.SKILL_SIGNING_KEY_ENV)
    ]
    skill = SimpleNamespace(
        name="lp-helper", metadata=SimpleNamespace(requires=SimpleNamespace(env=declared))
    )
    return SimpleNamespace(load_all=lambda: [skill])


def _ctx(via: str | None, loader: Any) -> RpcContext:
    access = AccessContext(
        surface=ConnectionSurface.CONTROL,
        admitted=True,
        credential_verified=via is None,
        agent=AgentBinding(via=via, session_key="agent:main:webchat:t") if via else None,
    )
    return RpcContext(conn_id="c", access=access, skill_loader=loader)


async def _call(method: str, params: dict[str, Any], via: str | None, loader: Any) -> Any:
    return await get_dispatcher().dispatch("r", method, params, _ctx(via, loader))


SIGNING_KEYS = sorted(env_policy.SKILL_SIGNING_KEY_ENV)


@pytest.mark.parametrize("name", SIGNING_KEYS)
def test_no_manifest_makes_a_hot_wallet_key_agent_settable(name: str) -> None:
    assert env_policy.agent_may_own(name) is False
    for secret in (None, True, False):
        catalog = env_catalog.build_catalog(_loader(secret), present_names=set())
        assert catalog[name].agent_settable is False


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["token", "process", "window"])
@pytest.mark.parametrize("name", SIGNING_KEYS)
async def test_an_agent_can_neither_set_nor_reveal_a_hot_wallet_key(via: str, name: str) -> None:
    env_store.set_env_var(name, KEY)
    for secret in (None, False):  # the manifest may even claim it is no secret
        loader = _loader(secret)
        res = await _call("env.set", {"name": name, "value": "0x" + "ee" * 32}, via, loader)
        assert not res.ok and res.error is not None and res.error.code == "env.protected"
        res = await _call("env.reveal", {"name": name}, via, loader)
        assert not res.ok and res.error is not None and res.error.code == "env.protected"
        assert KEY not in str(res.error.message)
    assert os.environ[name] == KEY


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SIGNING_KEYS)
async def test_the_operator_still_manages_a_hot_wallet_key(name: str) -> None:
    loader = _loader(True)
    res = await _call("env.set", {"name": name, "value": KEY}, None, loader)
    assert res.ok, res.error
    res = await _call("env.reveal", {"name": name}, None, loader)
    assert res.ok and res.payload["value"] == KEY


@pytest.mark.parametrize("name", ["AGENTOS_VAULT_KEK", "AGENTOS_VAULT_KEK_DIR"])
def test_the_vault_sealing_key_is_written_by_no_surface(name: str) -> None:
    assert env_policy.is_writable(name) is False
    assert env_policy.agent_may_own(name) is False


def test_operator_only_env_spellings_come_from_config_roots() -> None:
    """``agentos.config_roots`` is the one list: every root's env spelling is refused."""
    from agentos.config_roots import OPERATOR_ONLY_CONFIG_ROOTS, OPERATOR_ONLY_ENV_PREFIXES

    for root in OPERATOR_ONLY_CONFIG_ROOTS:
        name = "AGENTOS_GATEWAY_" + root.upper().replace(".", "__")
        assert env_policy.is_writable(name) is False, name
        assert env_policy.is_writable(name + "__X") is False, name
    for prefix in OPERATOR_ONLY_ENV_PREFIXES:
        assert env_policy.is_writable(prefix.rstrip("_") + "_X") is False, prefix
