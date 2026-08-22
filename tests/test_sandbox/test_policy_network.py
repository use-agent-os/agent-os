from __future__ import annotations

from pathlib import Path

from agentos.sandbox.config import SandboxSettings
from agentos.sandbox.policy import build_policy
from agentos.sandbox.types import NetworkMode, SecurityLevel


def test_standard_network_http_keeps_host_network(tmp_path: Path) -> None:
    policy = build_policy(
        SecurityLevel.STANDARD,
        "network.http",
        tmp_path,
        SandboxSettings(),
        trusted=True,
    )

    assert policy.network is NetworkMode.HOST


def test_standard_shell_and_code_exec_keep_network_none(tmp_path: Path) -> None:
    settings = SandboxSettings()

    shell_policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        tmp_path,
        settings,
        trusted=True,
    )
    code_policy = build_policy(
        SecurityLevel.STANDARD,
        "code.exec",
        tmp_path,
        settings,
        trusted=True,
    )

    assert shell_policy.network is NetworkMode.NONE
    assert code_policy.network is NetworkMode.NONE


def test_network_default_proxy_allowlist_resolves_policy(tmp_path: Path) -> None:
    settings = SandboxSettings(network_default="proxy_allowlist")

    shell_policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        tmp_path,
        settings,
        trusted=True,
    )
    strict_policy = build_policy(
        SecurityLevel.STRICT,
        "shell.exec",
        tmp_path,
        settings,
        trusted=True,
    )
    locked_policy = build_policy(
        SecurityLevel.LOCKED,
        "shell.exec",
        tmp_path,
        settings,
        trusted=True,
    )

    assert shell_policy.network is NetworkMode.PROXY_ALLOWLIST
    assert strict_policy.network is NetworkMode.PROXY_ALLOWLIST
    assert locked_policy.network is NetworkMode.PROXY_ALLOWLIST


def test_disabled_level_keeps_host_even_with_proxy_allowlist_default(tmp_path: Path) -> None:
    settings = SandboxSettings(network_default="proxy_allowlist", allow_legacy_mode=True)

    disabled_policy = build_policy(
        SecurityLevel.DISABLED,
        "shell.exec",
        tmp_path,
        settings,
        trusted=True,
    )

    assert disabled_policy.network is NetworkMode.HOST


def test_standard_network_tag_keeps_host_even_with_proxy_allowlist_default(tmp_path: Path) -> None:
    settings = SandboxSettings(network_default="proxy_allowlist")

    policy = build_policy(
        SecurityLevel.STANDARD,
        "network.http",
        tmp_path,
        settings,
        trusted=True,
    )

    assert policy.network is NetworkMode.HOST


def test_validate_combination_warns_on_reserved_proxy_allowlist() -> None:
    settings = SandboxSettings(network_default="proxy_allowlist")
    effective = settings.validate_combination()

    assert "network_default_proxy_allowlist_reserved" in effective.notes
