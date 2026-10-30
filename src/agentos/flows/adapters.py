"""Provider seams of the flow engine (protocols, retries, error wording).

:class:`ImageAdapter` turns one ``image`` node call into a provider request;
:class:`TextAdapter` runs the LLM nodes. This package never imports a provider:
the gateway's composition root (``agentos.gateway.flows_wiring``) supplies the
real adapters, tests and the smoke script supply fakes.
"""

from __future__ import annotations

import asyncio
import email.utils
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from agentos.flows.models import ModelEntry
from agentos.flows.types import NodeError

RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 520, 522, 524, 529})
MAX_RETRIES = 3
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0

Sleep = Callable[[float], Awaitable[None]]


@dataclass
class AdapterRequest:
    prompt: str
    images: list[tuple[bytes, str]] = field(default_factory=list)
    n: int = 1
    aspect_ratio: str | None = None
    image_size: str | None = None
    size: str | None = None
    quality: str | None = None
    background: str | None = None
    timeout_s: float = 180.0


@dataclass
class AdapterResult:
    images: list[tuple[bytes, str]] = field(default_factory=list)
    cost_usd: float | None = None
    text: str | None = None


class ImageAdapter(Protocol):
    async def generate(self, entry: ModelEntry, request: AdapterRequest) -> AdapterResult: ...


@dataclass
class TextResult:
    text: str
    cost_usd: float | None = None


class TextAdapter(Protocol):
    async def complete(
        self,
        *,
        task: str,
        system: str,
        prompt: str,
        images: list[tuple[bytes, str]] | None = None,
        session_key: str | None = None,
    ) -> TextResult: ...


# ── errors ────────────────────────────────────────────────────────────────


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _first_sentences(text: str, count: int = 2, limit: int = 300) -> str:
    """The first sentences of a provider message (drops appended support text)."""
    parts = _SENTENCE_END.split(" ".join(text.split()))
    return " ".join(parts[:count])[:limit]


def _provider_message(response: httpx.Response | None) -> str:
    if response is None:
        return ""
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - not JSON: fall back to text
        return _first_sentences(response.text or "")
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return _first_sentences(str(error["message"]))
        if isinstance(error, str):
            return _first_sentences(error)
        if isinstance(body.get("message"), str):
            return _first_sentences(str(body["message"]))
    return ""


def _provider_code(response: httpx.Response | None) -> str:
    try:
        error = response.json().get("error") if response is not None else None
    except Exception:  # noqa: BLE001 - no JSON body, no code
        return ""
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else ""


def classify_http_error(exc: BaseException, *, provider: str = "The provider") -> NodeError:
    """Turn a transport/HTTP exception into a short, human-readable node error.

    *provider* names who answered (``"OpenRouter"``), so a 402 reads
    ``OpenRouter: insufficient credits``.
    """
    from agentos.redact import redact_sensitive_text

    def clean(text: str) -> str:
        return redact_sensitive_text(text) or text

    if isinstance(exc, NodeError):
        return exc
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        detail = clean(_provider_message(exc.response))
        suffix = f": {detail}" if detail else ""
        if status in (401, 403):
            return NodeError(
                "auth", f"{provider}: the API key was rejected (HTTP {status}){suffix}"
            )
        if status == 402 and _provider_code(exc.response) == "budget_exceeded":
            return NodeError("budget", f"{provider}: a spending budget stopped this call{suffix}")
        if status == 402:
            return NodeError("credits", f"{provider}: insufficient credits{suffix}")
        if status == 429:
            return NodeError("rate_limited", f"{provider}: rate limited{suffix}")
        if status >= 500:
            return NodeError("provider_error", f"{provider} failed (HTTP {status}){suffix}")
        return NodeError("provider_rejected", f"{provider} refused the request{suffix}")
    if isinstance(exc, httpx.TimeoutException):
        return NodeError("timeout", f"{provider} did not answer in time")
    if isinstance(exc, httpx.TransportError):
        return NodeError("network", f"Could not reach {provider}: {clean(str(exc))}")
    message = clean(str(exc)) or exc.__class__.__name__
    if message.endswith(" is not set"):
        return NodeError("unavailable", message)
    return NodeError("provider_error", message)


def _retry_after_s(exc: BaseException) -> float | None:
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    raw = exc.response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRY_STATUSES
    return isinstance(exc, httpx.TransportError)


async def with_retries(
    call: Callable[[], Awaitable[Any]],
    *,
    sleep: Sleep = asyncio.sleep,
    max_retries: int = MAX_RETRIES,
) -> Any:
    """Run *call*; retry 408/429/5xx and transport errors with exponential backoff.

    Waits ``1, 2, 4 ... s`` capped at 30 s, or the provider's ``Retry-After``
    (also capped) when it sent one. At most ``max_retries`` retries.
    """
    attempt = 0
    while True:
        try:
            return await call()
        except Exception as exc:
            if attempt >= max_retries or not is_retryable(exc):
                raise
            delay = _retry_after_s(exc)
            if delay is None:
                delay = BACKOFF_BASE_S * (2**attempt)
            await sleep(min(BACKOFF_CAP_S, delay))
            attempt += 1


# ── defaults when nothing is wired ────────────────────────────────────────


class UnwiredImageAdapter:
    """Used when no composition root supplied an adapter: every call is unavailable."""

    async def generate(self, entry: ModelEntry, request: AdapterRequest) -> AdapterResult:
        raise NodeError("unavailable", "No image provider is wired into this process")


class UnwiredTextAdapter:
    async def complete(
        self,
        *,
        task: str,
        system: str,
        prompt: str,
        images: list[tuple[bytes, str]] | None = None,
        session_key: str | None = None,
    ) -> TextResult:
        raise NodeError("unavailable", "No language model is wired into this process")
