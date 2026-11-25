"""The operator's read-only workspace file browser (``workspace.files.*``)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from agentos.gateway import workspace_files as wf
from agentos.gateway.access import ConnectionSurface
from agentos.gateway.agent_surface import AgentBinding
from agentos.gateway.auth import AccessContext
from agentos.gateway.config import AuthConfig, GatewayConfig
from agentos.gateway.download_grants import get_download_grants, register_download_routes
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.gateway.workspace_files import (
    WorkspaceFileError,
    classify,
    is_denied_name,
    list_directory,
    parse_relative_path,
    read_download,
    read_text,
    stat_download,
)

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX links and FIFOs")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # Bytes, not text: on Windows write_text would turn "\n" into "\r\n".
    (workspace / "MEMORY.md").write_bytes(b"# Memory\n\nhello\n")
    (workspace / "notes.txt").write_text("plain", encoding="utf-8")
    (workspace / "data.json").write_text('{"a": 1}', encoding="utf-8")
    (workspace / "pic.png").write_bytes(PNG)
    (workspace / "doc.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    (workspace / "documents").mkdir()
    (workspace / "documents" / "Plan.md").write_text("plan", encoding="utf-8")
    (workspace / ".env").write_text("TOKEN=abc", encoding="utf-8")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("[core]", encoding="utf-8")
    (workspace / "MEMORY.md.lock").write_text("", encoding="utf-8")
    (workspace / "server.pem").write_text("-----BEGIN", encoding="utf-8")
    return workspace


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    return secret


def _code(exc: pytest.ExceptionInfo[WorkspaceFileError]) -> str:
    return exc.value.code


# ---------------------------------------------------------------------------
# Path parsing
# ---------------------------------------------------------------------------


class TestParse:
    @pytest.mark.parametrize("raw", [None, ""])
    def test_root(self, raw: Any) -> None:
        assert parse_relative_path(raw) == ()

    def test_segments(self) -> None:
        assert parse_relative_path("documents/Plan.md") == ("documents", "Plan.md")
        assert parse_relative_path("a b/c d.txt") == ("a b", "c d.txt")

    @pytest.mark.parametrize(
        "raw",
        [
            "/etc/passwd",
            "~/x",
            "..",
            "../x",
            "a/../b",
            "a/./b",
            "a//b",
            "a/",
            "a\\b",
            "..\\..\\x",
            "C:/Windows",
            "a/file.txt:stream",
            "a\x00b",
            "a\nb",
            "a /b",
            "a./b",
            123,
            ["a"],
            "x" * 2000,
            "/".join(["d"] * 40),
            "y" * 300,
        ],
    )
    def test_refused_shapes(self, raw: Any) -> None:
        with pytest.raises(WorkspaceFileError) as exc:
            parse_relative_path(raw)
        assert _code(exc) == "invalid_path"

    @pytest.mark.parametrize(
        "raw",
        [
            ".env",
            ".env.local",
            ".git/config",
            "projects/app/.git/HEAD",
            ".ssh/id_rsa",
            "keys/id_ed25519",
            "certs/server.pem",
            "certs/SERVER.PEM",
            "x.key",
            "trading.sqlite",
            "trading.sqlite-wal",
            "state.db",
            "MEMORY.md.lock",
            "wallets/main.json",
            "credentials",
            "credentials.json",
            "secrets.toml",
            "Secrets.YAML",
            "unlock.key",
        ],
    )
    def test_denied_names_at_any_depth(self, raw: str) -> None:
        with pytest.raises(WorkspaceFileError) as exc:
            parse_relative_path(raw)
        assert _code(exc) == "denied"


class TestNames:
    @pytest.mark.parametrize("name", ["MEMORY.md", "notes.txt", "secretary.md", "keynote.txt"])
    def test_ordinary_names_are_allowed(self, name: str) -> None:
        assert not is_denied_name(name)

    def test_classify(self) -> None:
        assert classify("README.md") == ("markdown", "markdown")
        assert classify("a.TXT") == ("text", "")
        assert classify("a.json") == ("code", "json")
        assert classify("Dockerfile") == ("code", "dockerfile")
        assert classify("a.PNG") == ("image", "")
        assert classify("a.svg") == ("image", "")
        assert classify("a.pdf") == ("pdf", "")
        assert classify("LICENSE") == ("other", "")
        # An HTML file is source to read, never a page to run.
        assert classify("index.html") == ("code", "xml")


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


class TestList:
    def test_directories_first_and_sensitive_names_hidden(self, root: Path) -> None:
        listing = list_directory(root, ())
        names = [entry["name"] for entry in listing["entries"]]
        assert names == ["documents", "data.json", "doc.pdf", "MEMORY.md", "notes.txt", "pic.png"]
        assert listing["truncated"] is False and listing["exists"] is True
        by_name = {entry["name"]: entry for entry in listing["entries"]}
        assert by_name["documents"]["type"] == "directory"
        assert by_name["MEMORY.md"]["kind"] == "markdown"
        assert by_name["data.json"]["language"] == "json"
        assert by_name["pic.png"]["size"] == len(PNG)
        assert by_name["MEMORY.md"]["path"] == "MEMORY.md"

    def test_nested(self, root: Path) -> None:
        listing = list_directory(root, ("documents",))
        assert [e["path"] for e in listing["entries"]] == ["documents/Plan.md"]

    def test_missing_root_is_an_empty_browser(self, tmp_path: Path) -> None:
        listing = list_directory(tmp_path / "nope", ())
        assert listing["entries"] == [] and listing["exists"] is False

    def test_missing_directory(self, root: Path) -> None:
        with pytest.raises(WorkspaceFileError) as exc:
            list_directory(root, ("nope",))
        assert _code(exc) == "not_found"

    def test_a_file_is_not_a_directory(self, root: Path) -> None:
        with pytest.raises(WorkspaceFileError) as exc:
            list_directory(root, ("notes.txt",))
        assert _code(exc) == "not_a_directory"

    def test_capped(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wf, "MAX_DIR_ENTRIES", 3)
        listing = list_directory(root, ())
        assert len(listing["entries"]) == 3
        assert listing["truncated"] is True and listing["total"] == 6

    @posix_only
    def test_symlinks_are_listed_but_not_openable(self, root: Path, outside: Path) -> None:
        os.symlink(outside, root / "link.txt")
        os.symlink(outside.parent, root / "linkdir")
        by_name = {e["name"]: e for e in list_directory(root, ())["entries"]}
        assert by_name["link.txt"]["type"] == "symlink"
        assert by_name["link.txt"]["openable"] is False
        assert by_name["linkdir"]["type"] == "symlink"

    @posix_only
    def test_a_symlinked_directory_is_not_entered(self, root: Path, outside: Path) -> None:
        os.symlink(outside.parent, root / "linkdir")
        with pytest.raises(WorkspaceFileError) as exc:
            list_directory(root, ("linkdir",))
        assert _code(exc) == "unsafe"

    @posix_only
    def test_a_symlinked_root_is_the_operators_choice(self, root: Path, tmp_path: Path) -> None:
        alias = tmp_path / "alias"
        os.symlink(root, alias)
        assert list_directory(alias, ("documents",))["entries"][0]["name"] == "Plan.md"

    @posix_only
    def test_special_files_are_not_listed(self, root: Path) -> None:
        os.mkfifo(root / "pipe")
        assert "pipe" not in [e["name"] for e in list_directory(root, ())["entries"]]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


class TestRead:
    def test_markdown(self, root: Path) -> None:
        result = read_text(root, ("MEMORY.md",))
        assert result["previewable"] is True
        assert result["content"] == "# Memory\n\nhello\n"
        assert result["kind"] == "markdown" and result["truncated"] is False

    def test_extensionless_text_reads_as_text(self, root: Path) -> None:
        (root / "LICENSE").write_text("Apache", encoding="utf-8")
        result = read_text(root, ("LICENSE",))
        assert result["previewable"] is True and result["kind"] == "text"

    def test_bom_is_dropped_and_bad_bytes_replaced(self, root: Path) -> None:
        (root / "bom.txt").write_bytes(b"\xef\xbb\xbfhi \xff there")
        assert read_text(root, ("bom.txt",))["content"] == "hi \ufffd there"

    def test_binary_is_not_previewed(self, root: Path) -> None:
        (root / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
        result = read_text(root, ("blob.bin",))
        assert result["previewable"] is False and result["reason"] == "binary"
        assert result["content"] == ""

    def test_images_and_pdfs_go_through_a_ticket(self, root: Path) -> None:
        for name in ("pic.png", "doc.pdf"):
            result = read_text(root, (name,))
            assert result["previewable"] is False and result["reason"] == "download"
            assert result["content"] == ""

    def test_truncated_at_the_cap(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wf, "MAX_TEXT_BYTES", 10)
        (root / "big.txt").write_text("a" * 9 + "é" + "tail", encoding="utf-8")
        result = read_text(root, ("big.txt",))
        # The two-byte character straddles the cap: dropped, not mangled.
        assert result["truncated"] is True and result["content"] == "a" * 9
        assert result["size"] == 15

    def test_missing(self, root: Path) -> None:
        with pytest.raises(WorkspaceFileError) as exc:
            read_text(root, ("nope.md",))
        assert _code(exc) == "not_found"

    def test_a_directory_is_not_a_file(self, root: Path) -> None:
        with pytest.raises(WorkspaceFileError) as exc:
            read_text(root, ("documents",))
        assert _code(exc) == "not_a_file"
        with pytest.raises(WorkspaceFileError) as exc:
            read_text(root, ())
        assert _code(exc) == "not_a_file"

    @posix_only
    def test_a_symlink_to_outside_is_not_read(self, root: Path, outside: Path) -> None:
        os.symlink(outside, root / "link.txt")
        with pytest.raises(WorkspaceFileError) as exc:
            read_text(root, ("link.txt",))
        assert _code(exc) == "unsafe"

    @posix_only
    def test_a_symlink_to_inside_is_not_read_either(self, root: Path) -> None:
        os.symlink(root / "notes.txt", root / "alias.txt")
        with pytest.raises(WorkspaceFileError) as exc:
            read_text(root, ("alias.txt",))
        assert _code(exc) == "unsafe"

    @posix_only
    def test_a_file_under_a_symlinked_directory_is_not_read(
        self, root: Path, outside: Path
    ) -> None:
        os.symlink(outside.parent, root / "linkdir")
        with pytest.raises(WorkspaceFileError) as exc:
            read_text(root, ("linkdir", outside.name))
        assert _code(exc) == "unsafe"

    @posix_only
    def test_a_hardlink_is_not_read(self, root: Path, outside: Path) -> None:
        os.link(outside, root / "hard.txt")
        with pytest.raises(WorkspaceFileError) as exc:
            read_text(root, ("hard.txt",))
        assert _code(exc) == "unsafe"
        entry = next(e for e in list_directory(root, ())["entries"] if e["name"] == "hard.txt")
        assert entry["openable"] is False and entry["reason"] == "hardlink"

    @posix_only
    def test_a_fifo_is_refused_without_blocking(self, root: Path) -> None:
        os.mkfifo(root / "pipe.txt")
        with pytest.raises(WorkspaceFileError) as exc:
            read_text(root, ("pipe.txt",))
        assert _code(exc) == "unsafe"

    def test_the_sensitive_path_floor_applies(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wf, "_sensitive", lambda path: path.name == "notes.txt")
        with pytest.raises(WorkspaceFileError) as exc:
            read_text(root, ("notes.txt",))
        assert _code(exc) == "denied"
        assert read_text(root, ("MEMORY.md",))["previewable"] is True


class TestDownload:
    def test_image_and_pdf(self, root: Path) -> None:
        image = read_download(root, ("pic.png",))
        assert image.data == PNG and image.mime == "image/png" and image.size == len(PNG)
        assert stat_download(root, ("doc.pdf",)).mime == "application/pdf"
        assert stat_download(root, ("doc.pdf",)).data == b""

    def test_text_is_not_downloadable(self, root: Path) -> None:
        with pytest.raises(WorkspaceFileError) as exc:
            stat_download(root, ("notes.txt",))
        assert _code(exc) == "unsupported"

    def test_too_large(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wf, "MAX_IMAGE_BYTES", 8)
        for call in (stat_download, read_download):
            with pytest.raises(WorkspaceFileError) as exc:
                call(root, ("pic.png",))
            assert _code(exc) == "too_large"

    @posix_only
    def test_a_symlinked_image_is_not_served(self, root: Path, tmp_path: Path) -> None:
        target = tmp_path / "outside.png"
        target.write_bytes(PNG)
        os.symlink(target, root / "link.png")
        with pytest.raises(WorkspaceFileError) as exc:
            read_download(root, ("link.png",))
        assert _code(exc) == "unsafe"


# ---------------------------------------------------------------------------
# RPC
# ---------------------------------------------------------------------------


def _config(root: Path) -> GatewayConfig:
    return GatewayConfig(auth=AuthConfig(mode="token", token="secret"), workspace_dir=str(root))


def _ctx(
    config: GatewayConfig,
    *,
    conn_id: str = "conn-1",
    via: str | None = None,
    surface: ConnectionSurface = ConnectionSurface.CONTROL,
) -> RpcContext:
    return RpcContext(
        conn_id=conn_id,
        access=AccessContext(
            surface=surface,
            admitted=True,
            credential_verified=True,
            agent=AgentBinding(via=via) if via else None,
        ),
        config=config,
    )


async def _call(method: str, params: Any, ctx: RpcContext) -> Any:
    return await get_dispatcher().dispatch("r1", method, params, ctx)


@pytest.fixture(autouse=True)
def _clean_grants() -> None:
    get_download_grants().clear()


class TestRpc:
    @pytest.mark.asyncio
    async def test_list_root(self, root: Path) -> None:
        res = await _call("workspace.files.list", {}, _ctx(_config(root)))
        assert res.ok, res.error
        assert res.payload["agentId"] == "main"
        assert "MEMORY.md" in [e["name"] for e in res.payload["entries"]]
        assert res.payload["limits"]["entries"] == wf.MAX_DIR_ENTRIES

    @pytest.mark.asyncio
    async def test_read(self, root: Path) -> None:
        res = await _call(
            "workspace.files.read", {"path": "documents/Plan.md"}, _ctx(_config(root))
        )
        assert res.ok, res.error
        assert res.payload["content"] == "plan"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("path", "code"),
        [
            ("../outside-secret.txt", "workspace.invalid_path"),
            ("/etc/passwd", "workspace.invalid_path"),
            (".env", "workspace.denied"),
            ("server.pem", "workspace.denied"),
            ("nope.png", "workspace.not_found"),
        ],
    )
    async def test_refusals_carry_a_code(
        self, root: Path, outside: Path, path: str, code: str
    ) -> None:
        for method in ("workspace.files.read", "workspace.files.download"):
            res = await _call(method, {"path": path}, _ctx(_config(root)))
            assert not res.ok
            assert res.error.details == {"code": code}
            assert "TOP SECRET" not in str(res.error)

    @pytest.mark.asyncio
    async def test_another_agents_workspace_is_its_own_directory(self, root: Path) -> None:
        other = root / "agents" / "helper"
        other.mkdir(parents=True)
        (other / "SOUL.md").write_text("helper", encoding="utf-8")
        res = await _call("workspace.files.list", {"agentId": "helper"}, _ctx(_config(root)))
        assert res.ok, res.error
        assert [e["name"] for e in res.payload["entries"]] == ["SOUL.md"]
        # The agent id is an id, never a path out of the workspace.
        res = await _call("workspace.files.list", {"agentId": "../../.."}, _ctx(_config(root)))
        if res.ok:
            assert res.payload["agentId"] not in ("", "..", "../../..")
            assert "/" not in res.payload["agentId"] and ".." not in res.payload["agentId"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("via", ["token", "process", "window"])
    @pytest.mark.parametrize(
        "method", ["workspace.files.list", "workspace.files.read", "workspace.files.download"]
    )
    async def test_an_agent_is_refused(self, root: Path, via: str, method: str) -> None:
        res = await _call(method, {"path": "MEMORY.md"}, _ctx(_config(root), via=via))
        assert not res.ok and res.error.code == "agent.operator_required"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method", ["workspace.files.list", "workspace.files.read", "workspace.files.download"]
    )
    async def test_not_on_the_channel_surface(self, root: Path, method: str) -> None:
        ctx = _ctx(_config(root), surface=ConnectionSurface.CHANNEL)
        res = await _call(method, {"path": "MEMORY.md"}, ctx)
        assert not res.ok

    @pytest.mark.asyncio
    async def test_params_must_be_an_object(self, root: Path) -> None:
        res = await _call("workspace.files.read", ["MEMORY.md"], _ctx(_config(root)))
        assert not res.ok

    @pytest.mark.asyncio
    async def test_download_needs_a_socket(self, root: Path) -> None:
        res = await _call(
            "workspace.files.download", {"path": "pic.png"}, _ctx(_config(root), conn_id="http")
        )
        assert not res.ok

    @pytest.mark.asyncio
    async def test_download_refuses_text(self, root: Path) -> None:
        res = await _call("workspace.files.download", {"path": "notes.txt"}, _ctx(_config(root)))
        assert not res.ok and res.error.details == {"code": "workspace.unsupported"}


# ---------------------------------------------------------------------------
# Ticketed download route
# ---------------------------------------------------------------------------


def _client(config: GatewayConfig, alive: set[str] | None = None) -> TestClient:
    app = Starlette()
    live = alive if alive is not None else {"conn-1"}
    register_download_routes(app, config=config, connection_alive=lambda c: c in live)
    return TestClient(app)


async def _ticket(config: GatewayConfig, path: str) -> str:
    res = await _call("workspace.files.download", {"path": path}, _ctx(config))
    assert res.ok, res.error
    return str(res.payload["url"])


class TestRoute:
    @pytest.mark.asyncio
    async def test_serves_the_image_sandboxed(self, root: Path) -> None:
        config = _config(root)
        url = await _ticket(config, "pic.png")
        response = _client(config).get(url)
        assert response.status_code == 200
        assert response.content == PNG
        assert response.headers["content-type"] == "image/png"
        assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-disposition"].startswith("attachment")

    @pytest.mark.asyncio
    async def test_head(self, root: Path) -> None:
        config = _config(root)
        response = _client(config).head(await _ticket(config, "doc.pdf"))
        assert response.status_code == 200 and response.content == b""
        assert response.headers["content-type"] == "application/pdf"

    @pytest.mark.asyncio
    async def test_gone_with_the_connection(self, root: Path) -> None:
        config = _config(root)
        url = await _ticket(config, "pic.png")
        assert _client(config, alive=set()).get(url).status_code == 404

    @pytest.mark.asyncio
    async def test_bound_to_its_connection(self, root: Path) -> None:
        config = _config(root)
        url = await _ticket(config, "pic.png")
        foreign = url.replace("/conn-1/", "/conn-2/")
        assert _client(config, alive={"conn-1", "conn-2"}).get(foreign).status_code == 404

    @pytest.mark.asyncio
    async def test_rechecked_on_every_request(self, root: Path) -> None:
        config = _config(root)
        url = await _ticket(config, "pic.png")
        client = _client(config)
        assert client.get(url).status_code == 200
        (root / "pic.png").unlink()
        assert client.get(url).status_code == 404

    @posix_only
    @pytest.mark.asyncio
    async def test_a_file_swapped_for_a_link_is_not_served(self, root: Path, outside: Path) -> None:
        config = _config(root)
        url = await _ticket(config, "pic.png")
        (root / "pic.png").unlink()
        os.symlink(outside, root / "pic.png")
        response = _client(config).get(url)
        assert response.status_code == 404
        assert b"TOP SECRET" not in response.content

    @pytest.mark.asyncio
    async def test_no_thumbnail_variant(self, root: Path) -> None:
        config = _config(root)
        url = await _ticket(config, "pic.png")
        assert _client(config).get(url + "?variant=thumbnail").status_code == 400
