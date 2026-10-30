"""Image Flows: a small node-graph image workflow engine inside the gateway.

Two link types (TEXT, IMAGE), seven nodes, a model registry with prices,
a Merkle cache and an asyncio executor. :class:`FlowService` is the public
entry point; ``agentos.gateway.rpc_flows`` exposes it as ``flows.*`` RPC.
"""

from __future__ import annotations

from agentos.flows.service import (
    FlowService,
    get_flow_service,
    peek_flow_service,
    reset_flow_service,
    set_flow_service,
)
from agentos.flows.types import FlowError, ImageRef

__all__ = [
    "FlowError",
    "FlowService",
    "ImageRef",
    "get_flow_service",
    "peek_flow_service",
    "reset_flow_service",
    "set_flow_service",
]
