"""Near-miss tool names resolve; arguments are validated and safely coerced."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agentos.tool_boundary import ToolCall
from agentos.tools.argument_admission import admit_tool_arguments, tool_argument_schema
from agentos.tools.deferred import DeferredToolSurface
from agentos.tools.dispatch import build_tool_handler, preflight_tool_call
from agentos.tools.registry import ToolRegistry
from agentos.tools.tool_name_resolution import resolve_tool_name
from agentos.tools.types import ToolContext, ToolSpec


def _spec(parameters: dict[str, Any], required: list[str] | None = None) -> ToolSpec:
    return ToolSpec(
        name="probe",
        description="Probe tool.",
        parameters=parameters,
        required=required or [],
    )


async def _echo(**kwargs: Any) -> str:
    return json.dumps(kwargs, sort_keys=True)


def _name_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for name in (
        "read_file",
        "web_search",
        "write_file",
        "fetch_page",
        "Fetch_Page",
        "mcp_github_search",
        "mcp_search",
    ):
        registry.register(ToolSpec(name=name, description=name, parameters={}), _echo)
    return registry


# ---------------------------------------------------------------------------
# Name resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("functions.read_file", "read_file"),
        ("tools.read_file", "read_file"),
        ("Functions.read_file", "read_file"),
        ("read_file:0", "read_file"),
        ("read_file:12", "read_file"),
        ("Read_File", "read_file"),
        ("READ_FILE", "read_file"),
        ("functions.Read_File:3", "read_file"),
        ("web/search", "web_search"),
        ("Web/Search", "web_search"),
        ("  read_file  ", "read_file"),
        # An MCP name wrapped in a namespace or counter is still its exact name.
        ("functions.mcp_github_search", "mcp_github_search"),
        ("mcp_github_search:0", "mcp_github_search"),
        # Ambiguous: two tools differ only by case.
        ("FETCH_PAGE", None),
        ("functions.fetch_PAGE", None),
        # MCP names are never rewritten loosely, nor reached from outside.
        ("MCP_GitHub_Search", None),
        ("mcp/github/search", None),
        ("Mcp_Search", None),
        ("github_search", None),
        ("search", None),
        # Not near enough to anything.
        ("read", None),
        ("read_files", None),
        ("functions.", None),
        (":0", None),
        ("", None),
    ],
)
def test_name_resolution_table(requested: str, expected: str | None) -> None:
    resolution = resolve_tool_name(requested, _name_registry(), ToolContext())

    assert (resolution.resolved if resolution else None) == expected


def test_name_resolution_never_lands_on_a_tool_the_caller_cannot_see() -> None:
    ctx = ToolContext(denied_tools={"write_file"})

    assert resolve_tool_name("Write_File", _name_registry(), ctx) is None
    assert resolve_tool_name("functions.write_file", _name_registry(), ctx) is None
    resolution = resolve_tool_name("Read_File", _name_registry(), ctx)
    assert resolution is not None
    assert resolution.rules == ("loose_match",)


async def test_dispatch_runs_the_resolved_tool_under_its_registered_name() -> None:
    handler = build_tool_handler(_name_registry(), ToolContext())

    result = await handler(
        ToolCall(tool_use_id="n1", tool_name="functions.Read_File:0", arguments={"x": 1})
    )

    assert result.is_error is False
    assert result.tool_name == "read_file"
    assert json.loads(result.content) == {"x": 1}


@pytest.mark.parametrize("requested", ["FETCH_PAGE", "MCP_GitHub_Search", "github_search"])
async def test_dispatch_keeps_tool_not_found_when_resolution_declines(requested: str) -> None:
    handler = build_tool_handler(_name_registry(), ToolContext())

    result = await handler(ToolCall(tool_use_id="n2", tool_name=requested, arguments={}))

    payload = json.loads(result.content)
    assert result.is_error is True
    assert payload["error_class"] == "ToolNotFound"
    assert payload["tool"] == requested


async def test_near_miss_onto_an_unloaded_deferred_tool_still_asks_to_load_it() -> None:
    calls = 0

    async def _voice(**_kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        return "spoken"

    registry = ToolRegistry()
    registry.register(ToolSpec(name="tool_search", description="Search.", parameters={}), _echo)
    registry.register(ToolSpec(name="read_file", description="Read.", parameters={}), _echo)
    registry.register(
        ToolSpec(
            name="voice_maker",
            description="Speak.",
            parameters={"text": {"type": "string"}},
            defer_loading=True,
        ),
        _voice,
    )
    ctx = ToolContext(session_key="agent:main:admission-deferred")
    surface = DeferredToolSurface.build(registry, ctx)
    assert surface is not None
    handler = surface.guard(build_tool_handler(registry, ctx))

    result = await handler(
        ToolCall(tool_use_id="d1", tool_name="functions.Voice_Maker", arguments={"text": "hi"})
    )

    payload = json.loads(result.content)
    assert payload["error_class"] == "ToolNotLoaded"
    assert "voice_maker" in payload["user_message"]
    assert calls == 0


# ---------------------------------------------------------------------------
# Argument coercion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("schema", "value", "expected"),
    [
        ({"type": "integer"}, "5", 5),
        ({"type": "integer"}, " -3 ", -3),
        ({"type": "integer"}, "+7", 7),
        ({"type": "integer"}, "5.0", 5),
        ({"type": "integer"}, 5.0, 5),
        ({"type": "number"}, "2.5", 2.5),
        ({"type": "number"}, "3", 3),
        ({"type": "number"}, "1e3", 1000.0),
        ({"type": "boolean"}, "true", True),
        ({"type": "boolean"}, "False", False),
        ({"type": "array", "items": {"type": "string"}}, '["a", "b"]', ["a", "b"]),
        ({"type": "array", "items": {"type": "integer"}}, ["1", 2], [1, 2]),
        ({"type": "object"}, '{"k": "v"}', {"k": "v"}),
        # A value that already fits one declared type is never rewritten.
        ({"type": ["string", "integer"]}, "5", "5"),
        ({"type": "number"}, 4, 4),
        ({"anyOf": [{"type": "integer"}, {"type": "null"}]}, "7", 7),
        ({"anyOf": [{"type": "string"}, {"type": "integer"}]}, "7", "7"),
        ({"oneOf": [{"type": "boolean"}, {"type": "null"}]}, None, None),
    ],
)
def test_coercion_table(schema: dict[str, Any], value: Any, expected: Any) -> None:
    admission = admit_tool_arguments(_spec({"field": schema}), {"field": value})

    assert admission.ok, admission.issues
    assert admission.arguments == {"field": expected}
    assert type(admission.arguments["field"]) is type(expected)


@pytest.mark.parametrize(
    ("schema", "value", "message"),
    [
        ({"type": "integer"}, "5.5", 'expected integer, got string "5.5"'),
        ({"type": "integer"}, "ten", 'expected integer, got string "ten"'),
        ({"type": "integer"}, 2.5, "expected integer, got number 2.5"),
        ({"type": "integer"}, True, "expected integer, got boolean true"),
        ({"type": "number"}, "nan", 'expected number, got string "nan"'),
        ({"type": "number"}, "1e999", 'expected number, got string "1e999"'),
        ({"type": "boolean"}, "yes", 'expected boolean, got string "yes"'),
        ({"type": "boolean"}, 1, "expected boolean, got integer 1"),
        ({"type": "array"}, "a,b", 'expected array, got string "a,b"'),
        ({"type": "array"}, '{"a": 1}', "expected array, got string"),
        ({"type": "object"}, "[1]", 'expected object, got string "[1]"'),
        ({"type": "string"}, 5, "expected string, got integer 5"),
        ({"type": "string"}, {"a": 1}, "expected string, got object"),
        ({"type": "string", "enum": ["a", "b"]}, "c", 'must be one of ["a", "b"], got string "c"'),
        ({"type": "integer", "enum": [1, 2]}, "3", "must be one of [1, 2], got integer 3"),
        ({"anyOf": [{"type": "integer"}, {"type": "null"}]}, "x", "expected integer or null"),
    ],
)
def test_values_that_cannot_be_coerced_are_rejected(
    schema: dict[str, Any], value: Any, message: str
) -> None:
    admission = admit_tool_arguments(_spec({"field": schema}), {"field": value})

    assert not admission.ok
    assert [issue.path for issue in admission.issues] == ["field"]
    assert message in admission.issues[0].message


def test_null_is_never_produced_and_only_passes_where_allowed() -> None:
    spec = _spec(
        {"path": {"type": "string"}, "offset": {"type": "integer"}},
        required=["path"],
    )

    optional_null = admit_tool_arguments(spec, {"path": "a.txt", "offset": None})
    required_null = admit_tool_arguments(spec, {"path": None})

    assert optional_null.ok
    assert optional_null.arguments == {"path": "a.txt", "offset": None}
    assert [issue.render() for issue in required_null.issues] == ["path: expected string, got null"]
    for text in ("null", "None", ""):
        coerced = admit_tool_arguments(spec, {"path": "a", "offset": text})
        assert not coerced.ok


def test_unknown_arguments_are_kept_unless_the_schema_forbids_them() -> None:
    loose = _spec({"path": {"type": "string"}})
    strict = _spec(
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        }
    )

    kept = admit_tool_arguments(loose, {"path": "a", "extra": "kept", "n": "5"})
    refused = admit_tool_arguments(strict, {"path": "a", "extra": "x"})

    assert kept.ok
    assert kept.arguments == {"path": "a", "extra": "kept", "n": "5"}
    assert [issue.render() for issue in refused.issues] == ["extra: unexpected argument"]


def test_nested_paths_are_named_and_coerced() -> None:
    spec = _spec(
        {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}, "tag": {"type": "string"}},
                    "required": ["tag"],
                },
            },
            "env": {"type": "object", "additionalProperties": {"type": "string"}},
        }
    )

    admission = admit_tool_arguments(
        spec,
        {
            "items": [{"count": "2", "tag": "a"}, {"count": "x"}],
            "env": {"PORT": 8080, "HOME": "/tmp"},
        },
    )

    assert admission.coerced == ("items[0].count",)
    assert [issue.render() for issue in admission.issues] == [
        'items[1].count: expected integer, got string "x"',
        "items[1].tag: required argument is missing (expected string)",
        "env.PORT: expected string, got integer 8080",
    ]


@pytest.mark.parametrize(
    "parameters",
    [
        {"type": "object", "properties": "not-a-mapping"},
        {"type": "object", "properties": ["path"]},
    ],
)
def test_unusable_schema_skips_validation(parameters: dict[str, Any]) -> None:
    spec = _spec(parameters)

    admission = admit_tool_arguments(spec, {"path": 5, "anything": "goes"})

    assert tool_argument_schema(spec) is None
    assert admission.validated is False
    assert admission.ok
    assert admission.arguments == {"path": 5, "anything": "goes"}


def test_keywords_outside_the_subset_are_not_enforced() -> None:
    spec = _spec(
        {
            "ref": {"$ref": "#/definitions/Thing"},
            "both": {"allOf": [{"type": "string"}]},
            "custom": {"type": "uuid"},
            "free": {},
            "flag": True,
        }
    )

    admission = admit_tool_arguments(
        spec, {"ref": 1, "both": 2, "custom": 3, "free": [4], "flag": "x"}
    )

    assert admission.ok
    assert admission.coerced == ()


def test_full_object_schema_and_spec_required_are_both_honoured() -> None:
    spec = ToolSpec(
        name="probe",
        description="Probe.",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
            "required": ["a"],
        },
        required=["b"],
    )

    admission = admit_tool_arguments(spec, {})

    assert [issue.path for issue in admission.issues] == ["a", "b"]


# ---------------------------------------------------------------------------
# Dispatch wiring
# ---------------------------------------------------------------------------


def _typed_registry(calls: list[dict[str, Any]]) -> ToolRegistry:
    async def _read(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "read ok"

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="read_file",
            description="Read a file.",
            parameters={
                "path": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
                "raw": {"type": "boolean"},
            },
            required=["path"],
        ),
        _read,
    )
    return registry


async def test_coerced_arguments_reach_the_handler() -> None:
    calls: list[dict[str, Any]] = []
    handler = build_tool_handler(_typed_registry(calls), ToolContext())

    result = await handler(
        ToolCall(
            tool_use_id="c1",
            tool_name="Read_File",
            arguments={"path": "a.txt", "offset": "10", "raw": "true"},
        )
    )

    assert result.is_error is False
    assert calls == [{"path": "a.txt", "offset": 10, "raw": True}]


async def test_rejected_call_names_every_failing_path_and_never_runs() -> None:
    calls: list[dict[str, Any]] = []
    handler = build_tool_handler(_typed_registry(calls), ToolContext())

    result = await handler(
        ToolCall(
            tool_use_id="r1",
            tool_name="read_file",
            arguments={"offset": "ten", "limit": 3.5},
        )
    )

    payload = json.loads(result.content)
    assert calls == []
    assert result.is_error is True
    assert result.execution_status is not None
    assert result.execution_status["reason"] == "invalid_arguments"
    assert payload["error_class"] == "ToolArgumentsRejected"
    assert payload["retry_allowed"] is False
    message = payload["user_message"]
    assert message.startswith("Invalid arguments for read_file: ")
    assert 'offset: expected integer, got string "ten"' in message
    assert "limit: expected integer, got number 3.5" in message
    assert "path: required argument is missing (expected string)" in message


async def test_rejection_runs_before_hooks_and_policy() -> None:
    seen: list[str] = []

    class _Hook:
        name = "probe"

        def before_tool(self, call: Any) -> None:
            seen.append("before")

        def after_tool(self, call: Any, outcome: Any) -> None:
            seen.append("after")

    calls: list[dict[str, Any]] = []
    handler = build_tool_handler(_typed_registry(calls), ToolContext(), tool_hooks=[_Hook()])

    result = await handler(ToolCall(tool_use_id="h1", tool_name="read_file", arguments={}))

    assert result.is_error is True
    assert seen == []
    assert calls == []


async def test_preflight_applies_resolution_and_admission() -> None:
    registry = _typed_registry([])
    ctx = ToolContext()

    passes = await preflight_tool_call(
        registry=registry,
        ctx=ctx,
        tool_call=ToolCall(
            tool_use_id="p1", tool_name="functions.read_file", arguments={"path": "a"}
        ),
    )
    rejected = await preflight_tool_call(
        registry=registry,
        ctx=ctx,
        tool_call=ToolCall(tool_use_id="p2", tool_name="read_file", arguments={"path": 1}),
    )

    assert passes is None
    assert rejected is not None
    assert json.loads(rejected.content)["error_class"] == "ToolArgumentsRejected"


async def test_tool_without_a_schema_still_runs_with_any_arguments() -> None:
    registry = ToolRegistry()
    registry.register(ToolSpec(name="loose", description="Loose.", parameters={}), _echo)
    handler = build_tool_handler(registry, ToolContext())

    result = await handler(
        ToolCall(tool_use_id="l1", tool_name="loose", arguments={"a": "1", "b": None})
    )

    assert result.is_error is False
    assert json.loads(result.content) == {"a": "1", "b": None}
