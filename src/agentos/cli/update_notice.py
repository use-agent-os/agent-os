"""Passive, gh-style "a new release is available" notice.

Emitted at most once per 24h on RPC-connected commands only (never on offline
paths). Every failure mode is silent — this is a courtesy line, never a reason
to slow down or break a command.

Suppression matrix (any one suppresses):
  * stderr is not a TTY            (piped / captured output)
  * a CI environment variable set  (CI / GITHUB_ACTIONS / …)
  * config ``updates.notify = false``
  * checked within the last 24h    (state file timestamp)
  * ``AGENTOS_NO_UPDATE_NOTICE=1`` escape hatch
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from agentos.paths import state_dir

_CHECK_INTERVAL_S = 24 * 60 * 60
_STATE_FILE = ("update_notice.json",)
# Env vars that mark an automated / non-interactive context.
_CI_ENV_VARS = ("CI", "CONTINUOUS_INTEGRATION", "GITHUB_ACTIONS", "BUILDKITE", "GITLAB_CI")


def notice_state_path() -> Path:
    return state_dir(*_STATE_FILE)


def _stderr_is_tty() -> bool:
    try:
        return bool(sys.stderr.isatty())
    except Exception:  # noqa: BLE001 - defensive; a broken stderr just suppresses
        return False


def _ci_active() -> bool:
    return any(os.environ.get(var, "").strip() for var in _CI_ENV_VARS)


def _read_state(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, last_checked: float, latest: str | None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {"last_checked": last_checked}
        if latest:
            payload["latest"] = latest
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass  # best-effort; a read-only home just means we re-check next time


def _due_for_check(path: Path, now: float) -> bool:
    state = _read_state(path)
    last = state.get("last_checked")
    if not isinstance(last, int | float):
        return True
    return (now - float(last)) >= _CHECK_INTERVAL_S


def _config_notify_enabled(config: object | None) -> bool:
    updates = getattr(config, "updates", None)
    if updates is None:
        return True
    return bool(getattr(updates, "notify", True))


def maybe_emit_update_notice(
    *,
    current_version: str,
    config: object | None = None,
    now: float | None = None,
    force: bool = False,
) -> str | None:
    """Emit the update notice to stderr if all suppression checks pass.

    Returns the message that was emitted (for tests), or ``None`` when
    suppressed. Network access is delegated to :mod:`pypi_client` and fully
    mockable; failures are silent.
    """

    if os.environ.get("AGENTOS_NO_UPDATE_NOTICE", "").strip() == "1":
        return None
    if not force:
        if not _stderr_is_tty():
            return None
        if _ci_active():
            return None
    if not _config_notify_enabled(config):
        return None

    now = time.time() if now is None else now
    path = notice_state_path()
    if not force and not _due_for_check(path, now):
        return None

    from agentos.cli.pypi_client import latest_version
    from agentos.cli.version_utils import is_newer

    latest = latest_version(timeout=2.0)
    # Record the check regardless of outcome so an offline run still throttles.
    _write_state(path, now, latest)
    if not latest or not is_newer(latest, current_version):
        return None

    message = (
        f"A new release of use-agent-os is available: "
        f"{current_version} → {latest}. Run 'agentos upgrade'."
    )
    print(message, file=sys.stderr)
    return message
