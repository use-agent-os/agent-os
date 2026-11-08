"""Shared, deterministic test doubles for driving the agent harness.

``ScriptedProvider`` plays back a fixed list of provider turns and records each
request; ``FakeToolbox`` supplies tools whose timing and failure behaviour a
test controls. Both stay offline and credential-free so they are safe on the
default ``pytest`` path.
"""

from tests.fakes.scripted_provider import (
    CallRecord,
    Hang,
    Raise,
    ScriptedProvider,
    done,
    error,
    text,
    thinking,
    tool_call,
)
from tests.fakes.selector import ChainSelector
from tests.fakes.tools import FakeTool, FakeToolbox

__all__ = [
    "CallRecord",
    "ChainSelector",
    "FakeTool",
    "FakeToolbox",
    "Hang",
    "Raise",
    "ScriptedProvider",
    "done",
    "error",
    "text",
    "thinking",
    "tool_call",
]
