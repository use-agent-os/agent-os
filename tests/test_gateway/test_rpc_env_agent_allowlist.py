"""WAL-18: an agent's connection may write only agent-settable catalog names.

``env.set`` applies a value live to the gateway's own ``os.environ`` -- the
process that holds the vault keys. ``ECC_BACKEND_CLASS`` (not on the small
write denylist) names the class eth-keys imports to compute every signature,
so an agent-bound ``env.set`` of it used to pick the vault's signing code.
Agent-bound connections are now held to an allowlist: only a credential a
skill or a built-in tool declares in the env catalog. The operator's own
connections keep the denylist alone, and the denylist still applies to all.
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

SKILL_KEY = "WEATHERLY_API_KEY"
SECRET = "wk-" + "q" * 32


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


def _skill(name: str, *env: tuple[str, bool | None]) -> Any:
    declared = [
        SimpleNamespace(name=n, description="", url="", secret=secret, required=True)
        for n, secret in env
    ]
    return SimpleNamespace(
        name=name, metadata=SimpleNamespace(requires=SimpleNamespace(env=declared))
    )


def _loader(*skills: Any) -> Any:
    return SimpleNamespace(load_all=lambda: list(skills))


# A skill the agent could have written itself: it declares the signer's
# backend selector and the gateway's own RPC fallback next to a real key.
LOADER = _loader(
    _skill(
        "weatherly",
        (SKILL_KEY, None),
        ("ECC_BACKEND_CLASS", True),
        ("RPC_BASE_URL", None),
        ("OPENAI_API_KEY", True),
    )
)


def _ctx(via: str | None) -> RpcContext:
    access = AccessContext(
        surface=ConnectionSurface.CONTROL,
        admitted=True,
        credential_verified=via is None,
        agent=AgentBinding(via=via, session_key="agent:main:webchat:t") if via else None,
    )
    return RpcContext(conn_id="c", access=access, skill_loader=LOADER)


async def _call(method: str, params: dict[str, Any], via: str | None) -> Any:
    return await get_dispatcher().dispatch("r", method, params, _ctx(via))


AGENT_VIAS = ("token", "process", "window")


class TestAgentEnvSet:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("via", AGENT_VIAS)
    @pytest.mark.parametrize(
        "name",
        [
            "ECC_BACKEND_CLASS",  # the audit's PoC: picks the signing code
            "ETH_HASH_BACKEND",
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
            "SSLKEYLOGFILE",
            "RPC_BASE_URL",  # the signer's RPC fallback
            "UNISWAP_API_KEY",
            "OPENAI_API_KEY",  # a provider key is the operator's
            "SOME_RANDOM_NAME",  # not in the catalog at all
        ],
    )
    async def test_a_non_agent_settable_name_is_refused(self, via: str, name: str) -> None:
        res = await _call("env.set", {"name": name, "value": "x.y.Z"}, via)
        assert not res.ok and res.error is not None
        assert res.error.code == "env.protected"
        assert name not in os.environ or os.environ[name] != "x.y.Z"
        assert env_store.read_env_file().get(name) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("via", AGENT_VIAS)
    async def test_a_skill_credential_still_works(self, via: str) -> None:
        res = await _call("env.set", {"name": SKILL_KEY, "value": SECRET}, via)
        assert res.ok, res.error
        assert os.environ[SKILL_KEY] == SECRET
        assert SECRET not in str(res.payload)

        res = await _call("env.unset", {"name": SKILL_KEY}, via)
        assert res.ok, res.error
        assert res.payload["removed"] is True

    @pytest.mark.asyncio
    async def test_a_builtin_tool_credential_is_agent_settable(self) -> None:
        res = await _call("env.set", {"name": "FIRECRAWL_API_KEY", "value": SECRET}, "token")
        assert res.ok, res.error

    @pytest.mark.asyncio
    async def test_an_operator_file_name_is_not_an_agents_to_unset(self) -> None:
        env_store.set_env_var("MY_OWN_TOKEN", SECRET)
        res = await _call("env.unset", {"name": "MY_OWN_TOKEN"}, "process")
        assert not res.ok and res.error is not None and res.error.code == "env.protected"
        assert env_store.read_env_file()["MY_OWN_TOKEN"] == SECRET

    @pytest.mark.asyncio
    async def test_import_is_held_to_the_same_allowlist(self) -> None:
        res = await _call(
            "env.import", {"name": "ECC_BACKEND_CLASS", "sourceId": "gh-cli"}, "token"
        )
        assert not res.ok and res.error is not None and res.error.code == "env.protected"


class TestOperatorUnchanged:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("via", [None, "unbound"])
    async def test_operator_and_console_keep_todays_behaviour(self, via: str | None) -> None:
        for name in ("SOME_RANDOM_NAME", "RPC_BASE_URL", "OPENAI_API_KEY", SKILL_KEY):
            res = await _call("env.set", {"name": name, "value": "v1"}, via)
            assert res.ok, (name, res.error)
        res = await _call("env.reveal", {"name": "OPENAI_API_KEY"}, via)
        assert res.ok and res.payload["value"] == "v1"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("via", [None, "unbound", "token"])
    async def test_the_denylist_still_applies_to_everyone(self, via: str | None) -> None:
        res = await _call("env.set", {"name": "PYTHONPATH", "value": "/tmp"}, via)
        assert not res.ok


class TestAgentReveal:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("via", AGENT_VIAS)
    @pytest.mark.parametrize("name", [SKILL_KEY, "OPENAI_API_KEY", "MY_OWN_TOKEN", "HOME_X"])
    async def test_a_secret_or_unknown_name_is_not_revealed(self, via: str, name: str) -> None:
        env_store.set_env_var(name, SECRET)
        res = await _call("env.reveal", {"name": name}, via)
        assert not res.ok and res.error is not None
        assert res.error.code == "env.protected"
        assert SECRET not in str(res.error.message)

    @pytest.mark.asyncio
    async def test_a_refused_reveal_does_not_use_the_operators_budget(self) -> None:
        env_store.set_env_var("OPENAI_API_KEY", SECRET)
        for _ in range(10):
            await _call("env.reveal", {"name": "OPENAI_API_KEY"}, "token")
        res = await _call("env.reveal", {"name": "OPENAI_API_KEY"}, None)
        assert res.ok and res.payload["value"] == SECRET

    @pytest.mark.asyncio
    async def test_a_non_secret_catalog_entry_may_be_read(self) -> None:
        loader = _loader(_skill("units", ("WEATHERLY_UNITS", False)))
        env_store.set_env_var("WEATHERLY_UNITS", "metric")
        access = AccessContext(
            surface=ConnectionSurface.CONTROL,
            admitted=True,
            credential_verified=False,
            agent=AgentBinding(via="token"),
        )
        ctx = RpcContext(conn_id="c", access=access, skill_loader=loader)
        res = await get_dispatcher().dispatch("r", "env.reveal", {"name": "WEATHERLY_UNITS"}, ctx)
        assert res.ok and res.payload["value"] == "metric"


class TestCatalogFlag:
    def test_only_skill_and_tool_credentials_are_agent_settable(self) -> None:
        catalog = env_catalog.build_catalog(LOADER, present_names={"MY_OWN_TOKEN"})
        assert catalog[SKILL_KEY].agent_settable is True
        assert catalog["FIRECRAWL_API_KEY"].agent_settable is True
        for name in ("ECC_BACKEND_CLASS", "RPC_BASE_URL", "OPENAI_API_KEY", "MY_OWN_TOKEN"):
            assert catalog[name].agent_settable is False, name

    @pytest.mark.parametrize(
        "name",
        [
            "ECC_BACKEND_CLASS",
            "PYTHON_KEYRING_BACKEND",
            "PYTHONWARNINGS_TOKEN",
            "AGENTOS_SOMETHING_TOKEN",
            "RPC_ROBINHOOD_URL",
            "UNISWAP_API_KEY",
            "LD_PRELOAD",
        ],
    )
    def test_agent_may_own_refuses_runtime_names(self, name: str) -> None:
        assert env_policy.agent_may_own(name) is False

    def test_the_operator_denylist_is_unchanged(self) -> None:
        # Defence in depth stays small: these are allowlist-only refusals.
        for name in ("ECC_BACKEND_CLASS", "RPC_BASE_URL", "SSL_CERT_FILE"):
            assert env_policy.is_writable(name) is True


class TestProjectEnvFile:
    """The working directory's ``.env`` is as agent-writable as the directory."""

    def test_runtime_steering_names_are_not_loaded_from_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentos.env import load_env

        work = tmp_path / "project"
        work.mkdir()
        (work / ".env").write_text(
            "ECC_BACKEND_CLASS=evil.Backend\n"
            "SSL_CERT_FILE=/tmp/ca.pem\n"
            "PYTHONPATH=/tmp/plant\n"
            "AGENTOS_GATEWAY_TRADING__DAILY_CAP_USD=1e9\n"
            "PROJECT_ONLY_API_KEY=ok\n",
            encoding="utf-8",
        )
        for name in ("ECC_BACKEND_CLASS", "SSL_CERT_FILE", "PYTHONPATH", "PROJECT_ONLY_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv("AGENTOS_GATEWAY_TRADING__DAILY_CAP_USD", raising=False)
        load_env(work)
        assert os.environ.get("PROJECT_ONLY_API_KEY") == "ok"
        for name in (
            "ECC_BACKEND_CLASS",
            "SSL_CERT_FILE",
            "PYTHONPATH",
            "AGENTOS_GATEWAY_TRADING__DAILY_CAP_USD",
        ):
            assert name not in os.environ, name

    def test_the_operators_own_env_file_is_read_as_before(self) -> None:
        from agentos.env import load_env
        from agentos.paths import default_agentos_home

        home_env = default_agentos_home() / ".env"
        home_env.parent.mkdir(parents=True, exist_ok=True)
        home_env.write_text("SSL_CERT_FILE=/etc/corp-ca.pem\n", encoding="utf-8")
        os.environ.pop("SSL_CERT_FILE", None)
        load_env(Path.cwd())
        assert os.environ.get("SSL_CERT_FILE") == "/etc/corp-ca.pem"
