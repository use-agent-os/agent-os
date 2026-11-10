"""Opt-in real-browser UI audit of the Control UI (overhaul-control-ui task 10.2).

Boots two real ``agentos gateway run`` processes on throwaway state dirs (the
default ``/control`` base path and a custom one) and drives
``webui_ui_audit.cjs`` through Playwright: every Appendix A destination at
360/768/1280 px in light and dark, axe-core, a keyboard-only pass, reduced
motion, forced colors, deep links with reload, legacy redirects and touch
target sizes. Screenshots and ``results.json`` land in ``AGENTOS_UI_AUDIT_OUT``
(default ``agentos-ui-audit`` under the system temp directory).

The default console is seeded before the audit so the data pages are audited
with content, not only in their empty states: sessions with token and cost
totals (Sessions, Usage, Home), a scheduled automation (Automations, Home) and
a pending exec approval (Inbox).

Run with (needs ``npm --prefix frontend ci`` and a built Control UI):

    npm --prefix frontend exec -- playwright install chromium
    AGENTOS_WEBUI_UI_AUDIT=1 uv run pytest tests/functional/test_webui_ui_audit.py -q -s
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
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


#: Sessions seeded into the default console: (key, model, input, output, cost, status).
SEEDED_SESSIONS = [
    ("agent:main:webchat:ui-audit-a", "openai/gpt-5-mini", 182_400, 9_150, 0.4123, "running"),
    ("agent:main:webchat:ui-audit-b", "anthropic/claude-sonnet", 64_020, 3_310, 1.0870, "done"),
    ("agent:main:webchat:ui-audit-c", "openai/gpt-5-mini", 1_250, 410, 0.0031, "done"),
]
SEEDED_JOB = {
    "name": "UI audit morning brief",
    "enabled": True,
    "payloadKind": "reminder",
    "message": "Morning brief from the UI audit.",
    "schedule": {"kind": "cron", "expr": "0 9 * * *"},
}


async def _seed_sessions(state: Path) -> None:
    from agentos.session.manager import SessionManager
    from agentos.session.storage import SessionStorage

    storage = SessionStorage(str(state / "sessions.db"))
    await storage.connect()
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        for key, model, input_tokens, output_tokens, cost, status in SEEDED_SESSIONS:
            await manager.create(key, agent_id="main")
            await manager.update(
                key,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_cost_usd=cost,
                estimated_cost_usd=cost,
                status=status,
            )
    finally:
        await storage.close()


def _seed_approval(state: Path) -> None:
    from agentos.application.approval_queue import ApprovalQueue

    queue = ApprovalQueue(db_path=str(state / "approval_queue.sqlite"))
    try:
        queue.request(
            "exec",
            {
                "command": "ls -la ~/projects",
                "toolName": "bash",
                "agent": "main",
                "sessionKey": SEEDED_SESSIONS[0][0],
            },
            ttl=24 * 3600,
        )
    finally:
        queue.close()


def _seed_offline(home: Path) -> None:
    """Seed what has to exist before the gateway opens its stores.

    ``AGENTOS_STATE_DIR`` names the AgentOS home; the gateway keeps its
    databases in the ``state`` directory inside it.
    """
    stores = home / "state"
    stores.mkdir(parents=True, exist_ok=True)
    asyncio.run(_seed_sessions(stores))
    _seed_approval(stores)


async def _seed_online(port: int) -> dict[str, int]:
    """Seed through the gateway, then read back what the pages will list."""
    from agentos.cli.gateway_client import GatewayClient

    client = GatewayClient()
    await client.connect(f"ws://127.0.0.1:{port}/ws")
    try:
        await client.call("cron.create", SEEDED_JOB)
        jobs = await client.call("cron.list", {})
        usage = await client.call("usage.status", {})
        sessions = await client.call("sessions.list", {"limit": 50})
    finally:
        await client.close()
    job_rows = jobs if isinstance(jobs, list) else (jobs or {}).get("jobs", [])
    approvals = httpx.get(f"http://127.0.0.1:{port}/api/approvals", timeout=5.0).json()
    return {
        "approvals": len(approvals.get("pending", approvals.get("approvals", []))),
        "jobs": len(job_rows),
        "usage_rows": len((usage or {}).get("sessions", [])),
        "sessions": len((sessions or {}).get("sessions", [])),
    }


def _gateway(root: Path, base_path: str, *, seed: bool = False) -> Iterator[str]:
    """Run ``agentos gateway run`` on a throwaway state dir; yield the console URL."""
    state = root / "state"
    state.mkdir(parents=True)
    if seed:
        _seed_offline(state)
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
            if seed:
                seeded = asyncio.run(_seed_online(port))
                expected = len(SEEDED_SESSIONS)
                assert seeded["jobs"] >= 1, seeded
                assert seeded["approvals"] >= 1, seeded
                assert seeded["usage_rows"] >= expected, seeded
                assert seeded["sessions"] >= expected, seeded
            yield f"http://127.0.0.1:{port}{base_path}"
        finally:
            _stop(server)


@pytest.fixture
def default_console(tmp_path: Path, frontend_node_modules: Path) -> Iterator[str]:
    yield from _gateway(tmp_path / "default", "/control", seed=True)


@pytest.fixture
def custom_console(tmp_path: Path, frontend_node_modules: Path) -> Iterator[str]:
    yield from _gateway(tmp_path / "custom", CUSTOM_BASE_PATH)


def test_control_ui_audit(
    default_console: str,
    custom_console: str,
    frontend_node_modules: Path,
) -> None:
    out_dir = Path(
        os.environ.get("AGENTOS_UI_AUDIT_OUT") or Path(tempfile.gettempdir()) / "agentos-ui-audit"
    )
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
