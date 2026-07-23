"""Invariants for the supported channel install contract."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
UV_LOCK = Path(__file__).resolve().parents[2] / "uv.lock"


@pytest.fixture(scope="module")
def project_table() -> dict:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["project"]


@pytest.fixture(scope="module")
def lock_package() -> dict:
    data = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
    return next(package for package in data["package"] if package["name"] == "use-agent-os")


def _dep_names(specs: list[str]) -> set[str]:
    """Extract canonical (lowercased) package names from a list of PEP 508 specs."""

    names: set[str] = set()
    for spec in specs:
        head = spec.strip()
        for sep in ("[", " ", ";", "=", ">", "<", "~", "!"):
            head = head.split(sep, 1)[0]
        if head:
            names.add(head.lower())
    return names


def test_supported_channel_sdk_is_in_base(project_table: dict) -> None:
    """Telegram imports its vendor SDK and must keep it in the base install."""

    base = _dep_names(project_table["dependencies"])
    assert "python-telegram-bot" in base
    assert not {"dingtalk-stream", "qq-botpy", "cryptography"} & base


def test_mcp_sdk_is_a_base_dependency(project_table: dict) -> None:
    """The built-in MCP UI and server must work without an install extra."""

    base = _dep_names(project_table["dependencies"])
    extras = project_table.get("optional-dependencies", {})

    assert "mcp" in base
    assert "mcp" not in extras


def test_no_dead_extras(project_table: dict) -> None:
    """Retired and non-public adapters must not expose install extras."""

    extras = project_table.get("optional-dependencies", {})
    assert not {"dingtalk", "matrix", "matrix-e2e", "msteams", "qq", "wecom"} & set(extras)


def test_base_channel_extras_are_not_exposed_as_noop_aliases(
    project_table: dict,
) -> None:
    """Base-install channels must not be exposed as no-op extras."""

    extras = project_table.get("optional-dependencies", {})
    for name in ("discord", "slack", "telegram"):
        assert name not in extras, f"{name} is installed from base; do not expose a no-op extra"


def test_lockfile_does_not_advertise_removed_base_channel_extras(
    lock_package: dict,
) -> None:
    """uv.lock metadata must match the package install contract."""

    provides_extras = set(lock_package.get("provides-extras", []))
    assert not {
        "dingtalk",
        "discord",
        "matrix",
        "matrix-e2e",
        "qq",
        "slack",
        "telegram",
        "wecom",
    } & provides_extras


def test_no_duplicate_ml_extra(project_table: dict) -> None:
    """``recommended`` and ``model-router`` historically overlapped — only one survives."""

    extras = project_table.get("optional-dependencies", {})
    has_recommended = "recommended" in extras
    has_model_router = "model-router" in extras
    assert has_recommended, "recommended extra must exist (router users opt in here)"
    assert not has_model_router, (
        "model-router extra duplicates recommended — collapse into one"
    )


def test_alpha_classifier_present(project_table: dict) -> None:
    """0.1.0 stays pre-stable — the classifier must reflect that."""

    classifiers = project_table.get("classifiers", [])
    assert "Development Status :: 3 - Alpha" in classifiers, (
        "Alpha classifier signals to PyPI/uv that this is pre-stable"
    )


def test_readme_points_at_user_facing_file(project_table: dict) -> None:
    """``readme`` must be the user-facing README, not the legacy portable view."""

    assert project_table["readme"] == "README.md", (
        "readme should point at the canonical README.md after the 0.1.0 refactor"
    )
