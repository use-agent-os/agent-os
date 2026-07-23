"""Opt-in real-browser smoke for React Chat controls, without provider spend.

Run with:

    AGENTOS_WEBUI_BROWSER_CHAT_E2E=1 \
      uv run pytest tests/functional/test_webui_browser_chat_e2e.py -q -s
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.webui_browser

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = REPO_ROOT / "frontend"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _node() -> str:
    return "node.exe" if os.name == "nt" else "node"


@pytest.fixture(scope="module")
def playwright_node_modules() -> Path:
    if os.environ.get("AGENTOS_WEBUI_BROWSER_CHAT_E2E") != "1":
        pytest.skip("set AGENTOS_WEBUI_BROWSER_CHAT_E2E=1 to run chat browser smoke")

    package_manifest = json.loads((FRONTEND_DIR / "package.json").read_text(encoding="utf-8"))
    expected_version = package_manifest["devDependencies"]["playwright"]
    installed_manifest_path = FRONTEND_DIR / "node_modules" / "playwright" / "package.json"
    if not installed_manifest_path.is_file():
        pytest.fail("Playwright is not installed; run `npm --prefix frontend ci` first")
    installed_manifest = json.loads(installed_manifest_path.read_text(encoding="utf-8"))
    assert installed_manifest["version"] == expected_version
    return FRONTEND_DIR / "node_modules"


def _wait_for_health(port: int, server: subprocess.Popen[str]) -> None:
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + 20.0
    last_error = ""
    while time.monotonic() < deadline:
        if server.poll() is not None:
            stdout = server.stdout.read() if server.stdout else ""
            stderr = server.stderr.read() if server.stderr else ""
            raise AssertionError(
                f"gateway exited early code={server.returncode}\nstdout={stdout}\nstderr={stderr}"
            )
        try:
            response = httpx.get(url, timeout=1.0)
            if response.status_code == 200 and response.json().get("ok") is True:
                return
        except Exception as exc:  # noqa: BLE001 - included in the timeout assertion.
            last_error = str(exc)
        time.sleep(0.1)
    raise AssertionError(f"gateway did not become healthy: {last_error}")


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=8)


def test_react_chat_public_controls_work_without_sending_to_provider(
    tmp_path: Path,
    playwright_node_modules: Path,
) -> None:
    port = _free_port()
    server_script = tmp_path / "react_chat_smoke_server.py"
    browser_script = tmp_path / "react_chat_smoke_browser.js"
    server_script.write_text(
        textwrap.dedent(
            f"""
            import uvicorn

            from agentos.gateway.app import create_gateway_app
            from agentos.gateway.config import AuthConfig, GatewayConfig

            config = GatewayConfig(
                host="127.0.0.1",
                port={port},
                auth=AuthConfig(mode="none"),
            )
            app = create_gateway_app(config)

            if __name__ == "__main__":
                uvicorn.run(app, host="127.0.0.1", port={port}, log_level="warning")
            """
        ),
        encoding="utf-8",
    )
    browser_script.write_text(
        textwrap.dedent(
            """
            const { chromium } = require("playwright");

            (async () => {
              const browser = await chromium.launch({ headless: true });
              const page = await browser.newPage();
              const diagnostics = {
                pageErrors: [],
                consoleErrors: [],
                requestFailures: [],
                websocketFrames: [],
              };
              page.on("pageerror", error => diagnostics.pageErrors.push(String(error)));
              page.on("console", message => {
                if (message.type() === "error") diagnostics.consoleErrors.push(message.text());
              });
              page.on("requestfailed", request => {
                diagnostics.requestFailures.push({
                  url: request.url(),
                  error: request.failure()?.errorText || "unknown failure",
                });
              });
              page.on("websocket", socket => {
                socket.on("framesent", event => {
                  diagnostics.websocketFrames.push(String(event.payload));
                });
              });

              const response = await page.goto(process.env.TARGET_URL, {
                waitUntil: "domcontentloaded",
                timeout: 30000,
              });
              await page.getByRole("heading", { name: "Chat" }).waitFor({ timeout: 15000 });
              await page
                .getByRole("region", { name: "Chat toolbar" })
                .waitFor({ timeout: 15000 });

              const sessionGroup = page.getByRole("group", { name: "Chat session controls" });
              const switchSession = page.getByRole("button", { name: "Switch chat session" });
              const newChat = page.getByRole("button", { name: "New chat" });
              const message = page.getByRole("textbox", { name: "Message" });
              const send = page.getByRole("button", { name: "Send" });
              const runModes = page.getByRole("button", { name: "Run modes" });

              const controlCounts = {
                toolbar: await page.getByRole("region", { name: "Chat toolbar" }).count(),
                sessionGroup: await sessionGroup.count(),
                switchSession: await switchSession.count(),
                newChat: await newChat.count(),
                message: await message.count(),
                send: await send.count(),
                runModes: await runModes.count(),
              };

              await switchSession.click();
              await page.getByRole("dialog", { name: "Switch session" }).waitFor();
              await page.keyboard.press("Escape");

              const sessionBefore = (await switchSession.textContent()).trim();
              await newChat.click();
              await page.waitForFunction(
                previous =>
                  document
                    .querySelector('[aria-label="Switch chat session"]')
                    ?.textContent?.trim() !== previous,
                sessionBefore
              );
              const sessionAfter = (await switchSession.textContent()).trim();
              const composerFocusedAfterNewChat = await message.evaluate(
                element => element === document.activeElement
              );

              await message.fill("Browser smoke draft — intentionally not sent");
              const sendEnabledForDraft = await send.isEnabled();
              const draft = await message.inputValue();

              await runModes.click();
              const runModesDialog = page.getByRole("dialog", { name: "Run modes" });
              await runModesDialog.waitFor();
              const runModesDialogCount = await runModesDialog.count();
              await page.getByRole("button", { name: "Close run modes" }).click();
              await runModesDialog.waitFor({ state: "detached" });

              const legacy = await page.evaluate(() => ({
                app: typeof window.App,
                router: typeof window.Router,
                dataNode: document.querySelector("#agentos-data") !== null,
              }));
              const currentSession = new URL(page.url()).searchParams.get("session");
              const result = {
                status: response ? response.status() : 0,
                title: await page.title(),
                controlCounts,
                sessionBefore,
                sessionAfter,
                currentSession,
                composerFocusedAfterNewChat,
                sendEnabledForDraft,
                draft,
                runModesDialogCount,
                legacy,
                diagnostics,
              };
              await browser.close();
              console.log(JSON.stringify(result));
            })().catch(error => {
              console.error(error && error.stack ? error.stack : String(error));
              process.exit(1);
            });
            """
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["AGENTOS_STATE_DIR"] = str(tmp_path / "state")
    server = subprocess.Popen(
        [sys.executable, str(server_script)],
        cwd=Path.cwd(),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        _wait_for_health(port, server)
        browser_env = dict(
            env,
            TARGET_URL=(
                f"http://127.0.0.1:{port}/control/chat?session=agent:main:webchat:browser-smoke"
            ),
            NODE_PATH=str(playwright_node_modules),
        )
        result = subprocess.run(
            [_node(), str(browser_script)],
            cwd=tmp_path,
            env=browser_env,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    finally:
        _stop_process(server)

    assert payload["status"] == 200
    assert payload["title"] == "Chat - AgentOS Control"
    assert payload["controlCounts"] == {
        "toolbar": 1,
        "sessionGroup": 1,
        "switchSession": 1,
        "newChat": 1,
        "message": 1,
        "send": 1,
        "runModes": 1,
    }
    assert payload["sessionBefore"] == "agent:main:webchat:browser-smoke"
    assert payload["sessionAfter"] != payload["sessionBefore"]
    assert payload["currentSession"] == payload["sessionAfter"]
    assert payload["composerFocusedAfterNewChat"] is True
    assert payload["sendEnabledForDraft"] is True
    assert payload["draft"] == "Browser smoke draft — intentionally not sent"
    assert payload["runModesDialogCount"] == 1
    assert payload["legacy"] == {"app": "undefined", "router": "undefined", "dataNode": False}
    assert payload["diagnostics"]["pageErrors"] == []
    assert payload["diagnostics"]["consoleErrors"] == []
    assert payload["diagnostics"]["requestFailures"] == []
    assert not any("chat.send" in frame for frame in payload["diagnostics"]["websocketFrames"])
