"""Opt-in real-browser UI audit of the Control UI (overhaul-control-ui task 10.2).

Boots two real ``agentos gateway run`` processes on throwaway state dirs (the
default ``/control`` base path and a custom one) and drives
``webui_ui_audit.cjs`` through Playwright: every Appendix A destination at
360/768/1280 px in light and dark, axe-core, a keyboard-only pass, reduced
motion, forced colors, deep links with reload, legacy redirects and touch
target sizes. Screenshots and ``results.json`` land in ``AGENTOS_UI_AUDIT_OUT``
(default ``/tmp/agentos-ui-audit``).

Run with (needs ``npm --prefix frontend ci`` and a built Control UI):

    npm --prefix frontend exec -- playwright install chromium
    AGENTOS_WEBUI_UI_AUDIT=1 uv run pytest tests/functional/test_webui_ui_audit.py -q -s
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.webui_browser

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = REPO_ROOT / "frontend"
AUDIT_SCRIPT = Path(__file__).with_name("webui_ui_audit.cjs")
CUSTOM_BASE_PATH = "/ops/agentos"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _node() -> str:
    return "node.exe" if os.name == "nt" else "node"


def _agentos() -> str:
    script = Path(sys.executable).with_name("agentos.exe" if os.name == "nt" else "agentos")
    return str(script)


@pytest.fixture(scope="module")
def frontend_node_modules() -> Path:
    if os.environ.get("AGENTOS_WEBUI_UI_AUDIT") != "1":
        pytest.skip("set AGENTOS_WEBUI_UI_AUDIT=1 to run the Control UI audit")
    for package in ("playwright", "@axe-core/playwright"):
        if not (FRONTEND_DIR / "node_modules" / package / "package.json").is_file():
            pytest.fail(f"{package} is not installed; run `npm --prefix frontend ci` first")
    return FRONTEND_DIR / "node_modules"


def _wait_for_health(port: int, server: subprocess.Popen[str], log: Path) -> None:
    deadline = time.monotonic() + 60.0
    last_error = ""
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise AssertionError(
                f"gateway exited early code={server.returncode}\n"
                f"{log.read_text(encoding='utf-8', errors='replace')}"
            )
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
            if response.status_code == 200 and response.json().get("ok") is True:
                return
        except Exception as exc:  # noqa: BLE001 - included in the timeout assertion.
            last_error = str(exc)
        time.sleep(0.2)
    raise AssertionError(f"gateway did not become healthy: {last_error}")


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _gateway(root: Path, base_path: str) -> Iterator[str]:
    """Run ``agentos gateway run`` on a throwaway state dir; yield the console URL."""
    state = root / "state"
    state.mkdir(parents=True)
    config = state / "config.toml"
    config.write_text(
        f'[auth]\nmode = "none"\n\n[control_ui]\nbase_path = "{base_path}"\n',
        encoding="utf-8",
    )
    port = _free_port()
    env = dict(os.environ, AGENTOS_STATE_DIR=str(state), AGENTOS_LOG_DIR=str(root / "logs"))
    env.pop("AGENTOS_GATEWAY_TOKEN", None)
    log = root / "gateway.log"
    with log.open("w", encoding="utf-8") as sink:
        server = subprocess.Popen(
            [_agentos(), "gateway", "run", "--port", str(port), "--config", str(config)],
            cwd=root,
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_for_health(port, server, log)
            yield f"http://127.0.0.1:{port}{base_path}"
        finally:
            _stop(server)


@pytest.fixture
def default_console(tmp_path: Path, frontend_node_modules: Path) -> Iterator[str]:
    yield from _gateway(tmp_path / "default", "/control")


@pytest.fixture
def custom_console(tmp_path: Path, frontend_node_modules: Path) -> Iterator[str]:
    yield from _gateway(tmp_path / "custom", CUSTOM_BASE_PATH)


def test_control_ui_audit(
    default_console: str,
    custom_console: str,
    frontend_node_modules: Path,
) -> None:
    out_dir = Path(os.environ.get("AGENTOS_UI_AUDIT_OUT", "/tmp/agentos-ui-audit"))
    # AUDIT_ONLY / AUDIT_VIEWPORTS split a long audit into runs with their own results.
    subset = "-".join(
        os.environ.get(name, "").replace(",", "+") or "all"
        for name in ("AUDIT_ONLY", "AUDIT_VIEWPORTS")
    )
    results_path = out_dir / f"results-{subset}.json"
    env = dict(
        os.environ,
        AUDIT_BASE_URL=default_console,
        AUDIT_ALT_BASE_URL=custom_console,
        AUDIT_OUT_DIR=str(out_dir),
        AUDIT_RESULTS=str(results_path),
        NODE_PATH=str(frontend_node_modules),
    )
    result = subprocess.run(
        [_node(), str(AUDIT_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    print(json.dumps(summary, indent=2))
    failures = json.loads(results_path.read_text(encoding="utf-8"))["failures"]
    assert failures == [], json.dumps(failures, indent=2)[:20000]
