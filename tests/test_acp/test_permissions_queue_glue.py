"""Approval queue / RPC glue for the ACP namespaces (payload, ttl, listing)."""

from __future__ import annotations

import sqlite3
import time

import pytest
from acp_perm_helpers import make_queue

from agentos.application.approval_queue import ApprovalQueue
from agentos.application.approval_rpc import (
    approval_display_title,
    approval_pending_rpc_payload,
)
from agentos.gateway import rpc_approvals
from agentos.gateway.rpc import RpcContext, get_dispatcher


def test_payload_round_trip_and_take_clears_it(tmp_path) -> None:
    queue = make_queue(tmp_path)
    aid = queue.request("acp_elicitation", {"sessionKey": "agent:main:acp:1"})
    queue.resolve(aid, True, payload={"content": {"x": 1}})
    entry = queue.get(aid)
    assert entry.payload == {"content": {"x": 1}}
    assert queue.status(aid)["has_payload"] is True
    assert "content" not in queue.status(aid)["params"]
    assert queue.take_payload(aid) == {"content": {"x": 1}}
    assert queue.take_payload(aid) is None
    assert queue.get(aid).payload is None
    with pytest.raises(KeyError):
        queue.take_payload("missing")
    queue.close()


def test_ttl_outlives_default_timeout_and_still_expires(tmp_path) -> None:
    queue = make_queue(tmp_path, default_timeout=300.0)
    long_row = queue.request("acp", {}, ttl=900.0)
    plain = queue.request("exec", {})
    backdate = time.time() - 400
    queue._conn.execute("UPDATE approval_queue SET created_at = ?", (backdate,))
    queue._conn.execute(
        "UPDATE approval_queue SET expires_at = ? WHERE approval_id = ?",
        (backdate + 900, long_row),
    )
    queue._conn.commit()
    pending = {p["id"] for p in queue.list_pending()}
    assert pending == {long_row}  # plain row aged out under default_timeout
    assert queue.get(plain).resolved is True
    queue._conn.execute(
        "UPDATE approval_queue SET expires_at = ? WHERE approval_id = ?",
        (time.time() - 1, long_row),
    )
    queue._conn.commit()
    assert queue.list_pending() == []
    entry = queue.get(long_row)
    assert entry.resolved and not entry.approved
    queue.close()


async def test_wait_honours_row_ttl(tmp_path) -> None:
    queue = make_queue(tmp_path, default_timeout=300.0)
    aid = queue.request("acp", {}, ttl=0.02)
    assert await queue.wait(aid, timeout=0.05) is False
    assert queue.get(aid).resolved is True
    queue.close()


def test_old_queue_file_is_migrated(tmp_path) -> None:
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE approval_queue (
            approval_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, params TEXT NOT NULL,
            created_at REAL NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
            approved INTEGER NOT NULL DEFAULT 0, consumed INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute(
        "INSERT INTO approval_queue VALUES ('old1', 'exec', '{}', ?, 0, 0, 0)", (time.time(),)
    )
    conn.commit()
    conn.close()
    queue = ApprovalQueue(db_path=str(path))
    assert [p["id"] for p in queue.list_pending()] == ["old1"]
    entry = queue.get("old1")
    assert entry.payload is None and entry.expires_at is None
    queue.resolve("old1", True, payload={"a": 1})
    assert queue.take_payload("old1") == {"a": 1}
    queue.close()


def test_exec_resolve_still_elevates_and_acp_does_not(tmp_path) -> None:
    queue = make_queue(tmp_path)
    exec_id = queue.request("exec", {"sessionKey": "agent:main:web:1"})
    queue.resolve(exec_id, True, elevated_mode="bypass")
    assert queue.get_elevated_mode("agent:main:web:1") == "bypass"
    assert queue.get(exec_id).params.get("rememberDecision") is None

    acp_id = queue.request("acp", {"sessionKey": "agent:main:acp:1"})
    queue.resolve(acp_id, True, elevated_mode="full")
    assert queue.get_elevated_mode("agent:main:acp:1") is None
    assert queue.get(acp_id).params["rememberDecision"] is True

    denied = queue.request("acp", {"sessionKey": "agent:main:acp:1"})
    queue.resolve(denied, False, allow_always=True)
    assert "rememberDecision" not in queue.get(denied).params

    elicit = queue.request("acp_elicitation", {"sessionKey": "agent:main:acp:1"})
    queue.resolve(elicit, True, allow_always=True, elevated_mode="bypass")
    assert "rememberDecision" not in queue.get(elicit).params
    assert queue.get_elevated_mode("agent:main:acp:1") is None
    queue.close()


def test_display_titles() -> None:
    assert approval_display_title("acp", {"displayTitle": " Codex wants to run: ls "}) == (
        "Codex wants to run: ls"
    )
    assert approval_display_title("exec", {"toolName": "exec_command", "command": "ls"}) == (
        "exec_command: ls"
    )
    assert approval_display_title("exec", {"toolName": "x", "argv": ["a", "b"]}) == "x: a b"
    assert approval_display_title("plugin", {"pluginId": "p1"}) == "Plugin p1 requests permissions"
    assert approval_display_title("acp", None) == "acp"


def test_pending_payload_lists_every_namespace(tmp_path) -> None:
    queue = make_queue(tmp_path)
    queue.request("exec", {"toolName": "exec_command", "command": "ls", "sessionKey": "s1"})
    queue.request("acp", {"displayTitle": "Codex wants to edit: a.py", "session_key": "s2"})
    queue.request("acp_elicitation", {"displayTitle": "Codex asks: ok?", "sessionKey": "s3"})
    payload = approval_pending_rpc_payload(queue)
    titles = {i["namespace"]: i["title"] for i in payload["pending"]}
    assert titles == {
        "exec": "exec_command: ls",
        "acp": "Codex wants to edit: a.py",
        "acp_elicitation": "Codex asks: ok?",
    }
    assert {i["sessionKey"] for i in payload["pending"]} == {"s1", "s2", "s3"}
    only = approval_pending_rpc_payload(queue, "acp")["pending"]
    assert [i["namespace"] for i in only] == ["acp"]
    queue.close()


async def test_rpc_list_and_resolve_with_payload(tmp_path, monkeypatch) -> None:
    queue = make_queue(tmp_path)
    monkeypatch.setattr(rpc_approvals, "get_approval_queue", lambda: queue)
    aid = queue.request("acp_elicitation", {"displayTitle": "Codex asks: branch?"})
    ctx = RpcContext(conn_id="test")
    listed = await get_dispatcher().dispatch("r1", "approval.list", {"namespace": None}, ctx)
    assert listed.error is None, listed.error
    assert [i["title"] for i in listed.payload["pending"]] == ["Codex asks: branch?"]

    bad = await get_dispatcher().dispatch(
        "r2", "exec.approval.resolve", {"id": aid, "approved": True, "payload": "nope"}, ctx
    )
    assert bad.error is not None
    assert queue.get(aid).resolved is False

    res = await get_dispatcher().dispatch(
        "r3",
        "exec.approval.resolve",
        {"id": aid, "approved": True, "payload": {"content": {"branch": "main"}}},
        ctx,
    )
    assert res.error is None, res.error
    assert res.payload["approved"] is True
    assert "payload" not in res.payload  # answers are not echoed back
    assert queue.take_payload(aid) == {"content": {"branch": "main"}}

    bad_ns = await get_dispatcher().dispatch("r4", "approval.list", {"namespace": 3}, ctx)
    assert bad_ns.error is not None
    queue.close()


def test_http_approvals_list_and_resolve_carry_title_and_payload(tmp_path, monkeypatch) -> None:
    from starlette.testclient import TestClient

    from agentos.gateway import app as app_mod
    from agentos.gateway.app import create_gateway_app
    from agentos.gateway.config import AuthConfig, GatewayConfig

    queue = make_queue(tmp_path)
    monkeypatch.setattr(app_mod, "get_approval_queue", lambda: queue)
    monkeypatch.setattr(rpc_approvals, "get_approval_queue", lambda: queue)
    aid = queue.request(
        "acp_elicitation",
        {"displayTitle": "Codex asks: branch?", "toolName": "Codex asks: branch?"},
    )
    app = create_gateway_app(config=GatewayConfig(auth=AuthConfig(mode="token", token="t-1")))
    headers = {"Authorization": "Bearer t-1"}
    with TestClient(app, base_url="http://localhost") as client:
        listed = client.get("/api/approvals", headers=headers)
        assert listed.status_code == 200, listed.text
        (item,) = listed.json()["pending"]
        assert item["title"] == "Codex asks: branch?"
        assert item["namespace"] == "acp_elicitation"
        resolved = client.post(
            "/api/approvals/resolve",
            headers=headers,
            json={
                "id": aid,
                "approved": True,
                "namespace": "acp_elicitation",
                "payload": {"content": {"branch": "main"}},
            },
        )
        assert resolved.status_code == 200, resolved.text
    assert queue.take_payload(aid) == {"content": {"branch": "main"}}
    queue.close()


def test_approval_list_display_answers_like_the_http_list(tmp_path, monkeypatch) -> None:
    """A console reaching a gateway only over its socket reads the same list."""
    from starlette.testclient import TestClient

    from agentos.gateway import app as app_mod
    from agentos.gateway.app import create_gateway_app
    from agentos.gateway.config import AuthConfig, GatewayConfig

    queue = make_queue(tmp_path)
    monkeypatch.setattr(app_mod, "get_approval_queue", lambda: queue)
    monkeypatch.setattr(rpc_approvals, "get_approval_queue", lambda: queue)
    queue.request("exec", {"toolName": "shell", "command": "rm -f x", "sessionKey": "s1"})
    queue.request("acp_elicitation", {"displayTitle": "Codex asks: branch?"})
    app = create_gateway_app(config=GatewayConfig(auth=AuthConfig(mode="token", token="t-1")))
    with TestClient(app, base_url="http://localhost") as client:
        http = client.get("/api/approvals", headers={"Authorization": "Bearer t-1"}).json()

    async def _rpc() -> dict:
        res = await get_dispatcher().dispatch(
            "r1", "approval.list", {"display": True}, RpcContext(conn_id="test")
        )
        assert res.error is None, res.error
        return res.payload

    import asyncio

    assert asyncio.run(_rpc()) == http
    queue.close()
