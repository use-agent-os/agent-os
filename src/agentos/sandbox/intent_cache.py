"""Compatibility exports for the application intent approval cache."""

from agentos.application.intent_cache import (
    IntentApprovalCache,
    _extract_intent,
    _extract_intents,
    get_intent_cache,
    reset_intent_cache,
)

__all__ = [
    "IntentApprovalCache",
    "_extract_intent",
    "_extract_intents",
    "get_intent_cache",
    "reset_intent_cache",
]
