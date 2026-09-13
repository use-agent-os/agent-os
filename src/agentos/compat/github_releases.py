"""Tiny GitHub Releases client: the fallback release source for ``agentos upgrade``.

Every AgentOS release is published twice — a wheel on PyPI and the same wheel
attached to the GitHub release the tag created (``wheelhouse-release.yml``).
``install.sh`` installs from the GitHub asset; ``agentos upgrade`` targets PyPI.
When the PyPI publish fails or lags, the two disagree and an upgrade that only
knows PyPI reports "a newer version is available" without being able to install
it. This module lets the upgrade consult GitHub too and, when GitHub is ahead,
build the exact ``dist[extras] @ <wheel-url>`` spec ``install.sh`` uses.

Like :mod:`agentos.compat.pypi_client`, it never raises on network failure: it
returns ``None`` so callers degrade to the PyPI answer.
"""

from __future__ import annotations

import os
import re

DEFAULT_REPOSITORY = "use-agent-os/agent-os"
_LATEST_RELEASE_URL = "https://api.github.com/repos/{repo}/releases/latest"
_WHEEL_URL = (
    "https://github.com/{repo}/releases/download/{tag}/use_agent_os-{version}-py3-none-any.whl"
)
_TAG_RE = re.compile(r"^v?(\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?(?:\.post\d+)?)$")


def repository() -> str:
    """The ``owner/name`` slug releases are fetched from.

    ``AGENTOS_REPOSITORY`` mirrors the override ``install.sh`` honours so a fork
    that publishes its own releases upgrades from them.
    """

    override = os.environ.get("AGENTOS_REPOSITORY", "").strip()
    return override or DEFAULT_REPOSITORY


def version_from_tag(tag: str) -> str | None:
    """``v2026.9.11`` → ``2026.9.11``; anything that is not a release tag → ``None``."""

    match = _TAG_RE.match(tag.strip())
    return match.group(1) if match else None


def latest_release_version(*, timeout: float = 5.0, repo: str | None = None) -> str | None:
    """Version of the newest non-prerelease GitHub release, or ``None``.

    GitHub's ``releases/latest`` already excludes drafts and prereleases, so a
    preview tag never wins over a stable one here.
    """

    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a hard dependency
        return None

    url = _LATEST_RELEASE_URL.format(repo=repo or repository())
    try:
        response = httpx.get(
            url,
            timeout=timeout,
            headers={"Accept": "application/vnd.github+json"},
            follow_redirects=True,
        )
    except Exception:  # noqa: BLE001 - offline / DNS / TLS / timeout all degrade to None
        return None
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - malformed body
        return None
    if not isinstance(body, dict):
        return None
    tag = body.get("tag_name")
    if not isinstance(tag, str):
        return None
    return version_from_tag(tag)


def wheel_url(version: str, *, repo: str | None = None) -> str:
    """The release-asset wheel URL for ``version`` (the one ``install.sh`` downloads)."""

    return _WHEEL_URL.format(repo=repo or repository(), tag=f"v{version}", version=version)
