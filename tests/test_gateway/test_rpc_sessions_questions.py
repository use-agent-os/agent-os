"""``sessions.questions``: unanswered ``ask_user`` questions, listed at load."""

from __future__ import annotations

import json
from typing import Any

from agentos.ask_user import build_presented_payload
from agentos.gateway.config import GatewayConfig
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage

QUESTION = [{"question": "Which chain?", "options": [{"label": "Base"}, {"label": "Robinhood"}]}]


def _asked(tool_use_id: str) -> list[dict[str, Any]]:
    return [
        {"type": "text", "text": "One thing first."},
        {"type": "tool_use", "tool_use_id": tool_use_id, "name": "ask_user", "input": {}},
        {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "name": "ask_user",
            "result": json.dumps(build_presented_payload(QUESTION)),
            "is_error": False,
        },
    ]


async def _questions(mgr: SessionManager, **params: Any) -> dict[str, Any]:
    ctx = RpcContext(conn_id="c", config=GatewayConfig(), session_manager=mgr)
    res = await get_dispatcher().dispatch("1", "sessions.questions", params, ctx)
    assert res.ok, res.error
    return res.payload


async def test_lists_only_questions_still_waiting_for_an_answer() -> None:
    store = SessionStorage(":memory:")
    await store.connect()
    try:
        mgr = SessionManager(store, inject_time_prefix=False)
        waiting, answered, plain = (f"agent:main:webchat:{n}" for n in ("q1", "q2", "q3"))
        for key in (waiting, answered, plain):
            await mgr.create(key, agent_id="main")
            await mgr.append_message(key, "user", "swap for me")
        await mgr.append_message(waiting, "assistant", "One thing first.", tool_calls=_asked("t1"))
        await mgr.append_message(answered, "assistant", "One thing first.", tool_calls=_asked("t2"))
        await mgr.append_message(answered, "user", "Base")
        await mgr.append_message(plain, "assistant", "Done.")

        payload = await _questions(mgr)
    finally:
        await store.close()

    assert [q["key"] for q in payload["questions"]] == [waiting]
    item = payload["questions"][0]
    assert item["toolUseId"] == "t1"
    assert item["questions"][0]["question"] == "Which chain?"
    assert isinstance(item["askedAt"], int)


async def test_without_sessions_there_is_nothing_to_list() -> None:
    ctx = RpcContext(conn_id="c", config=GatewayConfig(), session_manager=None)
    res = await get_dispatcher().dispatch("1", "sessions.questions", {"limit": "x"}, ctx)
    assert res.ok, res.error
    assert res.payload == {"questions": []}
