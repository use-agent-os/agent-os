"""GitHub Releases client — tag parsing, wheel URL, and a never-raising fetch."""

from __future__ import annotations

from typing import Any

import pytest

from agentos.compat import github_releases


def test_version_from_tag_accepts_release_tags() -> None:
    assert github_releases.version_from_tag("v2026.9.11") == "2026.9.11"
    assert github_releases.version_from_tag("2026.9.11") == "2026.9.11"
    assert github_releases.version_from_tag("v2026.9.11.post1") == "2026.9.11.post1"
    assert github_releases.version_from_tag("v0.0.1rc1") == "0.0.1rc1"


def test_version_from_tag_rejects_non_release_tags() -> None:
    assert github_releases.version_from_tag("desktop-v1") is None
    assert github_releases.version_from_tag("main") is None
    assert github_releases.version_from_tag("") is None


def test_wheel_url_matches_the_installer_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTOS_REPOSITORY", raising=False)
    assert github_releases.wheel_url("2026.9.11") == (
        "https://github.com/use-agent-os/agent-os/releases/download/v2026.9.11/"
        "use_agent_os-2026.9.11-py3-none-any.whl"
    )


def test_repository_override_mirrors_install_sh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_REPOSITORY", "someone/fork")
    assert github_releases.repository() == "someone/fork"
    assert github_releases.wheel_url("1.2.3").startswith("https://github.com/someone/fork/")


class _Response:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _patch_get(monkeypatch: pytest.MonkeyPatch, response: Any) -> None:
    import httpx

    def fake_get(*_: Any, **__: Any) -> Any:
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(httpx, "get", fake_get)


def test_latest_release_version_reads_tag_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get(monkeypatch, _Response(200, {"tag_name": "v2026.9.11"}))
    assert github_releases.latest_release_version() == "2026.9.11"


@pytest.mark.parametrize(
    "response",
    [
        _Response(404, {}),
        _Response(200, "not an object"),
        _Response(200, {"tag_name": "desktop-v1"}),
        _Response(200, ValueError("bad json")),
        ConnectionError("offline"),
    ],
)
def test_latest_release_version_degrades_to_none(
    monkeypatch: pytest.MonkeyPatch, response: Any
) -> None:
    _patch_get(monkeypatch, response)
    assert github_releases.latest_release_version() is None
