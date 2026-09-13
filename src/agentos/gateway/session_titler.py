"""Name a fresh session from its first user message.

Every WebChat session starts life as ``WebChat`` (or a bare short id), which
makes a sidebar of twenty sessions unreadable. This module gives each new
session a short, human title the moment the first message is sent, by asking
the auxiliary model for one. It runs off the turn path: the turn never waits
for it, and a failure only means the placeholder name stays.

Rules that keep it out of the operator's way:

* It only names sessions that still carry a placeholder (``None``,
  ``WebChat`` or the short session id). A name a person typed, or one the
  agent set with ``session_rename``, is never overwritten.
* It runs once per session, keyed on the first send, and never on internal
  run kinds (cron, heartbeat, subagent).
* The title is normalized through :func:`normalize_session_name` like every
  other rename, and broadcast as ``sessions.changed`` so open clients update
  their lists without polling.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from agentos.provider.auxiliary import AuxiliaryError, get_auxiliary_client
from agentos.provider.types import ChatConfig, Message
from agentos.session.naming import normalize_session_name

log = structlog.get_logger()

TASK = "session_title"
PLACEHOLDER_NAMES = frozenset({"webchat", "chat", "new chat", "new session", "untitled"})
# Counted on whitespace, so Vietnamese syllables each count as one: a 6-word
# title in the prompt's sense is often 9 or 10 here.
MAX_TITLE_WORDS = 10
MAX_TITLE_CHARS = 60
MAX_PROMPT_CHARS = 2_000

SYSTEM_PROMPT = (
    "You write titles for chat sessions. Given the user's first message, reply with "
    "ONLY a short title of 3 to 6 words that says what the conversation is about. "
    "Use the same language as the message. No quotes, no trailing punctuation, no "
    "explanation, no prefix like 'Title:'. Never answer the message itself."
)

_QUOTES = "\"'“”‘’«»`"
_PREFIX = re.compile(r"^\s*(title|tiêu đề|chủ đề)\s*[:：\-–]\s*", re.IGNORECASE)

Broadcast = Callable[[str, dict[str, Any]], Awaitable[None]]
ModelHint = tuple[str, str]

# Titling is trivial work: prefer the router's lowest text tier, which an
# operator has already declared to be their fast, cheap model.
_FAST_TIERS = ("c0", "c1")


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def fast_model_hint(config: Any) -> ModelHint:
    """(provider, model) of the router's cheapest text tier, or ("", "").

    Offered to the auxiliary client as a hint, not a decision: an explicit
    ``[auxiliary.tasks.session_title]`` or ``AGENTOS_SESSION_TITLE_*`` still
    outranks it, so the hint is withheld when the scope is pinned by env.
    """
    if os.environ.get("AGENTOS_SESSION_TITLE_MODEL") or os.environ.get(
        "AGENTOS_SESSION_TITLE_PROVIDER"
    ):
        return "", ""
    router = _value(config, "agentos_router")
    tiers = _value(router, "tiers", {})
    if not isinstance(tiers, dict):
        return "", ""
    for name in _FAST_TIERS:
        tier = tiers.get(name)
        model = str(_value(tier, "model", "") or "").strip()
        if tier is None or not model or _value(tier, "image_only", False):
            continue
        provider = str(
            _value(tier, "provider", _value(_value(config, "llm"), "provider", "")) or ""
        ).strip()
        return provider, model
    return "", ""


def is_placeholder_name(name: str | None, session_id: str | None = None) -> bool:
    """True when the session has no name a person chose."""
    if not name:
        return True
    stripped = name.strip()
    if stripped.lower() in PLACEHOLDER_NAMES:
        return True
    sid = (session_id or "").strip()
    return bool(sid) and stripped == sid[:8]


def clean_title(raw: str) -> str | None:
    """Turn model output into a storable title, or ``None`` if it is unusable."""
    text = (raw or "").strip().splitlines()[0] if (raw or "").strip() else ""
    text = _PREFIX.sub("", text).strip().strip(_QUOTES).strip()
    text = text.rstrip(".。!！?？:;,，")
    words = text.split()
    if not words:
        return None
    if len(words) > MAX_TITLE_WORDS:
        text = " ".join(words[:MAX_TITLE_WORDS])
    if len(text) > MAX_TITLE_CHARS:
        text = text[:MAX_TITLE_CHARS].rsplit(" ", 1)[0] or text[:MAX_TITLE_CHARS]
    return normalize_session_name(text)


def fallback_title(message: str) -> str | None:
    """Heuristic title when the model is unavailable: the first clause, trimmed."""
    first = (message or "").strip().splitlines()[0] if (message or "").strip() else ""
    first = re.split(r"[.!?。！？]\s", first, maxsplit=1)[0]
    return clean_title(first)


# A title is a handful of tokens, but a reasoning model spends its output
# budget thinking before it writes anything visible: capped at 32 tokens the
# visible answer never arrived and the text came back empty, and a retry on
# top of a slow first call blew the timeout. The cap is only a ceiling — the
# prompt keeps plain models at 3 to 6 words — so one call with room to think
# beats two.
TITLE_MAX_TOKENS = 512


async def generate_title(
    message: str,
    *,
    session_key: str,
    timeout: float = 30.0,
    hint: ModelHint = ("", ""),
) -> str | None:
    """Ask the auxiliary model for a title; fall back to a heuristic on failure."""
    prompt = (message or "").strip()[:MAX_PROMPT_CHARS]
    if not prompt:
        return None
    try:
        result = await get_auxiliary_client().complete(
            task=TASK,
            messages=[Message(role="user", content=prompt)],
            preferred_provider=hint[0],
            preferred_model=hint[1],
            chat_config=ChatConfig(
                max_tokens=TITLE_MAX_TOKENS, temperature=0.2, system=SYSTEM_PROMPT
            ),
            timeout=timeout,
            session_key=session_key,
        )
        title = clean_title(result.text)
        if title:
            return title
        log.info(
            "session_title.empty",
            session_key=session_key,
            raw=result.text[:80],
            output_tokens=getattr(result, "output_tokens", None),
        )
    except AuxiliaryError as exc:
        log.info("session_title.unavailable", session_key=session_key, error=str(exc))
    except Exception:  # pragma: no cover - defensive: never break a send
        log.exception("session_title.failed", session_key=session_key)
    return fallback_title(prompt)


class SessionTitler:
    """Schedules one background title job per session and applies the result."""

    def __init__(
        self,
        session_manager: Any,
        *,
        broadcast: Broadcast | None = None,
        enabled: bool = True,
        timeout: float = 30.0,
        hint: ModelHint = ("", ""),
    ) -> None:
        self._mgr = session_manager
        self._broadcast = broadcast
        self._enabled = enabled
        self._timeout = timeout
        self._hint = hint
        self._inflight: set[str] = set()
        self._pending: set[asyncio.Task[None]] = set()

    def maybe_schedule(
        self, session_key: str, message: str, *, run_kind: str | None = None
    ) -> bool:
        """Kick off titling if this session still needs a name. Never awaits the model."""
        if not self._enabled or not (message or "").strip():
            return False
        # Only a person's own turn names a session; cron, heartbeat and
        # subagent runs keep whatever name the session already has.
        if run_kind and run_kind not in ("session_turn", "chat", "interactive", "user"):
            return False
        if session_key in self._inflight:
            return False
        self._inflight.add(session_key)
        task = asyncio.create_task(self._run(session_key, message))
        # Keep a strong reference: the loop only weakly holds tasks, and a
        # GC'd task is cancelled silently mid-flight.
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return True

    async def _run(self, session_key: str, message: str) -> None:
        try:
            if not await self._needs_title(session_key):
                return
            title = await generate_title(
                message, session_key=session_key, timeout=self._timeout, hint=self._hint
            )
            if not title:
                return
            # Re-check: the person may have named it while the model was thinking.
            if not await self._needs_title(session_key):
                return
            await self._mgr.update(session_key, display_name=title)
            log.info("session_title.applied", session_key=session_key, title=title)
            if self._broadcast is not None:
                await self._broadcast(session_key, {"display_name": title, "displayName": title})
        except Exception:
            log.exception("session_title.apply_failed", session_key=session_key)
        finally:
            self._inflight.discard(session_key)

    async def _needs_title(self, session_key: str) -> bool:
        getter = getattr(self._mgr, "get_session", None)
        if getter is None:
            return False
        node = await getter(session_key)
        if node is None:
            return False
        return is_placeholder_name(
            getattr(node, "display_name", None), getattr(node, "session_id", None)
        )

    async def drain(self) -> None:
        """Wait for in-flight jobs (tests and shutdown)."""
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)


_titlers: dict[int, SessionTitler] = {}


def titler_for(
    session_manager: Any,
    *,
    broadcast: Broadcast | None = None,
    enabled: bool = True,
    timeout: float = 30.0,
    hint: ModelHint = ("", ""),
) -> SessionTitler:
    """One titler per session manager, created on first use.

    Keyed by identity so tests with their own manager get their own titler,
    and the in-flight set stays meaningful across sends on one gateway.
    """
    key = id(session_manager)
    titler = _titlers.get(key)
    if titler is None:
        titler = SessionTitler(
            session_manager, broadcast=broadcast, enabled=enabled, timeout=timeout, hint=hint
        )
        _titlers[key] = titler
    return titler


def reset_titlers() -> None:
    """Forget every titler (tests)."""
    _titlers.clear()
