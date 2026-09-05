"""Introspection and callable signature compatibility helpers."""

from __future__ import annotations

import inspect
from typing import Any


def accepts_keyword_arg(callable_obj: Any, name: str) -> bool:
    """Return True when callable accepts keyword argument `name` explicitly or via `**kwargs`.

    Returns False if `callable_obj` is not a callable or if inspect.signature
    raises TypeError or ValueError.
    """
    try:
        params = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return False
    if name in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


_accepts_keyword_arg = accepts_keyword_arg

__all__ = ["_accepts_keyword_arg", "accepts_keyword_arg"]
