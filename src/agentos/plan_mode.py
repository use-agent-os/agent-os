"""Plan mode: per-session research-only state with an approval gate.

While plan mode is on for a session, the gateway builds the turn's
ToolContext from a read-only allowlist — the model can explore, search, and
analyze but cannot mutate anything. The model presents its finished plan by
calling ``exit_plan_mode``, which follows the same end-turn-and-resume
contract as ``ask_user`` (see ``agentos.ask_user``): presenting the plan
terminates the turn, and approval arrives out of band (the Web UI plan card
or ``/plan off``), never as an in-band model decision.

This module is deliberately side-effect free (no registry imports) so the
gateway routing layer, the dispatch finalizer, RPC handlers, and text
surfaces can all import it without pulling in tool registration — the same
split as ``agentos.router_control`` and ``agentos.ask_user``.

State is in-memory and per-gateway, like ``RouterControlHoldStore``: a
gateway restart clears plan mode (the session falls back to normal tools,
which the operator can re-enable with ``/plan``).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from agentos.util.bounded_registry import BoundedSessionRegistry

EXIT_PLAN_TOOL_NAME = "exit_plan_mode"

# Status stamped on a successful exit_plan_mode payload. The dispatch
# finalizer keys terminates_turn off this value; renderers key off it too.
PLAN_STATUS_PRESENTED = "plan_presented"

_MAX_PLAN_CHARS = 40_000

# Read/research surface available while plan mode is on. This is an
# ALLOWLIST on purpose (the cron-surface precedent): a tool added later is
# never granted to plan mode by omission. Everything that writes files,
# runs code, sends messages, publishes, schedules, or spawns is absent.
PLAN_MODE_TOOL_ALLOW: frozenset[str] = frozenset(
    {
        # The exit door + the question tool.
        EXIT_PLAN_TOOL_NAME,
        "ask_user",
        # Filesystem reads.
        "read_file",
        "list_dir",
        "glob_search",
        "grep_search",
        "read_spreadsheet",
        "pdf",
        "image",
        # Git reads.
        "git_status",
        "git_diff",
        "git_log",
        # Web research.
        "web_search",
        "web_fetch",
        "http_request",
        "x_search",
        # Memory / session reads.
        "memory_search",
        "memory_get",
        "session_search",
        "session_status",
        "sessions_list",
        "sessions_history",
        "sessions_yield",
        # Skill / environment reads.
        "skill_list",
        "skill_view",
        "skill_search_community",
        "env_list",
        "agents_list",
        # Routing stays available: pinning a tier does not mutate the world.
        "router_control",
    }
)


@dataclass(frozen=True)
class PlanModeState:
    """Plan-mode record for one session."""

    enabled_at: float


class PlanModeStore:
    """In-memory per-session plan-mode flags. No TTL by design: a mode that
    silently expires mid-plan hands write tools back without the user's
    say-so, which is the worst possible failure for this feature. Only an
    explicit disable (approval, ``/plan off``) or a gateway restart clears it.
    """

    def __init__(self) -> None:
        self._sessions: BoundedSessionRegistry[str, PlanModeState] = (
            BoundedSessionRegistry(max_entries=5000, session_scoped=True)
        )

    def enable(self, session_key: str) -> None:
        key = (session_key or "").strip()
        if not key:
            raise ValueError("session_key is required")
        self._sessions.setdefault(key, PlanModeState(enabled_at=time.time()))

    def disable(self, session_key: str) -> bool:
        """Turn plan mode off. Returns True when it was on."""
        return self._sessions.pop((session_key or "").strip(), None) is not None

    def is_enabled(self, session_key: str) -> bool:
        return (session_key or "").strip() in self._sessions

    def get(self, session_key: str) -> PlanModeState | None:
        return self._sessions.get((session_key or "").strip())


_store: PlanModeStore | None = None


def get_plan_mode_store() -> PlanModeStore:
    global _store
    if _store is None:
        _store = PlanModeStore()
    return _store


def reset_plan_mode_store() -> None:
    """Test hook: drop the module singleton."""
    global _store
    _store = None


def validate_plan(raw: object) -> str:
    """Normalize and validate the plan text; raises ValueError on violation."""
    plan = str(raw or "").strip()
    if not plan:
        raise ValueError("'plan' must be a non-empty string describing the plan")
    if len(plan) > _MAX_PLAN_CHARS:
        raise ValueError(f"'plan' exceeds {_MAX_PLAN_CHARS} characters")
    return plan


def build_plan_presented_payload(plan: str) -> dict[str, Any]:
    """Build the tool-result payload for a successfully presented plan."""
    return {
        "status": PLAN_STATUS_PRESENTED,
        "plan": plan,
        "message": (
            "The plan was presented to the user. This turn ends now; wait for "
            "their approval or feedback, which arrives as the next user message. "
            "Plan mode stays on until the user approves."
        ),
    }


def _presented_payload(content: object) -> dict[str, Any] | None:
    if isinstance(content, dict):
        payload: Any = content
    elif isinstance(content, str):
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return None
    else:
        return None
    if isinstance(payload, dict) and payload.get("status") == PLAN_STATUS_PRESENTED:
        return payload
    return None


def exit_plan_payload_terminates_turn(content: object) -> bool:
    """True when a tool-result payload is a successfully presented plan."""
    return _presented_payload(content) is not None


def plan_from_tool_result(tool_name: object, content: object) -> str | None:
    """Extract the presented plan from an exit_plan_mode result, else None."""
    if tool_name != EXIT_PLAN_TOOL_NAME:
        return None
    payload = _presented_payload(content)
    if payload is None:
        return None
    plan = str(payload.get("plan") or "").strip()
    return plan or None


def format_plan_as_text(plan: str) -> str:
    """Render a presented plan for plain-text surfaces (channels, CLI)."""
    return (
        "--- Proposed plan ---\n"
        f"{plan}\n"
        "---------------------\n"
        "Reply with feedback to refine the plan, or send /plan off and tell "
        "me to proceed."
    )
