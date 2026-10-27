"""WebSocket protocol frame types and constants."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

# Protocol version negotiated during handshake
PROTOCOL_VERSION = 3
#: The oldest protocol version this server still speaks. A client whose
#: ``[minProtocol, maxProtocol]`` range does not include a version in
#: ``[MIN_PROTOCOL_VERSION, PROTOCOL_VERSION]`` is refused with
#: ``PROTOCOL_MISMATCH``.
MIN_PROTOCOL_VERSION = 3

# Payload limits
MAX_PAYLOAD_BYTES = 26_214_400  # 25 MiB
MAX_BUFFERED_BYTES = 52_428_800  # 50 MiB
MAX_PREAUTH_PAYLOAD_BYTES = 65_536  # 64 KiB

# Timing constants
TICK_INTERVAL_MS = 30_000
HEALTH_REFRESH_INTERVAL_MS = 60_000
PREAUTH_TIMEOUT_MS = 10_000
DEDUPE_TTL_MS = 300_000
DEDUPE_MAX_ENTRIES = 1000

# Graceful shutdown WS close code
WS_CLOSE_SERVICE_RESTART = 1012
#: Close code for a frame larger than the stage's payload limit.
WS_CLOSE_MESSAGE_TOO_BIG = 1009
#: Close code for a policy violation (rejected origin, slow consumer).
WS_CLOSE_POLICY_VIOLATION = 1008
#: Close code a client uses when it drops a connection on an event-sequence gap.
WS_CLOSE_SEQ_GAP = 4000

#: Additive features a client may gate on. ``hello-ok.features.capabilities``
#: carries ``sorted(CAPABILITIES)``. Names are kebab-case and never removed
#: once shipped; a client must treat an unknown name as absent.
CAPABILITIES: frozenset[str] = frozenset(
    {
        "approval-events",
        "artifact-tickets",
        "auth-rate-limit",
        "challenge-ts",
        "config-reload",
        "idempotency-keys",
        "inflight-snapshot",
        # `steer` sends join the running turn at its next checkpoint;
        # chat.history `pending_inputs`; chat.abort / sessions.abort `taskId`.
        "mid-turn-steering",
        "rpc-http",
        # /ready + /startupz states, STARTUP_SIDECARS during boot, and
        # `shutdown {reason, restart_expected_ms}` (gateway/readiness.py).
        "supervisor-contract",
        "terminal-replay",
        "tools-invoke",
        "typed-errors",
        "web-push",
    }
)


# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------


class ErrorCode(StrEnum):
    """Closed set of top-level ``error.code`` values.

    New errors use one of the first eight members and put the specific reason
    in ``details.code`` (:class:`ErrorDetailCode`). The remaining members are
    long-standing codes still sent on the wire; they stay so existing clients
    keep matching them. Domain handlers may also send a namespaced code
    (``project.conflict``, ``trading.invalid``); :func:`error_category` maps
    every code the gateway emits to one of the first eight.
    """

    INVALID_REQUEST = "INVALID_REQUEST"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_FOUND = "METHOD_NOT_FOUND"
    UNAVAILABLE = "UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    # Long-standing codes, kept on the wire as they are.
    NOT_LINKED = "NOT_LINKED"
    NOT_PAIRED = "NOT_PAIRED"
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    APPROVAL_NOT_FOUND = "APPROVAL_NOT_FOUND"


class ErrorDetailCode(StrEnum):
    """Closed set of ``error.details.code`` values: the specific reason."""

    PROTOCOL_MISMATCH = "PROTOCOL_MISMATCH"
    AUTH_RATE_LIMITED = "AUTH_RATE_LIMITED"
    REQUEST_START_TIMEOUT = "REQUEST_START_TIMEOUT"
    QUEUE_FULL = "QUEUE_FULL"
    GATEWAY_RESTARTING = "GATEWAY_RESTARTING"
    SLOW_CONSUMER = "SLOW_CONSUMER"
    REQUEST_CONFLICT = "REQUEST_CONFLICT"
    ABORT_INCOMPLETE = "ABORT_INCOMPLETE"
    STARTUP_SIDECARS = "STARTUP_SIDECARS"
    INVALID_FRAME = "INVALID_FRAME"
    INVALID_CONNECT_PARAMS = "INVALID_CONNECT_PARAMS"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    METHOD_NOT_ALLOWED_OVER_HTTP = "METHOD_NOT_ALLOWED_OVER_HTTP"
    # Direct tool calls, inbound hooks and ticketed downloads.
    POLICY_DENIED = "POLICY_DENIED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    FANOUT_LIMIT = "FANOUT_LIMIT"
    INTEGRITY_ERROR = "INTEGRITY_ERROR"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"


#: The canonical categories: what a client may branch on without knowing
#: any domain code.
CATEGORY_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.INVALID_REQUEST,
        ErrorCode.UNAUTHORIZED,
        ErrorCode.FORBIDDEN,
        ErrorCode.NOT_FOUND,
        ErrorCode.METHOD_NOT_FOUND,
        ErrorCode.UNAVAILABLE,
        ErrorCode.RATE_LIMITED,
        ErrorCode.INTERNAL_ERROR,
    }
)

#: Category of every non-category code the gateway sends today. A handler
#: that introduces a new upper-case code must add it here;
#: ``tests/test_gateway/test_error_codes.py`` fails otherwise.
LEGACY_ERROR_CATEGORIES: Mapping[str, ErrorCode] = {
    ErrorCode.NOT_LINKED: ErrorCode.UNAVAILABLE,
    ErrorCode.NOT_PAIRED: ErrorCode.UNAUTHORIZED,
    ErrorCode.AGENT_TIMEOUT: ErrorCode.UNAVAILABLE,
    ErrorCode.APPROVAL_NOT_FOUND: ErrorCode.NOT_FOUND,
    "QUEUE_FULL": ErrorCode.UNAVAILABLE,
    "QUEUE_FULL_DIRTY": ErrorCode.UNAVAILABLE,
    "GATEWAY_RESTARTING": ErrorCode.UNAVAILABLE,
    "RESTART_UNSUPPORTED": ErrorCode.UNAVAILABLE,
    "ACP_SESSION_INVALID": ErrorCode.INVALID_REQUEST,
    "ACP_UNAVAILABLE": ErrorCode.UNAVAILABLE,
    "ACP_ERROR": ErrorCode.INTERNAL_ERROR,
    "INVALID_PARAMS": ErrorCode.INVALID_REQUEST,
    "flush_unavailable": ErrorCode.UNAVAILABLE,
}

#: Upper-case code families a subsystem owns (``ACP_SESSION_CLOSED``,
#: ``ACP_HARNESS_MISSING``); categorized by the reason after the prefix.
LEGACY_ERROR_FAMILIES: tuple[str, ...] = ("ACP_",)

# Reason (the last segment of ``agent.not_found``, or what follows a family
# prefix) → category. Checked in order; the first marker found in the reason
# wins.
_REASON_RULES: tuple[tuple[str, ErrorCode], ...] = (
    ("not_found", ErrorCode.NOT_FOUND),
    ("unknown", ErrorCode.NOT_FOUND),
    ("operator_required", ErrorCode.FORBIDDEN),
    ("immutable", ErrorCode.FORBIDDEN),
    ("not_owner", ErrorCode.FORBIDDEN),
    ("not_allowed", ErrorCode.FORBIDDEN),
    ("forbidden", ErrorCode.FORBIDDEN),
    ("rate_limited", ErrorCode.RATE_LIMITED),
    ("unavailable", ErrorCode.UNAVAILABLE),
    ("unsupported", ErrorCode.UNAVAILABLE),
    ("disabled", ErrorCode.UNAVAILABLE),
    ("missing", ErrorCode.UNAVAILABLE),
    ("capacity", ErrorCode.UNAVAILABLE),
    ("max_concurrent", ErrorCode.UNAVAILABLE),
    ("timeout", ErrorCode.UNAVAILABLE),
    ("invalid", ErrorCode.INVALID_REQUEST),
    ("closed", ErrorCode.INVALID_REQUEST),
    ("not_running", ErrorCode.INVALID_REQUEST),
)


def _reason_category(reason: str) -> ErrorCode | None:
    for marker, category in _REASON_RULES:
        if marker in reason:
            return category
    return None


def error_category(code: str | None) -> ErrorCode | None:
    """The canonical category of any ``error.code`` the gateway emits.

    Category codes map to themselves, long-standing codes through
    :data:`LEGACY_ERROR_CATEGORIES`, a subsystem family code
    (:data:`LEGACY_ERROR_FAMILIES`) and a namespaced domain code
    (``<namespace>.<reason>``) by their reason: ``*.not_found`` is
    ``NOT_FOUND``, ``*.operator_required`` is ``FORBIDDEN``,
    ``*_unsupported`` is ``UNAVAILABLE``. Any other domain refusal
    (``*.conflict``, ``*.exists``) is ``INVALID_REQUEST``; any other family
    code ``INTERNAL_ERROR``. ``None`` for an empty or unknown code.
    """
    if not code:
        return None
    try:
        member = ErrorCode(code)
    except ValueError:
        member = None
    if member is not None and member in CATEGORY_CODES:
        return member
    legacy = LEGACY_ERROR_CATEGORIES.get(code)
    if legacy is not None:
        return legacy
    for prefix in LEGACY_ERROR_FAMILIES:
        if code.startswith(prefix) and len(code) > len(prefix):
            return _reason_category(code[len(prefix) :].lower()) or ErrorCode.INTERNAL_ERROR
    if "." not in code:
        return None
    return _reason_category(code.rsplit(".", 1)[1]) or ErrorCode.INVALID_REQUEST


def protocol_mismatch_details(client_min: int, client_max: int) -> dict[str, Any]:
    """``details`` for a handshake whose protocol range the server cannot meet.

    ``reload_required`` is true when the client is older than the server: a
    web console served from a stale bundle gets a compatible one on reload.
    A client newer than the server needs the server upgraded instead.
    """
    return {
        "code": ErrorDetailCode.PROTOCOL_MISMATCH.value,
        "client_min": client_min,
        "client_max": client_max,
        "server_min": MIN_PROTOCOL_VERSION,
        "server_max": PROTOCOL_VERSION,
        "reload_required": client_max < MIN_PROTOCOL_VERSION,
    }


def negotiate_protocol(client_min: int, client_max: int) -> int | None:
    """The highest version both sides speak, or ``None`` when ranges miss."""
    negotiated = min(client_max, PROTOCOL_VERSION)
    if negotiated < max(client_min, MIN_PROTOCOL_VERSION):
        return None
    return negotiated


# ---------------------------------------------------------------------------
# Client → Server frames
# ---------------------------------------------------------------------------

#: Longest request id / method name accepted in a ``req`` envelope.
MAX_REQ_ID_CHARS = 128
MAX_METHOD_CHARS = 128


class ReqFrame(BaseModel):
    """RPC request frame sent by client."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["req"] = "req"
    id: StrictStr = Field(max_length=MAX_REQ_ID_CHARS)
    method: StrictStr = Field(min_length=1, max_length=MAX_METHOD_CHARS)
    params: Any | None = None


# ---------------------------------------------------------------------------
# Server → Client frames
# ---------------------------------------------------------------------------


class ErrorShape(BaseModel):
    code: str
    message: str
    details: Any | None = None
    retryable: bool | None = None
    retry_after_ms: int | None = None


class ResFrame(BaseModel):
    """RPC response frame sent by server."""

    type: Literal["res"] = "res"
    id: str
    ok: bool
    payload: Any | None = None
    error: ErrorShape | None = None


class StateVersion(BaseModel):
    presence: int = 0
    health: int = 0


class EventFrame(BaseModel):
    """Server-pushed event frame."""

    type: Literal["event"] = "event"
    event: str
    payload: Any | None = None
    meta: dict[str, Any] | None = None
    seq: int | None = None
    state_version: StateVersion | None = None


class PingFrame(BaseModel):
    type: Literal["ping"] = "ping"


class PongFrame(BaseModel):
    type: Literal["pong"] = "pong"


# ---------------------------------------------------------------------------
# Handshake frames
# ---------------------------------------------------------------------------

_CLIENT_FIELD_MAX_CHARS = 256


def _client_field(*aliases: str) -> Any:
    return Field(
        default=None,
        max_length=_CLIENT_FIELD_MAX_CHARS,
        validation_alias=AliasChoices(*aliases),
    )


class ClientInfo(BaseModel):
    """Who is connecting. Informational: admission never depends on it.

    Every field is optional so an older client keeps connecting, and ``name``
    is accepted for ``id`` (the web console sent ``{name: "agentos-web"}``
    before the block was specified). Types are strict: a non-string value is
    a malformed handshake.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: StrictStr | None = _client_field("id", "name")
    display_name: StrictStr | None = _client_field("displayName", "display_name")
    version: StrictStr | None = _client_field("version")
    platform: StrictStr | None = _client_field("platform")
    device_family: StrictStr | None = _client_field("deviceFamily", "device_family")
    model_identifier: StrictStr | None = _client_field("modelIdentifier", "model_identifier")
    mode: StrictStr | None = _client_field("mode")
    instance_id: StrictStr | None = _client_field("instanceId", "instance_id")


#: ``auth`` keys that carry a credential; each must be a string when present.
_AUTH_CREDENTIAL_KEYS: tuple[str, ...] = ("token", "agentToken", "operatorSecret")


class ConnectParams(BaseModel):
    """``params`` of the ``connect`` request (camelCase on the wire)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    min_protocol: StrictInt = Field(default=1, ge=0, alias="minProtocol")
    max_protocol: StrictInt = Field(default=PROTOCOL_VERSION, ge=0, alias="maxProtocol")
    client: ClientInfo | None = None
    caps: list[StrictStr] | None = None
    commands: list[StrictStr] | None = None
    permissions: dict[str, bool] | None = None
    path_env: StrictStr | None = Field(default=None, alias="pathEnv")
    # Validated as a string here; ``resolve_auth`` decides which surfaces
    # admit (an unsupported one is an authentication failure, not a
    # malformed frame).
    client_kind: StrictStr = Field(default="control", alias="clientKind")
    auth: dict[str, Any] | None = None
    locale: StrictStr | None = None
    user_agent: StrictStr | None = Field(default=None, alias="userAgent")

    @model_validator(mode="after")
    def _range_is_ordered(self) -> ConnectParams:
        if self.min_protocol > self.max_protocol:
            raise ValueError("minProtocol must not be greater than maxProtocol")
        return self

    @field_validator("auth")
    @classmethod
    def _credentials_are_strings(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        for key in _AUTH_CREDENTIAL_KEYS:
            if key in value and value[key] is not None and not isinstance(value[key], str):
                raise ValueError(f"auth.{key} must be a string")
        return value


class ServerInfo(BaseModel):
    version: str
    conn_id: str


class FeaturesInfo(BaseModel):
    methods: list[str]
    events: list[str]
    capabilities: list[str] = Field(default_factory=lambda: sorted(CAPABILITIES))


class SnapshotInfo(BaseModel):
    presence: list[Any] = []
    health: Any = None
    state_version: StateVersion = StateVersion()
    uptime_ms: int = 0
    config_path: str | None = None
    state_dir: str | None = None
    auth_mode: str | None = None


class PolicyInfo(BaseModel):
    max_payload: int = MAX_PAYLOAD_BYTES
    max_buffered_bytes: int = MAX_BUFFERED_BYTES
    tick_interval_ms: int = TICK_INTERVAL_MS
    agent_stream_heartbeat_interval_ms: int = 15_000
    agent_stream_idle_timeout_ms: int = 600_000
    webui_stream_idle_grace_ms: int = 630_000
    client_ws_keepalive_timeout_ms: int = 120_000


class HelloOk(BaseModel):
    type: Literal["hello-ok"] = "hello-ok"
    protocol: int
    server: ServerInfo
    features: FeaturesInfo
    snapshot: SnapshotInfo
    policy: PolicyInfo
    auth: dict[str, Any] | None = None


def validation_error_details(code: ErrorDetailCode, exc: Exception) -> dict[str, Any]:
    """Typed ``details`` for a pydantic validation failure: where and why."""
    errors: list[dict[str, Any]] = []
    raw_errors = getattr(exc, "errors", None)
    if callable(raw_errors):
        for item in raw_errors(include_url=False, include_input=False):
            errors.append(
                {
                    "loc": [str(part) for part in item.get("loc", ())],
                    "msg": str(item.get("msg", "")),
                    "type": str(item.get("type", "")),
                }
            )
    return {"code": code.value, "errors": errors[:20]}


# ---------------------------------------------------------------------------
# Legacy error-code constants (aliases of the enum members)
# ---------------------------------------------------------------------------

ERROR_NOT_LINKED = ErrorCode.NOT_LINKED
ERROR_NOT_PAIRED = ErrorCode.NOT_PAIRED
ERROR_AGENT_TIMEOUT = ErrorCode.AGENT_TIMEOUT
ERROR_INVALID_REQUEST = ErrorCode.INVALID_REQUEST
ERROR_APPROVAL_NOT_FOUND = ErrorCode.APPROVAL_NOT_FOUND
ERROR_UNAVAILABLE = ErrorCode.UNAVAILABLE
ERROR_UNAUTHORIZED = ErrorCode.UNAUTHORIZED
ERROR_NOT_FOUND = ErrorCode.NOT_FOUND
ERROR_METHOD_NOT_FOUND = ErrorCode.METHOD_NOT_FOUND
ERROR_FORBIDDEN = ErrorCode.FORBIDDEN
ERROR_RATE_LIMITED = ErrorCode.RATE_LIMITED
ERROR_INTERNAL_ERROR = ErrorCode.INTERNAL_ERROR


def make_error_res(
    req_id: str,
    code: str,
    message: str,
    retryable: bool = False,
    details: Any | None = None,
    retry_after_ms: int | None = None,
) -> ResFrame:
    return ResFrame(
        id=req_id,
        ok=False,
        error=ErrorShape(
            code=str(code),
            message=message,
            retryable=retryable,
            details=details,
            retry_after_ms=retry_after_ms,
        ),
    )


def make_ok_res(req_id: str, payload: Any = None) -> ResFrame:
    return ResFrame(id=req_id, ok=True, payload=payload)


def make_event(
    event: str,
    payload: Any = None,
    seq: int | None = None,
    meta: dict[str, Any] | None = None,
) -> EventFrame:
    return EventFrame(event=event, payload=payload, seq=seq, meta=meta)
