from __future__ import annotations

from typing import Any

import pytest

from agentos.skills.hub.github import GitHubSource


class _Response:
    def __init__(
        self,
        *,
        json_data: dict[str, Any] | None = None,
        content: bytes = b"",
        status_code: int = 200,
    ) -> None:
        self._json_data = json_data or {}
        self.content = content
        self.text = content.decode("utf-8", errors="replace")
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _AsyncClient:
    tree_entries = [
        {"path": "skills/demo/SKILL.md", "type": "blob"},
        {"path": "skills/demo/scripts/run.py", "type": "blob"},
        {"path": "skills/demo/assets/logo.bin", "type": "blob"},
        {"path": "skills/other/SKILL.md", "type": "blob"},
    ]
    raw_payloads = {
        "skills/demo/SKILL.md": b"---\nname: demo\ndescription: Demo skill.\n---\n\n# Demo\n",
        "skills/demo/scripts/run.py": b"print('demo')\n",
        "skills/demo/assets/logo.bin": b"\x00\xff",
    }
    requests: list[tuple[str, dict[str, Any]]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _AsyncClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _Response:
        self.requests.append((url, kwargs))
        if "/git/trees/" in url:
            return _Response(json_data={"tree": self.tree_entries, "truncated": False})
        marker = "raw.githubusercontent.com/acme/skillpack/main/"
        if marker in url:
            rel_path = url.split(marker, 1)[1]
            return _Response(content=self.raw_payloads[rel_path])
        raise AssertionError(f"unexpected URL: {url}")


@pytest.mark.asyncio
async def test_fetch_github_tree_url_downloads_whole_skill_directory(monkeypatch) -> None:
    import httpx

    _AsyncClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    bundle = await GitHubSource().fetch("https://github.com/acme/skillpack/tree/main/skills/demo")

    assert bundle is not None
    assert bundle.name == "demo"
    assert set(bundle.files) == {"SKILL.md", "scripts/run.py", "assets/logo.bin"}
    assert bundle.files["scripts/run.py"] == "print('demo')\n"
    assert bundle.files["assets/logo.bin"] == b"\x00\xff"
    assert bundle.meta is not None
    assert bundle.meta.source_id == "github"
    assert bundle.meta.identifier == "acme/skillpack@main:skills/demo/SKILL.md"


@pytest.mark.asyncio
async def test_fetch_github_blob_url_uses_parent_skill_directory(monkeypatch) -> None:
    import httpx

    _AsyncClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    bundle = await GitHubSource().fetch(
        "https://github.com/acme/skillpack/blob/main/skills/demo/SKILL.md"
    )

    assert bundle is not None
    assert bundle.name == "demo"
    assert set(bundle.files) == {"SKILL.md", "scripts/run.py", "assets/logo.bin"}


@pytest.mark.asyncio
async def test_fetch_legacy_identifier_keeps_support_and_downloads_directory(monkeypatch) -> None:
    import httpx

    _AsyncClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    bundle = await GitHubSource().fetch("acme/skillpack@main:skills/demo/SKILL.md")

    assert bundle is not None
    assert bundle.name == "demo"
    assert set(bundle.files) == {"SKILL.md", "scripts/run.py", "assets/logo.bin"}


def test_default_gateway_router_exposes_github_without_token(monkeypatch) -> None:
    import agentos.gateway.rpc_skills as rpc_skills
    from agentos.skills.hub import defaults

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    defaults._default_router = None

    try:
        router = rpc_skills._get_default_router()
        assert "github" in router.source_ids
    finally:
        defaults._default_router = None


def test_frontmatter_field_reads_yaml_block_scalar() -> None:
    from agentos.skills.hub.github import _frontmatter_field

    skill_md = (
        "---\n"
        "name: bankr-communities\n"
        "description: >-\n"
        "  Manage token-gated communities\n"
        "  across Telegram and Farcaster.\n"
        "homepage: https://bankr.bot\n"
        "---\n"
        "# Body\n"
    )
    assert _frontmatter_field(skill_md, "name") == "bankr-communities"
    assert (
        _frontmatter_field(skill_md, "description")
        == "Manage token-gated communities across Telegram and Farcaster."
    )
    # Literal block scalars ("|") fold the same way for one-line display.
    assert _frontmatter_field(skill_md.replace(">-", "|-"), "description") == (
        "Manage token-gated communities across Telegram and Farcaster."
    )


def test_frontmatter_block_scalar_is_bounded_and_stops_at_frontmatter_end() -> None:
    from agentos.skills.hub.github import _frontmatter_field

    # The fold must stop at the closing --- and never leak the body.
    skill_md = (
        "---\n"
        "description: >-\n"
        "  Short description.\n"
        "---\n"
        "  indented body line that must NOT be folded in\n"
    )
    assert _frontmatter_field(skill_md, "description") == "Short description."

    # A hostile/unterminated block scalar cannot produce an unbounded value.
    hostile = "---\ndescription: |\n" + ("  spam line\n" * 5000)
    assert len(_frontmatter_field(hostile, "description")) <= 2000


def test_frontmatter_block_scalar_indicator_variants() -> None:
    from agentos.skills.hub.github import _frontmatter_field

    for indicator in ("|2", ">+", "> # a comment", "|-2"):
        skill_md = f"---\ndescription: {indicator}\n  Folded text here.\n---\n"
        assert _frontmatter_field(skill_md, "description") == "Folded text here.", indicator


def test_frontmatter_plain_value_is_length_bounded() -> None:
    from agentos.skills.hub.github import _MAX_FOLDED_LEN, _frontmatter_field

    # A very long single-line (non-block) description must not ship unbounded.
    hostile = "---\ndescription: " + ("A" * 1_000_000) + "\n---\n"
    assert len(_frontmatter_field(hostile, "description")) <= _MAX_FOLDED_LEN

    quoted = '---\ndescription: "' + ("B" * 1_000_000) + '"\n---\n'
    assert len(_frontmatter_field(quoted, "description")) <= _MAX_FOLDED_LEN


def test_frontmatter_handles_crlf_line_endings() -> None:
    from agentos.skills.hub.github import _frontmatter_field

    skill_md = "---\r\ndescription: On-chain data\r\n---\r\n# Body\r\n"
    assert _frontmatter_field(skill_md, "description") == "On-chain data"
