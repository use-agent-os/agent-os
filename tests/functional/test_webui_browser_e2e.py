"""Opt-in real-browser smoke tests for the built React Control UI.

Run these public-surface checks with:

    AGENTOS_WEBUI_BROWSER_E2E=1 \
      uv run pytest tests/functional/test_webui_browser_e2e.py -q -s
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
    if os.environ.get("AGENTOS_WEBUI_BROWSER_E2E") != "1":
        pytest.skip("set AGENTOS_WEBUI_BROWSER_E2E=1 to run browser smoke")

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


def _start_gateway(tmp_path: Path, port: int, base_path: str) -> subprocess.Popen[str]:
    server_script = tmp_path / "react_control_smoke_server.py"
    server_script.write_text(
        textwrap.dedent(
            f"""
            import uvicorn

            from agentos.gateway.app import create_gateway_app
            from agentos.gateway.config import AuthConfig, ControlUiConfig, GatewayConfig

            config = GatewayConfig(
                host="127.0.0.1",
                port={port},
                auth=AuthConfig(mode="none"),
                control_ui=ControlUiConfig(base_path={base_path!r}),
            )
            app = create_gateway_app(config)

            if __name__ == "__main__":
                uvicorn.run(app, host="127.0.0.1", port={port}, log_level="warning")
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
    _wait_for_health(port, server)
    return server


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=8)


def _run_browser(
    tmp_path: Path,
    playwright_node_modules: Path,
    *,
    target_url: str,
    expected_base: str,
    heading: str,
) -> dict[str, object]:
    browser_script = tmp_path / "react_control_smoke_browser.js"
    browser_script.write_text(
        textwrap.dedent(
            """
            const { chromium } = require("playwright");

            (async () => {
              const browser = await chromium.launch({ headless: true });
              const page = await browser.newPage();
              const diagnostics = { pageErrors: [], consoleErrors: [], requestFailures: [] };
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

              const response = await page.goto(process.env.TARGET_URL, {
                waitUntil: "domcontentloaded",
                timeout: 30000,
              });
              await page.getByRole("heading", { name: process.env.EXPECTED_HEADING }).waitFor({
                timeout: 15000,
              });

              const base = process.env.EXPECTED_BASE;
              const bootstrap = await page.evaluate(async path => {
                const result = await fetch(`${path}/api/bootstrap`);
                return { status: result.status, body: await result.json() };
              }, base);
              const assetUrls = await page.evaluate(() =>
                performance
                  .getEntriesByType("resource")
                  .map(entry => entry.name)
                  .filter(url => url.includes("/static/dist/assets/"))
              );
              const legacy = await page.evaluate(() => ({
                app: typeof window.App,
                router: typeof window.Router,
                dataNode: document.querySelector("#agentos-data") !== null,
              }));
              const result = {
                status: response ? response.status() : 0,
                title: await page.title(),
                appCount: await page.locator("#app").count(),
                baseHref: await page
                  .locator("base[data-agentos-control-base]")
                  .getAttribute("href"),
                mainNavCount: await page.getByRole("navigation", { name: "Main" }).count(),
                bootstrap,
                assetUrls,
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
    env.update(
        {
            "TARGET_URL": target_url,
            "EXPECTED_BASE": expected_base,
            "EXPECTED_HEADING": heading,
            "NODE_PATH": str(playwright_node_modules),
        }
    )
    result = subprocess.run(
        [_node(), str(browser_script)],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


def _assert_clean_react_surface(payload: dict[str, object]) -> None:
    assert payload["status"] == 200
    assert payload["appCount"] == 1
    assert payload["mainNavCount"] == 1
    assert payload["legacy"] == {"app": "undefined", "router": "undefined", "dataNode": False}
    assert payload["diagnostics"] == {
        "pageErrors": [],
        "consoleErrors": [],
        "requestFailures": [],
    }


def test_react_control_deep_link_loads_public_settings_surface(
    tmp_path: Path,
    playwright_node_modules: Path,
) -> None:
    port = _free_port()
    server = _start_gateway(tmp_path, port, "/control")
    try:
        payload = _run_browser(
            tmp_path,
            playwright_node_modules,
            target_url=f"http://127.0.0.1:{port}/control/settings",
            expected_base="/control",
            heading="Agent settings",
        )
    finally:
        _stop_process(server)

    _assert_clean_react_surface(payload)
    assert payload["title"] == "Agent Settings - AgentOS Control"
    assert payload["baseHref"] == "/control/static/dist/"
    assert payload["bootstrap"]["status"] == 200
    assert payload["bootstrap"]["body"]["base_path"] == "/control"
    assert payload["bootstrap"]["body"]["auth_mode"] == "none"


def test_react_control_custom_base_serves_deep_link_assets_and_bootstrap(
    tmp_path: Path,
    playwright_node_modules: Path,
) -> None:
    port = _free_port()
    server = _start_gateway(tmp_path, port, "/ops")
    try:
        payload = _run_browser(
            tmp_path,
            playwright_node_modules,
            target_url=f"http://127.0.0.1:{port}/ops/health",
            expected_base="/ops",
            heading="Health",
        )
    finally:
        _stop_process(server)

    _assert_clean_react_surface(payload)
    assert payload["title"] == "Health - AgentOS Control"
    assert payload["baseHref"] == "/ops/static/dist/"
    assert payload["bootstrap"]["status"] == 200
    assert payload["bootstrap"]["body"]["base_path"] == "/ops"
    assert payload["bootstrap"]["body"]["auth_mode"] == "none"
    asset_prefix = f"http://127.0.0.1:{port}/ops/static/dist/assets/"
    assert payload["assetUrls"]
    assert all(url.startswith(asset_prefix) for url in payload["assetUrls"])
