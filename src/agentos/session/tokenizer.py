"""Token estimation — tiktoken when available, len//4 fallback."""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)

_encoding = None
_tiktoken_available: bool | None = None
_FAST_ESTIMATE_CHAR_LIMIT = 100_000


def _get_encoding():
    global _encoding, _tiktoken_available
    if _tiktoken_available is False:
        return None
    if _encoding is not None:
        return _encoding
    try:
        import tiktoken

        _encoding = tiktoken.get_encoding("cl100k_base")
        _tiktoken_available = True
        return _encoding
    except ImportError:
        _tiktoken_available = False
        log.info("tiktoken_unavailable_fallback")
        return None
    except Exception as exc:  # noqa: BLE001
        _tiktoken_available = False
        log.warning("tiktoken_encoding_unavailable_fallback", error=str(exc))
        return None


def estimate_tokens(text: str) -> int:
    """Estimate token count. Uses tiktoken cl100k_base if available, else len//4."""
    if len(text) > _FAST_ESTIMATE_CHAR_LIMIT:
        return max(1, len(text) // 4)
    enc = _get_encoding()
    if enc is not None:
        return max(1, len(enc.encode(text)))
    return max(1, len(text) // 4)
