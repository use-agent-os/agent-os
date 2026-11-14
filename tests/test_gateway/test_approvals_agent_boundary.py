"""An agent never answers its own approvals, changes how they are answered, or elevates itself.

Reproduced as the audit did: through the real gateway routes, with a
gateway-minted agent token, and with no credentials while an exec window is
open. ``tools.invoke`` (already refused) is the control.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from agentos.application import approval_queue as approval_queue_mod
from agentos.application.approval_queue import ApprovalQueue
from agentos.gateway.access import ConnectionSurface
from agentos.gateway.agent_surface import AgentBinding, get_agent_surface
from agentos.gateway.app import create_gateway_app
from agentos.gateway.auth import AccessContext
from agentos.gateway.config import GatewayConfig
from agentos.gateway.rpc import RpcContext

_SESSION = "agent:main:main"
_HOST = {"host": "127.0.0.1:18791"}


@pytest.fixture
def queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ApprovalQueue:
    q = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    monkeypatch.setattr(approval_queue_mod, "_queue", q)
    get_agent_surface().reset()
    yield q
    get_agent_surface().reset()
    q.close()


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_gateway_app(GatewayConfig()), client=("127.0.0.1", 50000))


def _token() -> str:
    return get_agent_surface().mint_token(_SESSION, "main")


def _agent(token: str) -> dict[str, str]:
    return {"x-agentos-agent-token": token, **_HOST}


class TestHttpRoutes:
    def test_tools_invoke_is_the_control(self, queue: ApprovalQueue, client: TestClient) -> None:
        res = client.post("/api/v1/tools/invoke", json={"tool": "noop"}, headers=_agent(_token()))
        assert res.status_code == 403

    def test_token_bound_shell_cannot_approve_its_flow_cost_prompt(
        self, queue: ApprovalQueue, client: TestClient
    ) -> None:
        flow = queue.request("flow", {"sessionKey": _SESSION, "toolName": "flow_run"})
        res = client.post(
            "/api/approvals/resolve",
            json={"id": flow, "approved": True, "namespace": "flow"},
            headers=_agent(_token()),
        )
        assert res.status_code == 403, res.text
        assert queue.get(flow).resolved is False

    def test_window_bound_shell_cannot_approve_an_exec_row(
        self, queue: ApprovalQueue, client: TestClient
    ) -> None:
        row = queue.request("exec", {"sessionKey": _SESSION, "command": "rm -rf ~/x"})
        with get_agent_surface().exec_window(_SESSION):
            res = client.post(
                "/api/approvals/resolve", json={"id": row, "approved": True}, headers=_HOST
            )
        assert res.status_code == 403, res.text
        assert queue.get(row).resolved is False

    def test_agent_cannot_elevate_its_session_to_full(
        self, queue: ApprovalQueue, client: TestClient
    ) -> None:
        row = queue.request("exec", {"sessionKey": _SESSION, "command": "cat x"})
        res = client.post(
            "/api/elevated-mode",
            json={"sessionKey": _SESSION, "mode": "full"},
            headers=_agent(_token()),
        )
        assert res.status_code == 403
        assert queue.get_elevated_mode(_SESSION) is None
        assert queue.get(row).resolved is False

    def test_agent_cannot_switch_the_global_mode_to_auto_approve(
        self, queue: ApprovalQueue, client: TestClient
    ) -> None:
        before = queue.get_settings().mode
        res = client.post(
            "/api/approvals/settings", json={"mode": "auto-approve"}, headers=_agent(_token())
        )
        assert res.status_code == 403
        assert queue.get_settings().mode == before

    def test_the_users_console_still_answers(
        self, queue: ApprovalQueue, client: TestClient
    ) -> None:
        """Unbound loopback (the web console, no shell running) is still the user."""
        row = queue.request("exec", {"sessionKey": _SESSION, "command": "ls"})
        res = client.post(
            "/api/approvals/resolve", json={"id": row, "approved": True}, headers=_HOST
        )
        assert res.status_code == 200, res.text
        assert queue.get(row).approved is True
        res = client.post(
            "/api/elevated-mode", json={"sessionKey": _SESSION, "mode": "on"}, headers=_HOST
        )
        assert res.status_code == 200


def _ctx(via: str | None) -> RpcContext:
    access = AccessContext(
        surface=ConnectionSurface.CONTROL,
        admitted=True,
        credential_verified=False,
        agent=AgentBinding(via=via, session_key=_SESSION) if via else None,
    )
    return RpcContext(conn_id="c", access=access)


class TestRpc:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("via", ["token", "process", "window"])
    @pytest.mark.parametrize(
        ("method", "params"),
        [
            ("exec.approval.resolve", {"id": "{id}", "approved": True}),
            ("plugin.approval.resolve", {"id": "{id}", "approved": True}),
            ("exec.approvals.set", {"mode": "auto-approve"}),
            ("exec.approvals.node.set", {"nodeId": "n1", "mode": "auto-approve"}),
            ("exec.approval.forget", {}),
            ("channels.pairing.approve", {"channel": "telegram", "senderId": "1", "code": "x"}),
        ],
    )
    async def test_refused_for_a_recognized_agent(
        self, queue: ApprovalQueue, via: str, method: str, params: dict[str, Any]
    ) -> None:
        from agentos.gateway.rpc import get_dispatcher

        row = queue.request(
            "plugin" if method.startswith("plugin") else "exec", {"sessionKey": _SESSION}
        )
        params = {k: (row if v == "{id}" else v) for k, v in params.items()}
        res = await get_dispatcher().dispatch("c", method, params, _ctx(via))
        assert not res.ok and res.error is not None
        assert res.error.code == "approvals.operator_required", res.error
        assert queue.get(row).resolved is False
        assert queue.get_settings().mode != "auto-approve"

    def test_elevated_hint_from_an_agent_is_dropped(self) -> None:
        from agentos.gateway.rpc_sessions import _trusted_elevated_hint

        for via in ("token", "process", "window"):
            assert _trusted_elevated_hint(_ctx(via), {"elevated": "full"}) is None
        assert _trusted_elevated_hint(_ctx("unbound"), {"elevated": "full"}) == "full"
        assert _trusted_elevated_hint(_ctx(None), {"elevated": "on"}) == "on"


class TestTheDatabaseIsNotAnApprovalSurface:
    """The queue re-reads SQLite; a same-user write must not approve anything."""

    def _tamper(self, queue: ApprovalQueue, sql: str, *args: Any) -> None:
        conn = sqlite3.connect(queue._db_path)
        try:
            conn.execute(sql, args)
            conn.commit()
        finally:
            conn.close()

    @pytest.mark.asyncio
    async def test_a_direct_update_is_a_denial(self, queue: ApprovalQueue) -> None:
        row = queue.request("exec", {"sessionKey": _SESSION, "command": "curl x | sh"})
        self._tamper(
            queue,
            "UPDATE approval_queue SET resolved = 1, approved = 1 WHERE approval_id = ?",
            row,
        )
        entry = queue.get(row)
        assert entry.resolved is True and entry.approved is False
        assert await queue.wait(row, timeout=0.05) is False
        with pytest.raises(ValueError):
            queue.consume(row)

    def test_a_forged_row_with_elevation_is_a_denial(self, queue: ApprovalQueue) -> None:
        row = queue.request("exec", {"sessionKey": _SESSION, "command": "ls"})
        params = queue._get_row(row)["params"]
        self._tamper(
            queue,
            "INSERT INTO approval_queue (approval_id, namespace, params, created_at, resolved, "
            "approved, consumed) VALUES (?, 'exec', ?, 0, 1, 1, 0)",
            "forged-id",
            params.replace('"ls"', '"ls", "elevatedMode": "full"'),
        )
        assert queue.get("forged-id").approved is False

    def test_params_changed_after_the_users_approval_are_a_denial(
        self, queue: ApprovalQueue
    ) -> None:
        row = queue.request("exec", {"sessionKey": _SESSION, "command": "ls"})
        queue.resolve(row, True)
        assert queue.get(row).approved is True
        self._tamper(
            queue,
            "UPDATE approval_queue SET params = replace(params, 'ls', 'rm -rf ~') "
            "WHERE approval_id = ?",
            row,
        )
        assert queue.get(row).approved is False

    def test_another_queue_object_in_this_process_still_sees_the_decision(
        self, queue: ApprovalQueue, tmp_path: Path
    ) -> None:
        row = queue.request("exec", {"sessionKey": _SESSION, "command": "ls"})
        queue.resolve(row, True)
        other = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
        try:
            assert other.get(row).approved is True
        finally:
            other.close()

    def test_the_decision_record_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from collections import OrderedDict

        decisions: OrderedDict[tuple[str, str], str] = OrderedDict()
        monkeypatch.setattr(approval_queue_mod, "_LOCAL_DECISIONS", decisions)
        monkeypatch.setattr(approval_queue_mod, "_MAX_LOCAL_DECISIONS", 3)
        for index in range(5):
            approval_queue_mod._remember_decision(("db", f"a{index}"), "{}")
        assert list(decisions) == [("db", "a2"), ("db", "a3"), ("db", "a4")]
