"""Sandbox & security-grading package.

Public surface for the sandbox subsystem. Importing this package is safe at
startup: no subprocesses are spawned, no filesystem probing beyond an
in-memory import of the backend modules.
"""

from __future__ import annotations

from agentos.sandbox.backend import (
    Backend,
    BubblewrapBackend,
    NoopBackend,
    SeatbeltBackend,
    select_backend,
)
from agentos.sandbox.config import EffectiveMode, SandboxSettings
from agentos.sandbox.governance import (
    ApprovalGate,
    DenialLedger,
    action_fingerprint,
    gate_execution,
    on_successful_exec,
    post_denial_guard,
)
from agentos.sandbox.integration import (
    SandboxRuntime,
    configure_runtime,
    gate_action,
    get_runtime,
    record_success,
    reset_runtime,
    run_under_backend,
    sandboxed,
)
from agentos.sandbox.policy import LevelHints, build_policy, select_level
from agentos.sandbox.stale_output_cache import (
    StaleOutputCache,
    get_stale_output_cache,
    reset_stale_output_cache,
)
from agentos.sandbox.types import (
    ALLOW,
    ApprovalDecision,
    DenialReason,
    DenialResult,
    FollowupTag,
    MountMode,
    MountSpec,
    NetworkMode,
    ResourceLimits,
    SandboxBackendError,
    SandboxPolicy,
    SandboxRequest,
    SandboxResult,
    SecurityLevel,
    SuggestedNextStep,
)

__all__ = [
    "ALLOW",
    "ApprovalDecision",
    "ApprovalGate",
    "Backend",
    "BubblewrapBackend",
    "DenialLedger",
    "DenialReason",
    "DenialResult",
    "EffectiveMode",
    "FollowupTag",
    "LevelHints",
    "MountMode",
    "MountSpec",
    "NetworkMode",
    "NoopBackend",
    "ResourceLimits",
    "SandboxBackendError",
    "SandboxPolicy",
    "SandboxRequest",
    "SandboxResult",
    "SandboxRuntime",
    "SandboxSettings",
    "SeatbeltBackend",
    "SecurityLevel",
    "StaleOutputCache",
    "SuggestedNextStep",
    "action_fingerprint",
    "build_policy",
    "configure_runtime",
    "gate_action",
    "gate_execution",
    "get_runtime",
    "get_stale_output_cache",
    "on_successful_exec",
    "post_denial_guard",
    "record_success",
    "reset_runtime",
    "reset_stale_output_cache",
    "run_under_backend",
    "sandboxed",
    "select_backend",
    "select_level",
]
