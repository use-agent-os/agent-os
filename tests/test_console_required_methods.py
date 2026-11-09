"""The consoles may only call RPC methods the engine ships.

``frontend/src/lib/required-methods.ts`` lists every gateway RPC method the
web console calls; ``desktop/src/renderer/src/lib/required-methods.ts`` lists
the desktop's additional ones. Each name must be advertised in the handshake's
``features.methods`` (built from the same registry the dispatcher routes
with), or a console ships a button that can only ever answer
METHOD_NOT_FOUND.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentos.gateway.rpc import get_dispatcher
from agentos.gateway.websocket import _build_features

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LISTS = {
    "web console": (
        _REPO_ROOT / "frontend" / "src" / "lib" / "required-methods.ts",
        "REQUIRED_METHODS",
    ),
    "desktop": (
        _REPO_ROOT / "desktop" / "src" / "renderer" / "src" / "lib" / "required-methods.ts",
        "DESKTOP_REQUIRED_METHODS",
    ),
}
_STRING = re.compile(r"""['"]([^'"\n]+)['"]""")


def _array_body(source: str, name: str) -> str:
    match = re.search(
        rf"export const {name}\b[^=]*=\s*\[(?P<body>.*?)\]",
        source,
        flags=re.DOTALL,
    )
    assert match, f"{name} array not found"
    # Drop line comments so a commented-out name never counts.
    return re.sub(r"//[^\n]*", "", match.group("body"))


def _required(label: str) -> list[str]:
    path, name = _LISTS[label]
    return _STRING.findall(_array_body(path.read_text(encoding="utf-8"), name))


def _advertised() -> set[str]:
    return set(_build_features(get_dispatcher()).methods)


@pytest.mark.parametrize("label", sorted(_LISTS))
def test_list_parses_to_unique_method_names(label: str) -> None:
    methods = _required(label)
    assert len(methods) > 10, f"{label}: suspiciously short list, the parser may have broken"
    assert len(methods) == len(set(methods)), f"{label}: duplicate entries"
    for method in methods:
        # Hyphens appear in a few legacy gateway names (``last-heartbeat``).
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)*", method), method


@pytest.mark.parametrize("label", sorted(_LISTS))
def test_every_required_method_is_advertised(label: str) -> None:
    missing = sorted(set(_required(label)) - _advertised())
    assert missing == [], (
        f"the {label} calls RPC methods the gateway does not register: {missing}. "
        "Register them in the engine, or stop calling them."
    )


def test_desktop_list_holds_only_additional_methods() -> None:
    shared = set(_required("web console")) & set(_required("desktop"))
    assert shared == set(), f"listed in both files (keep them in the web list only): {shared}"
