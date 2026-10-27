"""Check a tool call's arguments against the tool's schema before it runs.

Dispatch used to hand the model's arguments straight to the handler. A wrong
type then surfaced as whatever the handler happened to raise — usually a
generic "wrong type" line that did not say which argument was wrong — and a
model that wrote ``"5"`` for an integer or ``"true"`` for a boolean failed
outright on a call whose meaning was never in doubt.

:func:`admit_tool_arguments` runs once per call, before hooks, policy and
approvals see it. It validates the arguments against the subset of JSON Schema
tool schemas actually use (``type``, ``properties``, ``required``, ``items``,
``enum``, ``additionalProperties``, ``anyOf``/``oneOf``), and repairs only
the cases where the intended value is unambiguous and nothing is lost:

* a numeric string where the schema wants an integer or a number;
* an integral float (``5.0``) where it wants an integer;
* ``"true"`` / ``"false"`` where it wants a boolean;
* a JSON-encoded string where it wants an array or an object.

Nothing is ever coerced into ``null``, and arguments the schema does not
mention are passed through untouched (rejected only where the schema says
``additionalProperties: false``). An optional argument sent as ``null`` is
left alone too: handlers treat it as "not given", and models send it often.

Keywords outside that subset (``$ref``, ``allOf``, ``format``, bounds, ...)
are not enforced; the handler still owns those. A schema that is not usable at
all — not a mapping, properties that are not a mapping — skips validation, so
a tool with a loose or odd schema (some MCP servers) behaves exactly as before.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from agentos.tools.types import SafeToolError, ToolSpec

__all__ = [
    "ArgumentAdmission",
    "ArgumentIssue",
    "ToolArgumentsRejectedError",
    "admit_tool_arguments",
    "tool_argument_schema",
]

#: ``error_class`` of the envelope a rejected call returns.
REJECTED_ERROR_CLASS: Final[str] = "ToolArgumentsRejected"
#: ``execution_status.reason`` of a rejected call.
REJECTED_REASON: Final[str] = "invalid_arguments"

_JSON_TYPES: Final[frozenset[str]] = frozenset(
    {"string", "integer", "number", "boolean", "object", "array", "null"}
)
_MAX_DEPTH: Final[int] = 32
_MAX_REPORTED_ISSUES: Final[int] = 8
_PREVIEW_CHARS: Final[int] = 40
_INTEGER_TEXT_RE = re.compile(r"[+-]?\d{1,30}(?:\.0+)?")
_NUMBER_TEXT_RE = re.compile(r"[+-]?(?:\d{1,30}(?:\.\d{0,30})?|\.\d{1,30})(?:[eE][+-]?\d{1,3})?")
# Beyond this an integral float is no longer guaranteed to be exact.
_EXACT_FLOAT_INT_LIMIT: Final[float] = float(2**53)


@dataclass(frozen=True)
class ArgumentIssue:
    """One argument that does not match the schema."""

    path: str
    message: str

    def render(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass(frozen=True)
class ArgumentAdmission:
    """Outcome of checking one call's arguments."""

    arguments: dict[str, Any]
    # Paths whose value was rewritten by a lossless coercion.
    coerced: tuple[str, ...] = ()
    issues: tuple[ArgumentIssue, ...] = ()
    # False when the tool's schema could not be used and nothing was checked.
    validated: bool = True

    @property
    def ok(self) -> bool:
        return not self.issues


class ToolArgumentsRejectedError(SafeToolError):
    """A tool call whose arguments do not match the tool's schema."""

    def __init__(self, tool_name: str, issues: Sequence[ArgumentIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__(render_rejection(tool_name, self.issues))


def render_rejection(tool_name: str, issues: Sequence[ArgumentIssue]) -> str:
    """The model-facing message for a rejected call: every failing path, named."""

    shown = [issue.render() for issue in issues[:_MAX_REPORTED_ISSUES]]
    hidden = len(issues) - len(shown)
    if hidden > 0:
        shown.append(f"and {hidden} more")
    return (
        f"Invalid arguments for {tool_name}: {'; '.join(shown)}. "
        "The tool did not run. Fix these arguments to match the tool schema and call it again."
    )


def tool_argument_schema(spec: ToolSpec) -> dict[str, Any] | None:
    """The object schema a call to ``spec`` is checked against, or None if unusable.

    ``ToolSpec.parameters`` is normally the ``properties`` mapping; some tools
    register a whole object schema there instead. Both shapes are accepted.
    """

    parameters = spec.parameters
    if not isinstance(parameters, Mapping):
        return None
    required: list[str] = []
    extra: dict[str, Any] = {}
    if parameters.get("type") == "object" and "properties" in parameters:
        properties = parameters.get("properties")
        nested_required = parameters.get("required")
        if isinstance(nested_required, list):
            required.extend(name for name in nested_required if isinstance(name, str))
        if "additionalProperties" in parameters:
            extra["additionalProperties"] = parameters["additionalProperties"]
    else:
        properties = parameters
    if not isinstance(properties, Mapping):
        return None
    spec_required = spec.required
    if spec_required is not None and not isinstance(spec_required, list | tuple):
        return None
    for name in spec_required or ():
        if isinstance(name, str) and name not in required:
            required.append(name)
    return {"type": "object", "properties": properties, "required": required, **extra}


def admit_tool_arguments(spec: ToolSpec, arguments: Mapping[str, Any]) -> ArgumentAdmission:
    """Validate ``arguments`` against ``spec``'s schema, applying safe coercions."""

    schema = tool_argument_schema(spec)
    if schema is None or not isinstance(arguments, Mapping):
        return ArgumentAdmission(arguments=dict(arguments or {}), validated=False)
    outcome = _check(dict(arguments), schema, "", depth=0, optional=False)
    value = outcome.value if isinstance(outcome.value, dict) else dict(arguments)
    return ArgumentAdmission(
        arguments=value,
        coerced=tuple(outcome.coerced),
        issues=tuple(outcome.issues),
    )


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


@dataclass
class _Outcome:
    value: Any
    issues: list[ArgumentIssue] = field(default_factory=list)
    coerced: list[str] = field(default_factory=list)

    def absorb(self, other: _Outcome) -> Any:
        self.issues.extend(other.issues)
        self.coerced.extend(other.coerced)
        return other.value


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list | tuple):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return type(value).__name__


def _preview(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) > _PREVIEW_CHARS:
        text = text[: _PREVIEW_CHARS - 3] + "..."
    return text


def _describe(value: Any) -> str:
    kind = _json_type(value)
    if kind in {"object", "array", "null"}:
        return kind
    return f"{kind} {_preview(value)}"


def _matches(value: Any, kind: str) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "object":
        return isinstance(value, Mapping)
    if kind == "array":
        return isinstance(value, list | tuple)
    if kind == "null":
        return value is None
    return False


_NOT_COERCED: Final[object] = object()


def _coerce(value: Any, kind: str) -> Any:
    """Return ``value`` losslessly converted to ``kind``, or ``_NOT_COERCED``."""

    if kind == "integer":
        if isinstance(value, float) and value.is_integer():
            if abs(value) <= _EXACT_FLOAT_INT_LIMIT:
                return int(value)
            return _NOT_COERCED
        if isinstance(value, str):
            text = value.strip()
            if _INTEGER_TEXT_RE.fullmatch(text):
                return int(text.split(".", 1)[0])
        return _NOT_COERCED
    if kind == "number":
        if isinstance(value, str):
            text = value.strip()
            if not _NUMBER_TEXT_RE.fullmatch(text):
                return _NOT_COERCED
            if _INTEGER_TEXT_RE.fullmatch(text) and "." not in text:
                return int(text)
            number = float(text)
            return number if math.isfinite(number) else _NOT_COERCED
        return _NOT_COERCED
    if kind == "boolean":
        if isinstance(value, str):
            text = value.strip().lower()
            if text == "true":
                return True
            if text == "false":
                return False
        return _NOT_COERCED
    if kind in {"array", "object"}:
        if not isinstance(value, str):
            return _NOT_COERCED
        text = value.strip()
        if not text.startswith("[" if kind == "array" else "{"):
            return _NOT_COERCED
        try:
            decoded = json.loads(text)
        except ValueError:
            return _NOT_COERCED
        return decoded if _matches(decoded, kind) else _NOT_COERCED
    # Never coerce into a string (that would hide a structural mistake) or into
    # null (that would turn a wrong value into "not given").
    return _NOT_COERCED


def _declared_types(schema: Mapping[str, Any]) -> list[str] | None:
    """The schema's ``type`` as a list, or None when it does not constrain the type."""

    raw = schema.get("type")
    if isinstance(raw, str):
        kinds = [raw]
    elif isinstance(raw, list) and raw and all(isinstance(item, str) for item in raw):
        kinds = list(raw)
    else:
        return None
    if any(kind not in _JSON_TYPES for kind in kinds):
        return None
    return kinds


def _join_path(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _enum_contains(options: list[Any], value: Any) -> bool:
    # ``True == 1`` in Python; an enum of integers must not admit a boolean.
    return any(option == value and type(option) is type(value) for option in options) or any(
        option == value
        and isinstance(option, int | float)
        and isinstance(value, int | float)
        and not isinstance(option, bool)
        and not isinstance(value, bool)
        for option in options
    )


def _check(
    value: Any,
    schema: Any,
    path: str,
    *,
    depth: int,
    optional: bool,
) -> _Outcome:
    outcome = _Outcome(value=value)
    if depth > _MAX_DEPTH or not isinstance(schema, Mapping) or "$ref" in schema:
        return outcome
    label = path or "arguments"

    branches = schema.get("anyOf", schema.get("oneOf"))
    if isinstance(branches, list) and branches and all(isinstance(b, Mapping) for b in branches):
        union = _check_union(value, branches, path, depth=depth, optional=optional)
        value = outcome.absorb(union)
        outcome.value = value
        if union.issues:
            return outcome

    kinds = _declared_types(schema)
    selected: str | None = None
    if value is None:
        if kinds is not None and "null" not in kinds and not optional:
            outcome.issues.append(ArgumentIssue(label, f"expected {' or '.join(kinds)}, got null"))
        else:
            _check_enum(outcome, schema, value, label)
        return outcome
    if kinds is not None:
        selected = next((kind for kind in kinds if _matches(value, kind)), None)
        if selected is None:
            for kind in kinds:
                converted = _coerce(value, kind)
                if converted is not _NOT_COERCED:
                    value = converted
                    outcome.value = value
                    outcome.coerced.append(label)
                    selected = kind
                    break
        if selected is None:
            outcome.issues.append(
                ArgumentIssue(label, f"expected {' or '.join(kinds)}, got {_describe(value)}")
            )
            return outcome

    if isinstance(value, Mapping) and selected in {None, "object"}:
        outcome.value = _check_object(outcome, value, schema, path, depth=depth)
    elif isinstance(value, list | tuple) and selected in {None, "array"}:
        outcome.value = _check_array(outcome, value, schema, path, depth=depth)
    _check_enum(outcome, schema, outcome.value, label)
    return outcome


def _check_enum(outcome: _Outcome, schema: Mapping[str, Any], value: Any, label: str) -> None:
    options = schema.get("enum")
    if not isinstance(options, list) or not options:
        return
    if _enum_contains(options, value):
        return
    allowed = ", ".join(_preview(option) for option in options[:12])
    if len(options) > 12:
        allowed += ", ..."
    outcome.issues.append(
        ArgumentIssue(label, f"must be one of [{allowed}], got {_describe(value)}")
    )


def _check_object(
    outcome: _Outcome,
    value: Mapping[str, Any],
    schema: Mapping[str, Any],
    path: str,
    *,
    depth: int,
) -> dict[str, Any]:
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        properties = {}
    raw_required = schema.get("required")
    required = (
        [name for name in raw_required if isinstance(name, str)]
        if isinstance(raw_required, list)
        else []
    )
    extra_schema = schema.get("additionalProperties")
    checked: dict[str, Any] = {}
    for key, item in value.items():
        key_path = _join_path(path, str(key))
        if key in properties:
            checked[key] = outcome.absorb(
                _check(
                    item,
                    properties[key],
                    key_path,
                    depth=depth + 1,
                    optional=key not in required,
                )
            )
        elif extra_schema is False:
            outcome.issues.append(ArgumentIssue(key_path, "unexpected argument"))
            checked[key] = item
        elif isinstance(extra_schema, Mapping):
            checked[key] = outcome.absorb(
                _check(item, extra_schema, key_path, depth=depth + 1, optional=True)
            )
        else:
            checked[key] = item
    for name in required:
        if name in value:
            continue
        expected = ""
        sub_schema = properties.get(name)
        if isinstance(sub_schema, Mapping):
            kinds = _declared_types(sub_schema)
            if kinds:
                expected = f" (expected {' or '.join(kinds)})"
        outcome.issues.append(
            ArgumentIssue(_join_path(path, name), f"required argument is missing{expected}")
        )
    return checked


def _check_array(
    outcome: _Outcome,
    value: Sequence[Any],
    schema: Mapping[str, Any],
    path: str,
    *,
    depth: int,
) -> Any:
    items = schema.get("items")
    if not isinstance(items, Mapping):
        return value
    label = path or "arguments"
    return [
        outcome.absorb(_check(item, items, f"{label}[{index}]", depth=depth + 1, optional=False))
        for index, item in enumerate(value)
    ]


def _check_union(
    value: Any,
    branches: list[Mapping[str, Any]],
    path: str,
    *,
    depth: int,
    optional: bool,
) -> _Outcome:
    """Pick the branch ``value`` fits as-is, else the first it fits after coercion."""

    attempts = [
        _check(value, branch, path, depth=depth + 1, optional=optional) for branch in branches
    ]
    for attempt in attempts:
        if not attempt.issues and not attempt.coerced:
            return attempt
    for attempt in attempts:
        if not attempt.issues:
            return attempt
    kinds: list[str] = []
    for branch in branches:
        for kind in _declared_types(branch) or ():
            if kind not in kinds:
                kinds.append(kind)
    label = path or "arguments"
    if kinds and all(all(issue.path == label for issue in attempt.issues) for attempt in attempts):
        return _Outcome(
            value=value,
            issues=[ArgumentIssue(label, f"expected {' or '.join(kinds)}, got {_describe(value)}")],
        )
    best = min(attempts, key=lambda attempt: len(attempt.issues))
    return best
