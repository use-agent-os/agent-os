"""Unit tests for parameter validation in skills RPC handlers."""

from __future__ import annotations

import pytest

import agentos.gateway.rpc_skills  # noqa: F401
from agentos.gateway.access import ConnectionSurface
from agentos.gateway.auth import AccessContext
from agentos.gateway.rpc import RpcContext, get_dispatcher


def _ctx() -> RpcContext:
    return RpcContext(
        conn_id="conn-test",
        access=AccessContext(
            surface=ConnectionSurface.CONTROL,
            admitted=True,
            credential_verified=True,
        ),
    )


class TestSkillsRpcParamValidation:
    @pytest.mark.parametrize(
        ("method", "invalid_params"),
        [
            ("skills.get", None),
            ("skills.get", []),
            ("skills.get", "string"),
            ("skills.get", {}),
            ("skills.get", {"name": 123}),
            ("skills.get", {"name": ""}),
            ("skills.get", {"name": "   "}),
            ("skills.get", {"name": None}),
            ("skills.search", None),
            ("skills.search", []),
            ("skills.search", {}),
            ("skills.search", {"query": 123}),
            ("skills.search", {"query": None}),
            ("skills.install", None),
            ("skills.install", []),
            ("skills.install", {}),
            ("skills.install", {"identifier": 123}),
            ("skills.install", {"identifier": ""}),
            ("skills.install", {"identifier": "   "}),
            ("skills.install", {"identifier": None}),
            ("skills.update", "not-a-dict"),
            ("skills.update", [123]),
            ("skills.update", {"name": 123}),
            ("skills.update", {"name": ""}),
            ("skills.update", {"name": "   "}),
            ("skills.uninstall", None),
            ("skills.uninstall", []),
            ("skills.uninstall", {}),
            ("skills.uninstall", {"name": 123}),
            ("skills.uninstall", {"name": ""}),
            ("skills.uninstall", {"name": "   "}),
            ("skills.uninstall", {"name": None}),
            ("skills.deps.install", None),
            ("skills.deps.install", []),
            ("skills.deps.install", {}),
            ("skills.deps.install", {"name": "foo"}),
            ("skills.deps.install", {"install_id": "bar"}),
            ("skills.deps.install", {"name": 123, "install_id": "bar"}),
            ("skills.deps.install", {"name": "", "install_id": "bar"}),
            ("skills.deps.install", {"name": "foo", "install_id": 123}),
            ("skills.deps.install", {"name": "foo", "install_id": ""}),
            ("skills.deps.install", {"name": "foo", "install_id": "   "}),
        ],
    )
    async def test_invalid_params_rejected(self, method: str, invalid_params: object) -> None:
        res = await get_dispatcher().dispatch("req-1", method, invalid_params, _ctx())
        assert res.error is not None, f"Expected error for {method} with {invalid_params!r}"
        assert res.error.code == "INVALID_REQUEST", res.error
