"""Former home of the ``_TurnRunner*Adapter`` classes (removed in #3819).

The stage ports are now implemented by ``TurnRunner`` itself
(:class:`agentos.engine.harness.ports.TurnRunnerPorts`) and the turn is
orchestrated by :class:`agentos.engine.harness.turn.TurnHarness`. This module
only keeps the compaction persistence error importable from its old path.
"""

from __future__ import annotations

from agentos.engine.harness.ports import CompactionPersistRejectedError

__all__ = ["CompactionPersistRejectedError"]
