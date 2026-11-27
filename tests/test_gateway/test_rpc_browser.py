"""The operator's live browser view (``browser.*``), driven through a fake engine.

The fake ``agent-browser`` keeps its tabs in a JSON file beside itself and
logs every invocation, so a test can assert both what the view showed and
which engine commands it ran -- above all, that reading status never runs
one and that a snapshot never starts a browser.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_tools.browser_fake_engine import posix_only, write_fake_engine

from agentos.gateway import rpc_browser
from agentos.gateway.access import ConnectionSurface
from agentos.gateway.agent_surface import AgentBinding
from agentos.gateway.auth import AccessContext
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.tools import agent_browser
from agentos.tools.browser_supervisor import SUPERVISOR_REGISTRY
from agentos.tools.builtin import browser as browser_tool
from agentos.tools.types import SSRFBlockedError

KEY = "agent:main:webchat:abc"
JPEG = b"\xff\xd8\xff\xe0FAKEJPEG\xff\xd9"

_FAKE_ENGINE = """#!/usr/bin/env python3
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
state_path = os.path.join(here, "engine_state.json")
with open(os.path.join(here, "engine_log.jsonl"), "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
argv = sys.argv[1:]
value_flags = {"--session", "--cdp", "--allowed-domains", "--session-name",
               "--screenshot-format", "--screenshot-quality"}
i = 0; cmd = None; rest = []
while i < len(argv):
    tok = argv[i]
    if tok in value_flags:
        i += 2; continue
    if tok.startswith("--"):
        i += 1; continue
    cmd = tok; rest = argv[i + 1:]; break
positional = []
j = 0
while j < len(rest):
    if rest[j] in value_flags:
        j += 2; continue
    positional.append(rest[j]); j += 1
try:
    state = json.load(open(state_path))
except Exception:
    state = {"tabs": [{"tabId": "t1", "title": "", "url": "about:blank", "active": True,
                       "type": "page"}], "redirect": {}, "fail": []}
def save():
    json.dump(state, open(state_path, "w"))
def out(d):
    print(json.dumps({"success": True, "data": d, "error": None}))
def fail(msg):
    print(json.dumps({"success": False, "data": None, "error": msg}))
active = next(t for t in state["tabs"] if t["active"])
if cmd in state.get("fail", []):
    fail(cmd + " broke")
elif cmd == "open":
    url = positional[0]
    url = state["redirect"].get(url, url)
    active["url"] = url
    active["title"] = "Title of " + url
    save()
    out({"title": active["title"], "url": url})
elif cmd == "tab" and positional[:1] == ["list"]:
    out({"tabs": state["tabs"]})
elif cmd == "tab":
    for t in state["tabs"]:
        t["active"] = t["tabId"] == positional[0]
    save()
    out({"tabId": positional[0]})
elif cmd == "screenshot":
    with open(positional[-1], "wb") as handle:
        handle.write(bytes.fromhex(os.environ.get("FAKE_JPEG", "ffd8ffe046414b454a504547ffd9")))
    out({"path": positional[-1]})
elif cmd in ("back", "forward", "reload"):
    out({"url": active["url"]})
elif cmd == "get":
    fail("no cdp url in the fake")
else:
    out({"ok": True})
"""


@pytest.fixture
def engine(tmp_path: Path) -> Path:
    binary = write_fake_engine(tmp_path, _FAKE_ENGINE)
    browser_tool.configure_browser(SimpleNamespace(enabled=True, binary_path=binary))
    return tmp_path


@pytest.fixture(autouse=True)
def _reset() -> Any:
    SUPERVISOR_REGISTRY.stop_all()
    agent_browser.close_all_sessions()
    browser_tool.reset_browser_runtime()
    yield
    SUPERVISOR_REGISTRY.stop_all()
    agent_browser.close_all_sessions()
    browser_tool.reset_browser_runtime()


@pytest.fixture(autouse=True)
def captures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Captures go under this test's tmp path, and none is left when it ends."""
    parent = tmp_path / "captures"
    parent.mkdir()
    rpc_browser.release_capture_dir()
    monkeypatch.setattr(rpc_browser, "CAPTURE_PARENT", str(parent))
    yield parent
    rpc_browser.release_capture_dir()
    assert _capture_dirs(parent) == []


def _capture_dirs(parent: Path) -> list[Path]:
    return sorted(parent.glob(f"{rpc_browser.CAPTURE_PREFIX}*"))


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stand in for DNS: ``*.internal`` hosts resolve privately, others publicly."""
    checked: list[str] = []

    def validate(url: str) -> None:
        checked.append(url)
        if ".internal" in url:
            raise SSRFBlockedError(f"Blocked: {url} resolves to 10.0.0.5 (private network)")

    monkeypatch.setattr(browser_tool, "validate_http_url_for_fetch", validate)
    return checked


def _binary(engine: Path) -> str:
    """The fake engine ``write_fake_engine`` put in ``engine`` (a .bat on Windows)."""
    return str(engine / ("agent-browser.bat" if sys.platform == "win32" else "agent-browser"))


def _log(engine: Path) -> list[list[str]]:
    path = engine / "engine_log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _commands(engine: Path) -> list[str]:
    """Engine commands run, minus ``get`` (the supervisor's endpoint probe)."""
    commands = []
    for argv in _log(engine):
        i = 0
        while i < len(argv):
            if argv[i] in {"--session", "--cdp", "--allowed-domains", "--session-name"}:
                i += 2
                continue
            if argv[i].startswith("--"):
                i += 1
                continue
            if argv[i] != "get":
                commands.append(argv[i])
            break
    return commands


def _set_state(engine: Path, **changes: Any) -> None:
    path = engine / "engine_state.json"
    state = json.loads(path.read_text())
    state.update(changes)
    path.write_text(json.dumps(state))


def _ctx(*, via: str | None = None, surface: ConnectionSurface = ConnectionSurface.CONTROL):
    return RpcContext(
        conn_id="conn-1",
        access=AccessContext(
            surface=surface,
            admitted=True,
            credential_verified=True,
            agent=AgentBinding(via=via) if via else None,
        ),
    )


async def _call(method: str, params: Any, ctx: RpcContext | None = None) -> Any:
    return await get_dispatcher().dispatch("r1", method, params, ctx or _ctx())


async def _start(engine: Path) -> None:
    res = await _call("browser.start", {"sessionKey": KEY})
    assert res.ok, res.error


METHODS = [
    "browser.status",
    "browser.snapshot",
    "browser.start",
    "browser.navigate",
    "browser.history",
    "browser.tabs.select",
]


class TestSurface:
    def test_every_method_is_registered_control_only(self) -> None:
        dispatcher = get_dispatcher()
        for method in METHODS:
            entry = dispatcher.get_entry(method)
            assert entry is not None, method
            assert entry.audiences == frozenset({ConnectionSurface.CONTROL})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", METHODS)
    @pytest.mark.parametrize("via", ["token", "process", "window"])
    async def test_an_agent_is_refused(self, engine: Path, method: str, via: str) -> None:
        params = {"sessionKey": KEY, "url": "https://example.com", "action": "back", "tabId": "t1"}
        res = await _call(method, params, _ctx(via=via))
        assert not res.ok and res.error.code == "agent.operator_required"
        assert _log(engine) == []
        assert agent_browser.get_session(KEY) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", METHODS)
    async def test_a_channel_is_refused(self, engine: Path, method: str) -> None:
        res = await _call(method, {"sessionKey": KEY}, _ctx(surface=ConnectionSurface.CHANNEL))
        assert not res.ok
        assert _log(engine) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", METHODS)
    @pytest.mark.parametrize("key", [None, "", "   ", 7, "a b", "tab\tkey", "x" * 600])
    async def test_a_bad_session_key_is_invalid(self, engine: Path, method: str, key: Any) -> None:
        res = await _call(method, {"sessionKey": key})
        assert not res.ok and res.error.code == "INVALID_REQUEST"
        assert _log(engine) == []


def test_gateway_shutdown_removes_the_capture_directory(captures: Path) -> None:
    """``agentos gateway stop`` ends in the ASGI lifespan shutdown: the
    directory goes with the browsers, not only at interpreter exit."""
    from starlette.testclient import TestClient

    from agentos.gateway.app import create_gateway_app
    from agentos.gateway.config import GatewayConfig

    rpc_browser._capture_path()
    assert len(_capture_dirs(captures)) == 1
    with TestClient(create_gateway_app(GatewayConfig())):
        pass
    assert _capture_dirs(captures) == []


class TestStatus:
    @pytest.mark.asyncio
    async def test_reports_a_stopped_browser_without_running_the_engine(self, engine: Path) -> None:
        res = await _call("browser.status", {"sessionKey": KEY})
        assert res.ok, res.error
        assert res.payload == {
            "sessionKey": KEY,
            "enabled": True,
            "installed": True,
            "mode": "managed",
            "running": False,
            "attachConfirmed": True,
            "canStart": True,
        }
        assert _log(engine) == []

    @pytest.mark.asyncio
    async def test_disabled_browser_is_reported_not_an_error(self, engine: Path) -> None:
        browser_tool.configure_browser(SimpleNamespace(enabled=False))
        res = await _call("browser.status", {"sessionKey": KEY})
        assert res.ok
        assert res.payload["enabled"] is False
        assert res.payload["canStart"] is False
        for method in METHODS[1:]:
            refused = await _call(
                method, {"sessionKey": KEY, "url": "https://a.test", "action": "back"}
            )
            if method == "browser.tabs.select":
                continue  # its params fail validation first: no tab id
            assert not refused.ok and refused.error.details == {"code": "browser.disabled"}
        assert _log(engine) == []

    @pytest.mark.asyncio
    async def test_missing_engine_carries_the_install_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(agent_browser, "resolve_binary", lambda: None)
        browser_tool.configure_browser(SimpleNamespace(enabled=True))
        res = await _call("browser.status", {"sessionKey": KEY})
        assert res.ok
        assert res.payload["installed"] is False
        assert "agent-browser" in res.payload["installHint"]
        assert "binaryPath" not in res.payload
        start = await _call("browser.start", {"sessionKey": KEY})
        assert not start.ok and start.error.details == {"code": "browser.not_installed"}

    @pytest.mark.asyncio
    async def test_unconfirmed_attach_mode_cannot_start(self, engine: Path) -> None:
        binary = _binary(engine)
        browser_tool.configure_browser(
            SimpleNamespace(enabled=True, binary_path=binary, cdp_port=9222)
        )
        res = await _call("browser.status", {"sessionKey": KEY})
        assert res.payload["mode"] == "attach"
        assert res.payload["attachConfirmed"] is False
        assert res.payload["canStart"] is False
        start = await _call("browser.start", {"sessionKey": KEY})
        assert not start.ok and start.error.details == {"code": "browser.attach_unconfirmed"}
        assert _log(engine) == []


@posix_only
class TestSnapshot:
    @pytest.mark.asyncio
    async def test_a_session_without_a_browser_is_not_started(self, engine: Path) -> None:
        res = await _call("browser.snapshot", {"sessionKey": KEY})
        assert res.ok, res.error
        assert res.payload["running"] is False
        assert res.payload["tabs"] == []
        assert _log(engine) == []
        assert agent_browser.get_session(KEY) is None

    @pytest.mark.asyncio
    async def test_tabs_and_a_jpeg_of_the_active_tab(
        self, engine: Path, public_dns: list[str]
    ) -> None:
        await _start(engine)
        _set_state(
            engine,
            tabs=[
                {"tabId": "t1", "title": "One", "url": "https://one.test/", "active": False},
                {"tabId": "t2", "title": "Two", "url": "https://two.test/", "active": True},
                {"tabId": "w1", "title": "worker", "url": "x", "active": False},
                {"tabId": "t3", "title": "sw", "url": "y", "active": False, "type": "worker"},
            ],
        )
        res = await _call("browser.snapshot", {"sessionKey": KEY})
        assert res.ok, res.error
        body = res.payload
        assert [tab["tabId"] for tab in body["tabs"]] == ["t1", "t2"]
        assert body["activeTabId"] == "t2"
        assert body["url"] == "https://two.test/"
        assert body["title"] == "Two"
        assert body["image"]["mime"] == "image/jpeg"
        assert base64.b64decode(body["image"]["data"]) == JPEG
        assert "withheld" not in body
        argv = _log(engine)[-1]
        assert "screenshot" in argv and "jpeg" in argv
        # The capture file is removed once read.
        assert not Path(argv[-1]).exists()

    @pytest.mark.asyncio
    async def test_the_capture_directory_is_private_and_removed(
        self, engine: Path, captures: Path
    ) -> None:
        await _start(engine)
        res = await _call("browser.snapshot", {"sessionKey": KEY})
        assert res.ok, res.error
        [directory] = _capture_dirs(captures)
        # One directory for the process, emptied after every capture.
        assert list(directory.iterdir()) == []
        if sys.platform != "win32":
            assert directory.stat().st_mode & 0o777 == 0o700
        rpc_browser.release_capture_dir()
        assert _capture_dirs(captures) == []
        rpc_browser.release_capture_dir()  # a second release is a no-op

    @pytest.mark.asyncio
    async def test_a_failed_capture_leaves_no_file(self, engine: Path, captures: Path) -> None:
        await _start(engine)
        _set_state(engine, fail=["screenshot"])
        res = await _call("browser.snapshot", {"sessionKey": KEY})
        assert not res.ok
        assert [list(d.iterdir()) for d in _capture_dirs(captures)] == [[]]

    @pytest.mark.asyncio
    async def test_the_capture_directory_goes_at_process_exit(
        self, engine: Path, captures: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registered: list[Any] = []
        monkeypatch.setattr(rpc_browser.atexit, "register", registered.append)
        monkeypatch.setattr(rpc_browser, "_exit_cleanup_registered", False)
        await _start(engine)
        assert (await _call("browser.snapshot", {"sessionKey": KEY})).ok
        assert (await _call("browser.snapshot", {"sessionKey": KEY})).ok
        assert registered == [rpc_browser.release_capture_dir]
        assert len(_capture_dirs(captures)) == 1
        registered[0]()  # what the interpreter runs at exit
        assert _capture_dirs(captures) == []

    @pytest.mark.asyncio
    async def test_viewing_never_extends_the_browsers_life(self, engine: Path) -> None:
        await _start(engine)
        session = agent_browser.get_session(KEY)
        assert session is not None
        session.last_used_at = 1.0
        res = await _call("browser.snapshot", {"sessionKey": KEY})
        assert res.ok, res.error
        assert agent_browser.get_session(KEY).last_used_at == 1.0

    @pytest.mark.asyncio
    async def test_a_private_page_is_not_captured(self, engine: Path) -> None:
        await _start(engine)
        _set_state(
            engine,
            tabs=[{"tabId": "t1", "title": "Router", "url": "http://192.168.1.1/", "active": True}],
        )
        res = await _call("browser.snapshot", {"sessionKey": KEY})
        assert res.ok, res.error
        assert res.payload["withheld"] == "private"
        assert res.payload["image"] is None
        assert "screenshot" not in _commands(engine)

    @pytest.mark.asyncio
    async def test_the_image_can_be_skipped(self, engine: Path) -> None:
        await _start(engine)
        res = await _call("browser.snapshot", {"sessionKey": KEY, "image": False})
        assert res.ok and res.payload["image"] is None
        assert "screenshot" not in _commands(engine)

    @pytest.mark.asyncio
    async def test_an_oversized_capture_is_refused(
        self, engine: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _start(engine)
        monkeypatch.setattr(rpc_browser, "MAX_IMAGE_BYTES", 4)
        res = await _call("browser.snapshot", {"sessionKey": KEY})
        assert not res.ok and res.error.details == {"code": "browser.too_large"}

    @pytest.mark.asyncio
    async def test_an_engine_failure_is_an_error_with_a_code(self, engine: Path) -> None:
        await _start(engine)
        _set_state(engine, fail=["screenshot"])
        res = await _call("browser.snapshot", {"sessionKey": KEY})
        assert not res.ok
        assert res.error.code == "UNAVAILABLE"
        assert res.error.details == {"code": "browser.failed"}
        assert "screenshot broke" in res.error.message

    @pytest.mark.asyncio
    async def test_page_strings_are_capped(self, engine: Path) -> None:
        await _start(engine)
        many = [
            {"tabId": f"t{n}", "title": "T" * 1000, "url": "https://a.test/", "active": n == 1}
            for n in range(1, rpc_browser.MAX_TABS + 6)
        ]
        _set_state(engine, tabs=many)
        res = await _call("browser.snapshot", {"sessionKey": KEY, "image": False})
        assert res.ok
        assert len(res.payload["tabs"]) == rpc_browser.MAX_TABS
        assert res.payload["truncated"] is True
        assert len(res.payload["title"]) == rpc_browser.MAX_TITLE_LENGTH


@posix_only
class TestStart:
    @pytest.mark.asyncio
    async def test_starts_on_about_blank(self, engine: Path) -> None:
        res = await _call("browser.start", {"sessionKey": KEY})
        assert res.ok, res.error
        assert res.payload["running"] is True
        assert res.payload["canStart"] is False
        assert _log(engine)[0][-2:] == ["open", "about:blank"]
        assert agent_browser.get_session(KEY) is not None

    @pytest.mark.asyncio
    async def test_attach_mode_starts_in_a_new_tab(self, engine: Path) -> None:
        # The user's own Chrome: the tab they are on is never sent to about:blank.
        binary = _binary(engine)
        browser_tool.configure_browser(
            SimpleNamespace(enabled=True, binary_path=binary, cdp_port=9222, attach_confirmed=True)
        )
        res = await _call("browser.start", {"sessionKey": KEY})
        assert res.ok, res.error
        assert res.payload["mode"] == "attach"
        first = _log(engine)[0]
        assert first[-2:] == ["tab", "new"]
        assert "open" not in _commands(engine)

    @pytest.mark.asyncio
    async def test_starting_twice_opens_once(self, engine: Path) -> None:
        await _start(engine)
        await _start(engine)
        assert _commands(engine).count("open") == 1

    @pytest.mark.asyncio
    async def test_a_failed_start_does_not_read_as_running(self, engine: Path) -> None:
        (engine / "engine_state.json").write_text(
            json.dumps(
                {"tabs": [{"tabId": "t1", "active": True}], "redirect": {}, "fail": ["open"]}
            )
        )
        res = await _call("browser.start", {"sessionKey": KEY})
        assert not res.ok and res.error.details == {"code": "browser.failed"}
        assert agent_browser.get_session(KEY) is None
        status = await _call("browser.status", {"sessionKey": KEY})
        assert status.payload["running"] is False


@posix_only
class TestNavigate:
    @pytest.mark.asyncio
    async def test_opens_a_public_page(self, engine: Path, public_dns: list[str]) -> None:
        await _start(engine)
        res = await _call("browser.navigate", {"sessionKey": KEY, "url": "https://example.com/"})
        assert res.ok, res.error
        assert res.payload["url"] == "https://example.com/"
        assert res.payload["title"] == "Title of https://example.com/"
        # Checked before opening, and the page it ended on after redirects.
        assert public_dns == ["https://example.com/", "https://example.com/"]

    @pytest.mark.asyncio
    async def test_needs_a_running_browser(self, engine: Path, public_dns: list[str]) -> None:
        res = await _call("browser.navigate", {"sessionKey": KEY, "url": "https://example.com/"})
        assert not res.ok and res.error.details == {"code": "browser.not_running"}
        assert _log(engine) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "file:///etc/passwd",
            "data:text/html,<script>1</script>",
            "chrome://settings",
            "about:config",
            "view-source:https://example.com",
            "ftp://example.com/",
            "--executable-path=/bin/sh",
        ],
    )
    async def test_refuses_anything_but_http(self, engine: Path, url: str) -> None:
        await _start(engine)
        res = await _call("browser.navigate", {"sessionKey": KEY, "url": url})
        assert not res.ok
        assert res.error.code == "FORBIDDEN"
        assert res.error.details == {"code": "browser.blocked"}
        assert _commands(engine).count("open") == 1  # only the start

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:18791/",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/",
            "http://10.0.0.8/admin",
        ],
    )
    async def test_the_tool_ssrf_policy_applies(self, engine: Path, url: str) -> None:
        # The real validator: IP literals need no DNS.
        await _start(engine)
        res = await _call("browser.navigate", {"sessionKey": KEY, "url": url})
        assert not res.ok and res.error.details == {"code": "browser.blocked"}
        assert _commands(engine).count("open") == 1

    @pytest.mark.asyncio
    async def test_the_tool_allowlist_applies(self, engine: Path, public_dns: list[str]) -> None:
        binary = _binary(engine)
        browser_tool.configure_browser(
            SimpleNamespace(enabled=True, binary_path=binary, allowed_domains=["example.com"])
        )
        await _start(engine)
        ok = await _call("browser.navigate", {"sessionKey": KEY, "url": "https://docs.example.com"})
        assert ok.ok, ok.error
        res = await _call("browser.navigate", {"sessionKey": KEY, "url": "https://evil.test/"})
        assert not res.ok and res.error.details == {"code": "browser.blocked"}
        assert "allowed_domains" in res.error.message

    @pytest.mark.asyncio
    async def test_a_redirect_to_a_private_address_is_left(
        self, engine: Path, public_dns: list[str]
    ) -> None:
        await _start(engine)
        _set_state(engine, redirect={"https://short.test/x": "http://db.internal/"})
        res = await _call("browser.navigate", {"sessionKey": KEY, "url": "https://short.test/x"})
        assert not res.ok and res.error.details == {"code": "browser.blocked"}
        state = json.loads((engine / "engine_state.json").read_text())
        assert state["tabs"][0]["url"] == "about:blank"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url", [None, "", 5, "https://a.test/\nX", "https://a.test/" + "a" * 3000]
    )
    async def test_malformed_urls_are_invalid(self, engine: Path, url: Any) -> None:
        res = await _call("browser.navigate", {"sessionKey": KEY, "url": url})
        assert not res.ok and res.error.code == "INVALID_REQUEST"
        assert _log(engine) == []


@posix_only
class TestHistoryAndTabs:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["back", "forward", "reload"])
    async def test_runs_exactly_the_named_action(self, engine: Path, action: str) -> None:
        await _start(engine)
        res = await _call("browser.history", {"sessionKey": KEY, "action": action})
        assert res.ok, res.error
        assert _commands(engine)[-1] == action

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["eval", "close", "open", "", None, "back; reload"])
    async def test_any_other_action_is_invalid(self, engine: Path, action: Any) -> None:
        await _start(engine)
        res = await _call("browser.history", {"sessionKey": KEY, "action": action})
        assert not res.ok and res.error.code == "INVALID_REQUEST"
        assert _commands(engine) == ["open"]

    @pytest.mark.asyncio
    async def test_history_needs_a_running_browser(self, engine: Path) -> None:
        res = await _call("browser.history", {"sessionKey": KEY, "action": "reload"})
        assert not res.ok and res.error.details == {"code": "browser.not_running"}
        assert _log(engine) == []

    @pytest.mark.asyncio
    async def test_selects_a_listed_tab(self, engine: Path) -> None:
        await _start(engine)
        _set_state(
            engine,
            tabs=[
                {"tabId": "t1", "title": "One", "url": "https://one.test/", "active": True},
                {"tabId": "t2", "title": "Two", "url": "https://two.test/", "active": False},
            ],
        )
        res = await _call("browser.tabs.select", {"sessionKey": KEY, "tabId": "t2"})
        assert res.ok, res.error
        snap = await _call("browser.snapshot", {"sessionKey": KEY, "image": False})
        assert snap.payload["activeTabId"] == "t2"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tab_id", ["new", "close", "t0", "list", "-1", "t1 t2", "", None, 1])
    async def test_a_tab_id_is_only_a_tab_id(self, engine: Path, tab_id: Any) -> None:
        await _start(engine)
        res = await _call("browser.tabs.select", {"sessionKey": KEY, "tabId": tab_id})
        assert not res.ok and res.error.code == "INVALID_REQUEST"
        assert _commands(engine) == ["open"]
