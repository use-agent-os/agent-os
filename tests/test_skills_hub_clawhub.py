from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest

from agentos.skills.hub.clawhub import (
    ClawHubSource,
    _normalize_zip_entry_path,
)


class _MockResponse:
    def __init__(
        self,
        *,
        content: bytes = b"",
        status_code: int = 200,
        json_data: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        self.content = content
        self.text = content.decode("utf-8", errors="replace")
        self.status_code = status_code
        self._json_data = json_data

    def json(self) -> Any:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _create_zip_bytes(files: dict[str, bytes | str]) -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            if isinstance(content, str):
                zf.writestr(name, content.encode("utf-8"))
            else:
                zf.writestr(name, content)
    return bio.getvalue()


@pytest.mark.parametrize(
    ("path", "root_prefix", "expected"),
    [
        ("SKILL.md", "", "SKILL.md"),
        ("skill-slug/SKILL.md", "skill-slug/", "SKILL.md"),
        ("skill-slug/scripts/run.py", "skill-slug/", "scripts/run.py"),
        ("skill-slug/scripts/sub/run.py", "skill-slug/", "scripts/sub/run.py"),
        ("skill-slug/scripts/sub\\run.py", "skill-slug/", "scripts/sub/run.py"),
        ("../outside.txt", "", None),
        ("skill-slug/../outside.txt", "skill-slug/", None),
        ("skill-slug/sub/../../outside.txt", "skill-slug/", None),
        ("skill-slug/sub\\..\\..\\outside.txt", "skill-slug/", None),
        ("C:\\Windows\\system32\\cmd.exe", "", None),
        ("C:outside.txt", "", None),
        ("/etc/passwd", "", None),
        ("skill-slug/", "", None),
        ("", "", None),
    ],
)
def test_normalize_zip_entry_path(path: str, root_prefix: str, expected: str | None) -> None:
    assert _normalize_zip_entry_path(path, root_prefix=root_prefix) == expected


@pytest.mark.asyncio
async def test_fetch_valid_zip_extracts_files(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    zip_bytes = _create_zip_bytes(
        {
            "my-skill/SKILL.md": "---\nname: my-skill\ndescription: Test skill\n---\n# My Skill\n",
            "my-skill/scripts/helper.py": "print('hello')",
            "my-skill/assets/logo.bin": b"\x00\xff\xfe",
        }
    )

    class _MockClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _MockClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> _MockResponse:
            return _MockResponse(content=zip_bytes)

    monkeypatch.setattr(httpx, "AsyncClient", _MockClient)

    source = ClawHubSource()
    bundle = await source.fetch("my-skill")

    assert bundle is not None
    assert bundle.name == "my-skill"
    assert "SKILL.md" in bundle.files
    assert bundle.files["SKILL.md"].startswith("---")
    assert bundle.files["scripts/helper.py"] == "print('hello')"
    assert bundle.files["assets/logo.bin"] == b"\x00\xff\xfe"


@pytest.mark.asyncio
async def test_fetch_raw_skill_md_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    raw_content = b"---\nname: my-skill\ndescription: Raw SKILL.md\n---\n# Raw\n"

    class _MockClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _MockClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> _MockResponse:
            return _MockResponse(content=raw_content)

    monkeypatch.setattr(httpx, "AsyncClient", _MockClient)

    source = ClawHubSource()
    bundle = await source.fetch("my-skill")

    assert bundle is not None
    assert bundle.name == "my-skill"
    assert "SKILL.md" in bundle.files
    assert "Raw SKILL.md" in bundle.files["SKILL.md"]


@pytest.mark.asyncio
async def test_fetch_blocks_excessive_entry_count(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    files = {
        "my-skill/SKILL.md": "---\nname: my-skill\n---\n",
        "my-skill/file1.txt": "a",
        "my-skill/file2.txt": "b",
        "my-skill/file3.txt": "c",
    }
    zip_bytes = _create_zip_bytes(files)

    class _MockClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _MockClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> _MockResponse:
            return _MockResponse(content=zip_bytes)

    monkeypatch.setattr(httpx, "AsyncClient", _MockClient)

    source = ClawHubSource(max_zip_entries=3)
    bundle = await source.fetch("my-skill")

    assert bundle is None


@pytest.mark.asyncio
async def test_fetch_blocks_declared_size_exceeding_total_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    files = {
        "my-skill/SKILL.md": "---\nname: my-skill\n---\n" + ("x" * 1000),
        "my-skill/data.txt": "y" * 1000,
    }
    zip_bytes = _create_zip_bytes(files)

    class _MockClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _MockClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> _MockResponse:
            return _MockResponse(content=zip_bytes)

    monkeypatch.setattr(httpx, "AsyncClient", _MockClient)

    source = ClawHubSource(max_zip_total_bytes=1500)
    bundle = await source.fetch("my-skill")

    assert bundle is None


@pytest.mark.asyncio
async def test_fetch_blocks_decompression_bomb_single_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    # Highly compressible stream (deflate bomb pattern)
    bomb_data = b"0" * (100 * 1024)
    files = {
        "my-skill/SKILL.md": "---\nname: my-skill\n---\n",
        "my-skill/bomb.txt": bomb_data,
    }
    zip_bytes = _create_zip_bytes(files)

    class _MockClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _MockClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> _MockResponse:
            return _MockResponse(content=zip_bytes)

    monkeypatch.setattr(httpx, "AsyncClient", _MockClient)

    # Allow total size but restrict single entry size
    source = ClawHubSource(max_zip_entry_bytes=10 * 1024, max_zip_total_bytes=500 * 1024)
    bundle = await source.fetch("my-skill")

    assert bundle is None


@pytest.mark.asyncio
async def test_fetch_blocks_decompression_bomb_cumulative_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    files = {
        "my-skill/SKILL.md": "---\nname: my-skill\n---\n",
        "my-skill/part1.txt": b"1" * (5 * 1024),
        "my-skill/part2.txt": b"2" * (5 * 1024),
    }
    zip_bytes = _create_zip_bytes(files)

    class _MockClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _MockClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> _MockResponse:
            return _MockResponse(content=zip_bytes)

    monkeypatch.setattr(httpx, "AsyncClient", _MockClient)

    # Entry cap is 6KB (each part passes), but total cap is 8KB (combined parts fail)
    source = ClawHubSource(max_zip_entry_bytes=6 * 1024, max_zip_total_bytes=8 * 1024)
    bundle = await source.fetch("my-skill")

    assert bundle is None


@pytest.mark.asyncio
async def test_fetch_filters_zip_slip_and_preserves_safe_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    files = {
        "my-skill/SKILL.md": "---\nname: my-skill\n---\n# Title\n",
        "my-skill/../evil.txt": "malicious",
        "my-skill/sub/..\\..\\evil_win.txt": "malicious_win",
        "my-skill/safe.txt": "safe content",
    }
    zip_bytes = _create_zip_bytes(files)

    class _MockClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _MockClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> _MockResponse:
            return _MockResponse(content=zip_bytes)

    monkeypatch.setattr(httpx, "AsyncClient", _MockClient)

    source = ClawHubSource()
    bundle = await source.fetch("my-skill")

    assert bundle is not None
    assert "SKILL.md" in bundle.files
    assert "safe.txt" in bundle.files
    assert "../evil.txt" not in bundle.files
    assert "evil.txt" not in bundle.files
    assert "evil_win.txt" not in bundle.files
