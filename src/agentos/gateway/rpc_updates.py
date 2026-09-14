"""Update-checking RPC handlers."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from agentos import __version__
from agentos.compat import pypi_client, version_utils
from agentos.gateway.access import CONTROL_ONLY
from agentos.gateway.rpc import RpcContext, get_dispatcher

_d = get_dispatcher()


@_d.method("updates.check", audiences=CONTROL_ONLY)
async def _handle_updates_check(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Check for new release availability, returning version info and status.

    Returns:
        A dict containing:
          - current: The currently running version of agent-os
          - latest: The latest version available on PyPI, or None if check is suppressed/failed
          - status: "up-to-date" | "outdated" | "offline"
    """
    config = getattr(ctx, "config", None)

    # 1. Respect preferences: AGENTOS_NO_UPDATE_NOTICE or updates.notify == False
    if os.environ.get(
        "AGENTOS_NO_UPDATE_NOTICE", ""
    ).strip() == "1" or not pypi_client.config_notify_enabled(config):
        return {
            "current": __version__,
            "latest": None,
            "status": "offline",
        }

    now = time.time()
    path = pypi_client.notice_state_path()

    # 2. Check if we need to contact PyPI or use cached state
    latest: str | None = None
    due = await asyncio.to_thread(pypi_client.due_for_check, path, now, "webui")
    if due:
        latest = await asyncio.to_thread(pypi_client.latest_version, timeout=2.0)
        # Record/cache the result
        await asyncio.to_thread(pypi_client.write_state, path, now, latest, "webui")
    else:
        state = await asyncio.to_thread(pypi_client.read_state, path)
        latest_val = state.get("latest")
        if isinstance(latest_val, str):
            latest = latest_val

    # 3. Determine status
    if latest is None:
        status = "offline"
    elif version_utils.is_newer(latest, __version__):
        status = "outdated"
    else:
        status = "up-to-date"

    return {
        "current": __version__,
        "latest": latest,
        "status": status,
    }
