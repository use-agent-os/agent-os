"""Issue #2118: a mid-stream Anthropic ``error`` event was silently dropped.

``{"type": "error", "error": {"type": "overloaded_error", ...}}`` is a documented
streaming event, sent *after* generation has begun -- so the pre-stream
``status_code != 200`` check has long since passed and never sees it. No branch
in the dispatch chain matched ``"error"``, and Anthropic closes the connection
afterwards with no ``message_stop`` and no ``[DONE]``, so ``aiter_lines()``
simply ran out and ``_stream()`` returned having yielded **neither** an
ErrorEvent nor a DoneEvent.

Two consequences. The user sees the reply stop mid-sentence with no reason, and
-- because nothing reaches failure classification -- ``ProviderCircuitBreaker``
never learns the provider was overloaded, so later turns keep being routed to
it instead of failing over.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agentos.provider import ChatConfig, DoneEvent, ErrorEvent, Message, TextDeltaEvent
from agentos.provider.anthropic import AnthropicProvider
from agentos.provider.failures import ProviderFailureKind, classify_provider_error

_MESSAGE_START = {
    "type": "message_start",
    "message": {"id": "msg_1", "model": "claude-opus-4-7", "usage": {"input_tokens": 10}},
}
_TEXT_BLOCK = [
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "partial answer"},
    },
]


def _sse_body(events: list[dict]) -> bytes:
    parts = []
    for ev in events:
        parts.append(f"event: {ev['type']}\n".encode())
        parts.append(f"data: {json.dumps(ev)}\n\n".encode())
    return b"".join(parts)


def _collect(monkeypatch: pytest.MonkeyPatch, events: list[dict]) -> list[object]:
    """Run a stream to exhaustion and return every event it yielded."""
    body = _sse_body(events)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("agentos.provider.anthropic.httpx.AsyncClient", patched_async_client)
    provider = AnthropicProvider(api_key="test", model="claude-opus-4-7")

    async def _run() -> list[object]:
        return [
            ev
            async for ev in provider.chat([Message(role="user", content="hi")], config=ChatConfig())
        ]

    return asyncio.run(_run())


def _errors(events: list[object]) -> list[ErrorEvent]:
    return [ev for ev in events if isinstance(ev, ErrorEvent)]


# ── the reported case ───────────────────────────────────────────────────────


def test_an_overloaded_error_mid_stream_yields_an_error_event(monkeypatch) -> None:
    """The stream used to end here having yielded nothing at all."""
    events = _collect(
        monkeypatch,
        [
            _MESSAGE_START,
            *_TEXT_BLOCK,
            {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
        ],
    )

    errors = _errors(events)
    assert len(errors) == 1
    assert "Overloaded" in errors[0].message


def test_the_upstream_error_type_is_carried_as_the_code(monkeypatch) -> None:
    """The code is not cosmetic: it is the string failure classification reads."""
    events = _collect(
        monkeypatch,
        [
            _MESSAGE_START,
            {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
        ],
    )

    assert _errors(events)[0].code == "overloaded_error"


def test_the_error_classifies_as_provider_overloaded(monkeypatch) -> None:
    """The half of this issue that is not visible to the user.

    Yielding *an* error is not enough — ``classify_provider_error`` matches on
    ``"overloaded_error"`` appearing in the joined code/message, and only a
    PROVIDER_OVERLOADED verdict lets the circuit breaker and provider fallback
    learn this provider is unhealthy. A generic code would show the user an
    error and still leave the breaker blind.
    """
    events = _collect(
        monkeypatch,
        [
            _MESSAGE_START,
            {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
        ],
    )
    error = _errors(events)[0]

    kind = classify_provider_error("anthropic", None, error.code, error.message)

    assert kind == ProviderFailureKind.PROVIDER_OVERLOADED


def test_a_rate_limit_error_mid_stream_classifies_as_rate_limited(monkeypatch) -> None:
    """The same path carries the other error types Anthropic documents."""
    events = _collect(
        monkeypatch,
        [
            _MESSAGE_START,
            {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
        ],
    )
    error = _errors(events)[0]

    assert error.code == "rate_limit_error"
    assert classify_provider_error("anthropic", None, error.code, error.message) == (
        ProviderFailureKind.RATE_LIMITED
    )


def test_text_streamed_before_the_error_is_still_delivered(monkeypatch) -> None:
    """The partial answer already sent is not thrown away — the error explains
    where it stopped, it does not replace it."""
    events = _collect(
        monkeypatch,
        [
            _MESSAGE_START,
            *_TEXT_BLOCK,
            {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
        ],
    )

    deltas = [ev for ev in events if isinstance(ev, TextDeltaEvent)]
    assert "".join(d.text for d in deltas) == "partial answer"


def test_no_done_event_follows_the_error(monkeypatch) -> None:
    """A DoneEvent would report the truncated turn as a clean completion."""
    events = _collect(
        monkeypatch,
        [
            _MESSAGE_START,
            *_TEXT_BLOCK,
            {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
        ],
    )

    assert not [ev for ev in events if isinstance(ev, DoneEvent)]


def test_the_stream_stops_at_the_error(monkeypatch) -> None:
    """Anthropic closes the connection after an error, but a proxy replaying a
    buffered tail must not resurrect the turn."""
    events = _collect(
        monkeypatch,
        [
            _MESSAGE_START,
            {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "should not appear"},
            },
            {"type": "message_stop"},
        ],
    )

    assert not [ev for ev in events if isinstance(ev, DoneEvent)]
    assert all("should not appear" not in getattr(ev, "text", "") for ev in events)


# ── malformed error payloads must not replace one failure with another ──────


@pytest.mark.parametrize(
    "error_body",
    [
        {},
        {"message": "no type given"},
        {"type": "", "message": ""},
        None,
        "not-an-object",
        [],
    ],
)
def test_a_malformed_error_payload_still_yields_one_error_event(monkeypatch, error_body) -> None:
    """An upstream that sends a shape we did not expect must still end the turn
    loudly. Raising a TypeError inside the stream loop would turn a reportable
    provider error into an unhandled crash."""
    events = _collect(
        monkeypatch,
        [_MESSAGE_START, {"type": "error", "error": error_body}],
    )

    errors = _errors(events)
    assert len(errors) == 1
    assert errors[0].code
    assert errors[0].message


def test_an_error_event_with_no_error_key_at_all(monkeypatch) -> None:
    events = _collect(monkeypatch, [_MESSAGE_START, {"type": "error"}])

    errors = _errors(events)
    assert len(errors) == 1
    assert errors[0].code == "stream_error"


# ── the happy path is undisturbed ───────────────────────────────────────────


def test_a_clean_stream_still_completes_with_done_and_no_error(monkeypatch) -> None:
    events = _collect(
        monkeypatch,
        [
            _MESSAGE_START,
            *_TEXT_BLOCK,
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
        ],
    )

    assert not _errors(events)
    done = [ev for ev in events if isinstance(ev, DoneEvent)]
    assert len(done) == 1
    assert done[0].stop_reason == "end_turn"


def test_an_unknown_event_type_is_still_ignored(monkeypatch) -> None:
    """Only ``error`` gains a branch; forward compatibility with new event
    types is unchanged."""
    events = _collect(
        monkeypatch,
        [
            _MESSAGE_START,
            {"type": "some_future_event", "detail": "ignore me"},
            {"type": "message_stop"},
        ],
    )

    assert not _errors(events)
    assert len([ev for ev in events if isinstance(ev, DoneEvent)]) == 1
