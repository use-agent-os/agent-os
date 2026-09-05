"""Signature and callable introspection helpers."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any


def accepts_keyword_arg(target: Callable[..., Any], arg_name: str) -> bool:
    """Check whether a callable accepts *arg_name* as a keyword argument.

    Returns ``True`` if the target's signature includes *arg_name* (either as
    a named parameter or through a ``**kwargs`` parameter). Returns ``False``
    if the parameter is not accepted or if signature introspection fails.
    """
    try:
        sig = inspect.signature(target)
    except (TypeError, ValueError):
        return False

    return any(
        (p.name == arg_name and p.kind != inspect.Parameter.POSITIONAL_ONLY)
        or p.kind == inspect.Parameter.VAR_KEYWORD
        for p in sig.parameters.values()
    )


_accepts_keyword_arg = accepts_keyword_arg
