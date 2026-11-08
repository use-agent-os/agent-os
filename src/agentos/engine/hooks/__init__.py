"""Engine-level hook protocols.

Current production status:

* :class:`TurnHook` is active for turn event emission. ``TurnRunner`` registers
  :class:`DefaultTraceEmitterHook` by default.
* :class:`CompactionHook` is active when explicitly supplied to ``TurnRunner``;
  the compaction/history stage fires it around pre-turn compaction attempts.
* :class:`ToolHook` is supported by the tool-dispatch factory, but ``TurnRunner``
  does not register tool hooks by default.

Default implementations in :mod:`agentos.engine.hooks.defaults` preserve the
canonical inline behavior unless a caller explicitly supplies additional hooks.
"""

from __future__ import annotations

from agentos.engine.hooks.defaults import (
    DefaultMemoryFlushHook,
    DefaultTraceEmitterHook,
    DefaultTranscriptHook,
    NoopCompactionHook,
    NoopToolHook,
    NoopTurnHook,
    build_default_turn_hooks,
    fire_after_compact,
    fire_before_compact,
)
from agentos.engine.hooks.types import (
    Allow,
    Amend,
    Block,
    CompactionHook,
    CompactionState,
    ReplaceResult,
    RequestHook,
    RequestHookCall,
    Rewrite,
    ToolHook,
    ToolHookCall,
    ToolHookDecision,
    ToolHookResult,
    TurnEvent,
    TurnHook,
    TurnHookContext,
    TurnHookResult,
    Veto,
)

__all__ = [
    "Allow",
    "Amend",
    "Block",
    "CompactionHook",
    "CompactionState",
    "DefaultMemoryFlushHook",
    "DefaultTraceEmitterHook",
    "DefaultTranscriptHook",
    "NoopCompactionHook",
    "NoopToolHook",
    "NoopTurnHook",
    "ReplaceResult",
    "RequestHook",
    "RequestHookCall",
    "Rewrite",
    "ToolHook",
    "ToolHookCall",
    "ToolHookDecision",
    "ToolHookResult",
    "TurnEvent",
    "TurnHook",
    "TurnHookContext",
    "TurnHookResult",
    "Veto",
    "build_default_turn_hooks",
    "fire_after_compact",
    "fire_before_compact",
]
