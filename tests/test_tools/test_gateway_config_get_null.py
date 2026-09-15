"""``config_get`` must tell a null-valued key apart from a missing one (#1892).

The lookup walked the config with ``dict.get`` and treated a ``None`` result
as absence::

    val = cfg_dict
    for p in parts:
        val = val.get(p)
    if val is None:
        raise ToolError(f"Config key not found: {key}")

so a key that is *configured as null* — ``auth.token`` on any install that has
not set one — was reported to the agent as not existing at all.

There is a second half to it that a dict-only fix does not reach:
``GatewayConfig.to_toml_dict()`` is ``model_dump(exclude_none=True)``, so a
null-valued key is not merely ``None`` in that view, it is *absent*. Fifty
declared keys of a default ``GatewayConfig`` are in that state, which is why
these tests drive the real config object rather than a hand-written dict.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agentos.gateway.config import GatewayConfig
from agentos.tools.builtin.control import gateway, set_gateway_config
from agentos.tools.types import ToolError


@pytest.fixture
def real_config() -> Any:
    """A stock GatewayConfig, installed as the tool's config source."""
    previous: Any = None
    from agentos.tools.builtin import control

    previous = control._gateway_config
    config = GatewayConfig()
    set_gateway_config(config)
    yield config
    set_gateway_config(previous)


async def _get(key: str) -> dict[str, Any]:
    return json.loads(await gateway(action="config_get", key=key))


# ── A declared key whose value is null ─────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [
        "workspace_strict",
        "auth.token",
        "auth.trusted_proxy",
        "attachments.media_root",
        "tools.profile",
        "task_runtime.turn_hard_deadline_s",
        "memory.embedding.model",
    ],
)
async def test_a_null_valued_key_reads_back_as_null(real_config: Any, key: str) -> None:
    """Fails without the fix: ToolError('Config key not found')."""
    assert await _get(key) == {"action": "config_get", "key": key, "value": None}


@pytest.mark.asyncio
async def test_a_key_the_agent_can_set_is_a_key_the_agent_can_read(
    real_config: Any,
) -> None:
    """The asymmetry that makes this user-visible: ``config_set`` accepts
    ``auth.token`` happily, while ``config_get`` swore it did not exist."""
    before = await _get("auth.token")
    assert before["value"] is None

    real_config.auth.token = "s3cr3t"
    after = await _get("auth.token")

    assert after["value"] == "s3cr3t"


# ── Absence is still absence ───────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    ["nope", "llm.nope", "nope.nope.nope", "auth.token.deeper"],
)
async def test_a_missing_key_still_raises(real_config: Any, key: str) -> None:
    """Guard: passes either way by design. Returning ``null`` for anything
    unknown would be a worse answer than the error it replaces."""
    with pytest.raises(ToolError, match="Config key not found"):
        await _get(key)


@pytest.mark.asyncio
async def test_walking_through_a_scalar_is_not_a_hit(real_config: Any) -> None:
    """``llm.provider`` is a string; it has no children."""
    with pytest.raises(ToolError, match="Config key not found"):
        await _get("llm.provider.nested")


# ── Values that were already readable must stay readable ──────────────────


@pytest.mark.asyncio
async def test_ordinary_values_are_unchanged(real_config: Any) -> None:
    """Guard: passes either way by design."""
    assert (await _get("llm.provider"))["value"] == "openrouter"
    assert (await _get("llm.base_url"))["value"].startswith("https://")


@pytest.mark.asyncio
async def test_falsy_but_present_values_are_unchanged(real_config: Any) -> None:
    """``""`` and ``0`` are configured values, not absences.

    Guard: passes either way by design — the old check only special-cased
    ``None``, and distinguishing presence from value must not start swallowing
    these instead.
    """
    assert (await _get("llm.api_key"))["value"] == ""
    assert (await _get("llm.max_tokens"))["value"] == 0


@pytest.mark.asyncio
async def test_a_whole_section_still_reads_back(real_config: Any) -> None:
    payload = await _get("llm")

    assert isinstance(payload["value"], dict)
    assert payload["value"]["provider"] == "openrouter"


# ── The TOML view withholds some values on purpose ────────────────────────


class _WithholdingConfig:
    """A config whose TOML view hides a key that has a real value.

    ``to_toml_dict`` prunes env-sourced secrets deliberately. Reading the
    model as a fallback must not hand those back: only a key that is *None*
    in the model may be answered with ``null``.
    """

    def to_toml_dict(self) -> dict[str, Any]:
        return {"llm": {"provider": "openrouter"}}

    def model_dump(self) -> dict[str, Any]:
        return {"llm": {"provider": "openrouter", "api_key": "sk-secret", "thinking": None}}


@pytest.mark.asyncio
async def test_a_withheld_non_null_value_is_not_revealed() -> None:
    from agentos.tools.builtin import control

    previous = control._gateway_config
    set_gateway_config(_WithholdingConfig())
    try:
        with pytest.raises(ToolError, match="Config key not found"):
            await _get("llm.api_key")
        assert (await _get("llm.thinking"))["value"] is None
    finally:
        set_gateway_config(previous)


@pytest.mark.asyncio
async def test_a_config_without_model_dump_still_works() -> None:
    """Guard: the fallback is optional — a config object that only exposes
    ``to_toml_dict`` behaves exactly as before."""
    from agentos.tools.builtin import control

    class _TomlOnlyConfig:
        def to_toml_dict(self) -> dict[str, Any]:
            return {"gateway": {"host": "127.0.0.1", "auth_token": None}}

    previous = control._gateway_config
    set_gateway_config(_TomlOnlyConfig())
    try:
        assert (await _get("gateway.host"))["value"] == "127.0.0.1"
        assert (await _get("gateway.auth_token"))["value"] is None
        with pytest.raises(ToolError, match="Config key not found"):
            await _get("gateway.missing")
    finally:
        set_gateway_config(previous)
