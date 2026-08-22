"""The browser tool: dispatch, SSRF, envelope, secret guard, truncation, allowlist,
attach gate, eval policy — driven through a real fake ``agent-browser`` engine.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from test_tools.browser_fake_engine import write_fake_engine

from agentos.tools import agent_browser
from agentos.tools.browser_supervisor import SUPERVISOR_REGISTRY
from agentos.tools.builtin import browser as browser_mod

Browser = Callable[..., Awaitable[str]]

# Fake engine: navigate→open returns {title,url}; snapshot returns a ~3000-char
# tree so truncation can be exercised; eval returns a value; get cdp-url loopback.
_FAKE_ENGINE = """#!/usr/bin/env python3
import json, sys
argv = sys.argv[1:]
value_flags = {"--session", "--cdp", "--allowed-domains", "--session-name", "--max-output"}
i = 0; cmd = None; rest = []
while i < len(argv):
    tok = argv[i]
    if tok in value_flags:
        i += 2; continue
    if tok.startswith("--"):
        i += 1; continue
    cmd = tok; rest = argv[i + 1:]; break
def out(d): print(json.dumps({"success": True, "data": d, "error": None}))
if cmd == "open":
    out({"title": "Fake", "url": rest[0] if rest else ""})
elif cmd == "snapshot":
    out({"snapshot": "button Go ref=e1 " * 180})
elif cmd == "eval":
    expr = rest[0] if rest else ""
    if "location.href" in expr:
        out({"result": "https://example.com/current"})
    else:
        out({"result": "subprocess-eval"})
elif cmd == "get":
    out({"cdpUrl": "ws://127.0.0.1:53870/devtools/browser/abc"})
elif cmd == "close":
    out({"closed": True})
else:
    out({"ok": True, "command": cmd, "args": rest})
"""


@pytest.fixture
def fake_binary(tmp_path: Path) -> str:
    return write_fake_engine(tmp_path, _FAKE_ENGINE)


@pytest.fixture(autouse=True)
def _reset() -> Any:
    SUPERVISOR_REGISTRY.stop_all()
    agent_browser.close_all_sessions()
    browser_mod.reset_browser_runtime()
    yield
    SUPERVISOR_REGISTRY.stop_all()
    agent_browser.close_all_sessions()
    browser_mod.reset_browser_runtime()


@pytest.fixture
def no_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip real DNS in the SSRF check so allowlist tests don't hit the network."""
    monkeypatch.setattr(browser_mod, "validate_http_url_for_fetch", lambda _url: None)


def _browser() -> Browser:
    return cast(Browser, browser_mod.browser.__wrapped__)


def _config(binary: str, **overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "enabled": True,
        "headless": True,
        "binary_path": binary,
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


async def _call(**kwargs: Any) -> dict[str, Any]:
    result = await _browser()(**kwargs)
    return cast(dict[str, Any], json.loads(result))


class TestDispatch:
    @pytest.mark.asyncio
    async def test_navigate_happy_path(self, fake_binary: str, no_dns: None) -> None:
        browser_mod.configure_browser(_config(fake_binary))
        result = await _call(action="navigate", url="https://example.com")
        assert result["success"] is True
        assert result["url"] == "https://example.com"
        # Snapshot wrapped in the untrusted envelope.
        assert "<untrusted" in result["snapshot"]
        assert "</untrusted>" in result["snapshot"]

    @pytest.mark.asyncio
    async def test_unknown_action(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary))
        result = await _call(action="frobnicate")
        assert result["success"] is False
        assert "unknown action" in result["error"]

    @pytest.mark.asyncio
    async def test_unavailable_engine(self, tmp_path: Path) -> None:
        browser_mod.configure_browser(_config(str(tmp_path / "missing"), enabled=False))
        result = await _call(action="snapshot")
        assert result["success"] is False
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    async def test_click_dispatches(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary))
        result = await _call(action="click", ref="e1")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_click_requires_ref(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary))
        result = await _call(action="click")
        assert result["success"] is False
        assert "ref" in result["error"]


class TestSsrf:
    @pytest.mark.asyncio
    async def test_metadata_endpoint_refused(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary))
        result = await _call(action="navigate", url="http://169.254.169.254/latest/meta-data")
        assert result["success"] is False
        assert "169.254.169.254" in result["error"] or "metadata" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_loopback_refused(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary))
        result = await _call(action="navigate", url="http://127.0.0.1:8080/admin")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_file_url_refused(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary))
        result = await _call(action="navigate", url="file:///etc/passwd")
        assert result["success"] is False
        assert "file:" in result["error"]

    @pytest.mark.asyncio
    async def test_data_url_refused(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary))
        result = await _call(action="navigate", url="data:text/html,<h1>hi</h1>")
        assert result["success"] is False
        assert "data:" in result["error"]

    @pytest.mark.asyncio
    async def test_about_blank_bypasses_allowlist(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary, allowed_domains=["example.com"]))
        result = await _call(action="navigate", url="about:blank")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_redirect_to_data_url_blocked(self, fake_binary: str, no_dns: None) -> None:
        browser_mod.configure_browser(_config(fake_binary))
        assert browser_mod._url_is_private("data:text/html,<script></script>") is True
        assert browser_mod._url_is_private("file:///etc/passwd") is True
        assert browser_mod._url_is_private("about:blank") is False


class TestAllowlist:
    @pytest.mark.asyncio
    async def test_off_list_domain_refused(self, fake_binary: str) -> None:
        # Allowlist is checked before any DNS resolution, so this needs no network.
        browser_mod.configure_browser(_config(fake_binary, allowed_domains=["example.com"]))
        result = await _call(action="navigate", url="https://evil.example.org/page")
        assert result["success"] is False
        assert "allowed_domains" in result["error"]

    @pytest.mark.asyncio
    async def test_on_list_domain_allowed(self, fake_binary: str, no_dns: None) -> None:
        browser_mod.configure_browser(_config(fake_binary, allowed_domains=["example.com"]))
        result = await _call(action="navigate", url="https://example.com/page")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_subdomain_of_allowed_permitted(self, fake_binary: str, no_dns: None) -> None:
        browser_mod.configure_browser(_config(fake_binary, allowed_domains=["example.com"]))
        result = await _call(action="navigate", url="https://docs.example.com/x")
        assert result["success"] is True


class TestSecretGuard:
    @pytest.mark.asyncio
    async def test_typing_a_token_refused(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary))
        secret = "sk-" + "a" * 40
        result = await _call(action="type", ref="e1", text=secret)
        assert result["success"] is False
        assert "credential" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_typing_env_value_refused(
        self, fake_binary: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MY_SECRET_VALUE", "super-secret-passphrase-123")
        browser_mod.configure_browser(_config(fake_binary))
        result = await _call(action="fill", ref="e1", text="super-secret-passphrase-123")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_ordinary_text_allowed(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary))
        result = await _call(action="type", ref="e1", text="hello world")
        assert result["success"] is True


class TestTruncation:
    @pytest.mark.asyncio
    async def test_snapshot_truncated_with_marker(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary, snapshot_max_chars=1000))
        result = await _call(action="snapshot")
        assert result["success"] is True
        assert result["truncated"] is True
        assert "truncated" in result["snapshot"]

    @pytest.mark.asyncio
    async def test_snapshot_not_truncated_when_short(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary, snapshot_max_chars=24000))
        result = await _call(action="snapshot")
        assert result["truncated"] is False


class TestAttachGate:
    @pytest.mark.asyncio
    async def test_attach_unconfirmed_refused(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary, cdp_port=9222, attach_confirmed=False))
        result = await _call(action="snapshot")
        assert result["success"] is False
        assert "attach_confirmed" in result["error"]

    @pytest.mark.asyncio
    async def test_attach_confirmed_passes(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary, cdp_port=9222, attach_confirmed=True))
        result = await _call(action="snapshot")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_managed_mode_never_gated(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary, cdp_port=0))
        result = await _call(action="snapshot")
        assert result["success"] is True


class TestEvalPolicy:
    @pytest.mark.asyncio
    async def test_denylist_blocks_cookie_when_restricted(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary, restrict_evaluate=True))
        result = await _call(action="eval", expression="document.cookie")
        assert result["success"] is False
        assert "restrict_evaluate" in result["error"]

    @pytest.mark.asyncio
    async def test_eval_allowed_by_default(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary))
        result = await _call(action="eval", expression="document.title")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_eval_requires_expression(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary))
        result = await _call(action="eval", expression="")
        assert result["success"] is False


class TestUntrustedBoundary:
    """Everything the engine returns is page-influenced and must cross into the
    transcript inside the envelope — not just the snapshot."""

    @pytest.mark.asyncio
    async def test_eval_result_is_wrapped(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary))
        result = await _call(action="eval", expression="document.body.innerText")
        assert result["success"] is True
        assert "<untrusted" in result["result"]
        assert "</untrusted>" in result["result"]

    @pytest.mark.asyncio
    async def test_tabs_payload_is_wrapped(self, fake_binary: str) -> None:
        # `tabs` returns titles and URLs the visited page chose.
        browser_mod.configure_browser(_config(fake_binary))
        result = await _call(action="tabs")
        assert result["success"] is True
        assert "<untrusted" in result["data"]

    @pytest.mark.asyncio
    async def test_click_payload_is_wrapped(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary))
        result = await _call(action="click", ref="e1")
        assert "<untrusted" in result["data"]


class TestEvalSsrfInManagedMode:
    """The managed Chromium runs on this host and reaches loopback, the LAN, and
    the metadata endpoint — the eval guards must not be attach-only."""

    @pytest.mark.asyncio
    async def test_metadata_fetch_refused_in_managed_mode(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary, cdp_port=0))
        assert browser_mod.agent_browser.is_attach_mode() is False
        result = await _call(
            action="eval",
            expression="fetch('http://169.254.169.254/latest/meta-data/').then(r=>r.text())",
        )
        assert result["success"] is False
        assert "169.254.169.254" in result["error"]

    @pytest.mark.asyncio
    async def test_loopback_fetch_refused_in_managed_mode(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary, cdp_port=0))
        result = await _call(action="eval", expression="fetch('http://127.0.0.1:8080/admin')")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_metadata_fetch_refused_in_attach_mode_too(self, fake_binary: str) -> None:
        browser_mod.configure_browser(_config(fake_binary, cdp_port=9222, attach_confirmed=True))
        result = await _call(
            action="eval", expression="fetch('http://169.254.169.254/latest/meta-data/')"
        )
        assert result["success"] is False


class TestDialogWithoutSupervisor:
    @pytest.mark.asyncio
    async def test_dialog_without_supervisor_is_actionable(self, fake_binary: str) -> None:
        # No navigate yet → no session/endpoint. Managed mode WILL try to resolve
        # an endpoint (get cdp-url), which the fake answers, so a supervisor can
        # attach. To test the no-supervisor message, use attach mode pointing at a
        # dead port so the live transport fails to connect.
        browser_mod.configure_browser(_config(fake_binary, cdp_port=9, attach_confirmed=True))
        result = await _call(action="dialog", dialog_action="accept")
        # Either the supervisor failed to attach (no supervisor message) or the
        # transport raised — both surface as a clean failure, never a crash.
        assert result["success"] is False
