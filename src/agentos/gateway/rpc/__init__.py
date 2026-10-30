"""Gateway RPC package.

This package preserves the former flat RPC import surface verbatim: every existing

    from agentos.gateway.rpc import RpcContext, RpcDispatcher, get_dispatcher

still resolves, because the names are re-exported from
:mod:`agentos.gateway.rpc.registry`. The package additionally exposes
:class:`RpcRegistry` and :func:`get_registry` as the canonical names —
``RpcDispatcher``/``get_dispatcher`` are thin shims.

Sibling modules ``agentos.gateway.rpc_*`` live outside this package and are
imported below so their handler registrations execute at boot time, which
is what the original ``rpc.py`` did.
"""

from __future__ import annotations

from agentos.gateway.rpc.registry import (
    AudienceDriftError,
    RpcContext,
    RpcDispatcher,
    RpcHandlerError,
    RpcHandlerFn,
    RpcMethodEntry,
    RpcRegistry,
    RpcUnavailableError,
    get_dispatcher,
    get_registry,
    require_params_dict,
    validate_classification,
)

__all__ = [
    "RpcContext",
    "AudienceDriftError",
    "RpcDispatcher",
    "RpcHandlerError",
    "RpcHandlerFn",
    "RpcMethodEntry",
    "RpcRegistry",
    "RpcUnavailableError",
    "get_dispatcher",
    "get_registry",
    "require_params_dict",
    "validate_classification",
]

# Import sibling submodules to trigger handler registration against the
# module-level singleton. The import surface intentionally omits the deprecated
# product RPC methods listed in ``REMOVED_PRODUCT_METHODS`` in
# tests/test_gateway/test_rpc_extended.py. These methods MUST NOT register
# handlers at boot — the release surface is contracted to reject them with
# METHOD_NOT_FOUND.
import agentos.gateway.download_grants  # noqa: E402, F401  (artifacts.download)
import agentos.gateway.rpc_acp  # noqa: E402, F401
import agentos.gateway.rpc_agents  # noqa: E402, F401
import agentos.gateway.rpc_approvals  # noqa: E402, F401
import agentos.gateway.rpc_channels  # noqa: E402, F401
import agentos.gateway.rpc_chat  # noqa: E402, F401
import agentos.gateway.rpc_commands  # noqa: E402, F401
import agentos.gateway.rpc_config  # noqa: E402, F401
import agentos.gateway.rpc_cron  # noqa: E402, F401
import agentos.gateway.rpc_diagnostics  # noqa: E402, F401
import agentos.gateway.rpc_doctor  # noqa: E402, F401
import agentos.gateway.rpc_env  # noqa: E402, F401
import agentos.gateway.rpc_flows  # noqa: E402, F401
import agentos.gateway.rpc_gateway  # noqa: E402, F401
import agentos.gateway.rpc_logs  # noqa: E402, F401
import agentos.gateway.rpc_mcp  # noqa: E402, F401
import agentos.gateway.rpc_memory  # noqa: E402, F401
import agentos.gateway.rpc_models  # noqa: E402, F401
import agentos.gateway.rpc_onboarding  # noqa: E402, F401
import agentos.gateway.rpc_plan  # noqa: E402, F401
import agentos.gateway.rpc_projects  # noqa: E402, F401
import agentos.gateway.rpc_push  # noqa: E402, F401
import agentos.gateway.rpc_router  # noqa: E402, F401
import agentos.gateway.rpc_secrets  # noqa: E402, F401
import agentos.gateway.rpc_sessions  # noqa: E402, F401
import agentos.gateway.rpc_skills  # noqa: E402, F401
import agentos.gateway.rpc_system  # noqa: E402, F401
import agentos.gateway.rpc_tools  # noqa: E402, F401
import agentos.gateway.rpc_trading  # noqa: E402, F401
import agentos.gateway.rpc_updates  # noqa: E402, F401
import agentos.gateway.rpc_usage  # noqa: E402, F401

# Fail fast if any registered handler crosses a connection-surface boundary.
validate_classification()
