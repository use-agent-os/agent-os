"""Persistence layer: schema migration + related primitives.

Public entry point is :func:`agentos.persistence.migrator.apply_pending`.
"""

from agentos.persistence.migrator import apply_pending

__all__ = ["apply_pending"]
