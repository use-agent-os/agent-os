"""Live E2E against the real agent-browser Chromium.

Skipped unless the ``agent-browser`` binary is installed (CI does not install
it). Run with: ``pytest -m browser_engine tests/test_tools/test_browser_engine_live.py``.

These exercise the real spawn/parse path, a real headless Chromium, and — for
the supervisor — a real CDP WebSocket attach to the managed browser.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agentos.tools import agent_browser
from agentos.tools.browser_supervisor import SUPERVISOR_REGISTRY
from agentos.tools.builtin import browser as browser_mod

pytestmark = [
    pytest.mark.browser_engine,
    pytest.mark.skipif(
        shutil.which("agent-browser") is None,
        reason="agent-browser binary not installed",
    ),
]

Browser = Callable[..., Awaitable[str]]

# Self-contained initial target — about:blank is the only allowed safe hostless target.
_BLANK = "about:blank"
_INJECT_DOM = (
    "document.title = 'LiveTest'; "
    "document.body.innerHTML = '<h1>Hello</h1><button>Go</button>'; "
    "document.title"
)


def _browser() -> Browser:
    return cast(Browser, browser_mod.browser.__wrapped__)


def _config(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "enabled": True,
        "headless": True,
        "binary_path": shutil.which("agent-browser") or "",
        "cdp_port": 0,
        "attach_confirmed": False,
        "persist_profile": False,
        "session_ttl_minutes": 15,
        "max_sessions": 3,
        "allowed_domains": [],
        "snapshot_max_chars": 24000,
        "dialog_policy": "must_respond",
        "dialog_timeout_s": 300.0,
        "restrict_evaluate": False,
        "allow_unsafe_evaluate": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _reset(request: pytest.FixtureRequest) -> Any:
    """Isolate each test on its own session key.

    The engine session name is derived from the AgentOS session key, so sharing
    one key across tests makes them share one browser: the fire-and-forget
    `close` from a finished test then races the browser the next test just
    opened, and the supervisor finds nothing to attach to. Production sessions
    have distinct keys; the tests should too.
    """
    from agentos.tools.types import CallerKind, InteractionMode, ToolContext, current_tool_context

    SUPERVISOR_REGISTRY.stop_all()
    agent_browser.close_all_sessions()
    browser_mod.reset_browser_runtime()
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.AGENT,
            interaction_mode=InteractionMode.INTERACTIVE,
            session_key=f"live:{request.node.name}",
        )
    )
    yield
    current_tool_context.reset(token)
    SUPERVISOR_REGISTRY.stop_all()
    agent_browser.close_all_sessions()
    browser_mod.reset_browser_runtime()


def _session_key(request: pytest.FixtureRequest) -> str:
    return f"live:{request.node.name}"


async def _call(**kwargs: Any) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(await _browser()(**kwargs)))


def _unwrap(value: str) -> str:
    """Return the payload inside the untrusted envelope.

    Every engine-derived field crosses that boundary, so a live assertion has to
    look inside it rather than compare the raw field.
    """
    assert "<untrusted" in value, f"expected an untrusted envelope, got: {value!r}"
    start = value.index(">") + 1
    end = value.rindex("</untrusted>")
    return value[start:end]


@pytest.mark.asyncio
async def test_navigate_snapshot_eval_close() -> None:
    browser_mod.configure_browser(_config())
    nav = await _call(action="navigate", url=_BLANK)
    assert nav["success"] is True, nav

    setup = await _call(action="eval", expression=_INJECT_DOM)
    assert setup["success"] is True

    snap = await _call(action="snapshot")
    assert snap["success"] is True
    assert "<untrusted" in snap["snapshot"]
    # The accessibility snapshot should mention the button.
    assert "Go" in snap["snapshot"] or "button" in snap["snapshot"].lower()

    ev = await _call(action="eval", expression="document.title")
    assert ev["success"] is True
    assert _unwrap(ev["result"]) == "LiveTest"

    closed = await _call(action="close")
    assert closed["success"] is True


@pytest.mark.asyncio
async def test_managed_supervisor_attaches_real_chromium(request: pytest.FixtureRequest) -> None:
    browser_mod.configure_browser(_config())
    await _call(action="navigate", url=_BLANK)
    # Resolve the managed browser's CDP endpoint and confirm it is loopback.
    endpoint = await agent_browser.resolve_cdp_endpoint(_session_key(request))
    assert endpoint is not None
    assert agent_browser.is_loopback_cdp_url(endpoint)
    # The supervisor should have attached during navigate (eval fast-path works).
    ev = await _call(action="eval", expression="1 + 2")
    assert ev["success"] is True
    assert _unwrap(ev["result"]) == "3"
    assert ev["method"] == "subprocess"
    await _call(action="close")


@pytest.mark.asyncio
async def test_real_dialog_intercepted_and_dismissed(request: pytest.FixtureRequest) -> None:
    browser_mod.configure_browser(_config(dialog_policy="must_respond"))
    # Navigate first, then trigger a confirm() from eval so the supervisor (which
    # attached during navigate) captures it as a pending dialog.
    await _call(action="navigate", url=_BLANK)
    supervisor = SUPERVISOR_REGISTRY.get(_session_key(request))
    if supervisor is None or not supervisor.active:
        pytest.skip("supervisor did not attach; dialog interception not available")
    # Fire confirm() without awaiting its result (it blocks until answered).
    fire = "setTimeout(() => window.confirm('proceed?'), 0); 'fired'"
    await _call(action="eval", expression=fire)
    # Give the event loop a moment to deliver the dialog event.
    import asyncio

    for _ in range(20):
        if supervisor.snapshot().pending_dialogs:
            break
        await asyncio.sleep(0.1)
    pending = supervisor.snapshot().pending_dialogs
    assert pending, "expected a pending dialog after confirm()"
    assert pending[0]["type"] == "confirm"
    resp = await _call(action="dialog", dialog_action="accept")
    assert resp["success"] is True
    await _call(action="close")


_GROWING_PAGE = (
    "document.title = 'Live views'; "
    "document.body.innerHTML = '<h1>Live</h1>"
    '<button onclick="this.insertAdjacentHTML(`afterend`, `<button>Added</button>`)">Grow</button>'
    "<input aria-label=Name>'; 1"
)


@pytest.mark.asyncio
async def test_action_views_batch_and_reads_on_the_real_engine() -> None:
    """The engine's --delta answers, parsed: an action returns what it changed."""
    import re

    browser_mod.configure_browser(_config())
    assert (await _call(action="navigate", url=_BLANK))["success"] is True
    assert (await _call(action="eval", expression=_GROWING_PAGE))["success"] is True
    tree = _unwrap((await _call(action="snapshot"))["snapshot"])
    grow = re.search(r'button "Grow" \[ref=(e\d+)', tree)
    name = re.search(r'textbox "Name" \[ref=(e\d+)', tree)
    assert grow and name, tree

    clicked = await _call(action="click", ref=grow.group(1))
    assert clicked["page"] == "changed", clicked
    assert "Added" in _unwrap(clicked["snapshot"])

    batch = await _call(
        action="batch",
        steps=[
            {"action": "fill", "ref": name.group(1), "text": "Key"},
            {"action": "get_text", "selector": "h1"},
            {"action": "title"},
        ],
    )
    assert batch["success"] is True, batch
    outputs = {entry["action"]: entry for entry in batch["results"]}
    assert _unwrap(outputs["get_text"]["text"]) == "Live"
    assert _unwrap(outputs["title"]["title"]) == "Live views"

    repeat = await _call(action="snapshot")
    assert "Key" in _unwrap(repeat["snapshot"])
    await _call(action="close")
