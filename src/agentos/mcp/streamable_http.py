"""MCP Streamable HTTP transport with optional OAuth 2.1 token persistence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from agentos import __version__
from agentos.env import trust_env as _trust_env
from agentos.mcp.client import MCPClient
from agentos.mcp.types import MCPServerConfig, MCPToolDef, MCPToolResult
from agentos.paths import state_dir as default_state_dir


class MCPDependencyError(RuntimeError):
    """Raised when Streamable HTTP is selected without the optional MCP SDK."""


class FileOAuthStorage:
    """Persist MCP OAuth credentials in a private, server-scoped JSON file."""

    def __init__(self, server_name: str, server_url: str, root: str | None = None) -> None:
        base = Path(root).expanduser() if root else default_state_dir()
        safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "-", server_name).strip("-") or "server"
        digest = hashlib.sha256(server_url.encode("utf-8")).hexdigest()[:12]
        self.path = base / "mcp" / f"{safe_name}-{digest}.oauth.json"
        self._tokens: Any = None
        self._client_info: Any = None
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return

        try:
            from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

            if payload.get("tokens"):
                self._tokens = OAuthToken.model_validate(payload["tokens"])
            if payload.get("client_info"):
                self._client_info = OAuthClientInformationFull.model_validate(
                    payload["client_info"]
                )
        except (ImportError, ValueError):
            self._tokens = None
            self._client_info = None

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        payload = {
            "tokens": self._tokens.model_dump(mode="json") if self._tokens else None,
            "client_info": (
                self._client_info.model_dump(mode="json") if self._client_info else None
            ),
        }
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.path)
            os.chmod(self.path, 0o600)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    async def get_tokens(self) -> Any:
        self._load()
        return self._tokens

    async def set_tokens(self, tokens: Any) -> None:
        self._load()
        self._tokens = tokens
        self._write()

    async def get_client_info(self) -> Any:
        self._load()
        return self._client_info

    async def set_client_info(self, client_info: Any) -> None:
        self._load()
        self._client_info = client_info
        self._write()

    async def is_authenticated(self) -> bool:
        tokens = await self.get_tokens()
        return bool(tokens and getattr(tokens, "access_token", None))

    def clear(self) -> None:
        self._tokens = None
        self._client_info = None
        self._loaded = True
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class MCPStreamableHTTPClient(MCPClient):
    """MCP SDK-backed Streamable HTTP client."""

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        redirect_handler: Callable[[str], Awaitable[None]] | None = None,
        callback_handler: Callable[[], Awaitable[tuple[str, str | None]]] | None = None,
    ) -> None:
        super().__init__(config)
        self._redirect_handler = redirect_handler
        self._callback_handler = callback_handler
        self._stack: AsyncExitStack | None = None
        self._session: Any = None

    def oauth_storage(self) -> FileOAuthStorage:
        if not self.config.url:
            raise ValueError("Streamable HTTP MCP server requires a URL")
        return FileOAuthStorage(
            self.config.name,
            self.config.url,
            self.config.state_dir,
        )

    async def connect(self) -> None:
        if not self.config.url:
            raise ValueError("Streamable HTTP MCP server requires a URL")

        try:
            from mcp.client.auth import OAuthClientProvider
            from mcp.client.session import ClientSession
            from mcp.client.streamable_http import streamable_http_client
            from mcp.shared.auth import OAuthClientMetadata
        except ImportError as exc:
            raise MCPDependencyError(
                'Streamable HTTP requires the optional dependency: uv sync --extra mcp'
            ) from exc

        auth = None
        if self.config.oauth:
            redirect_uri = self.config.oauth_redirect_uri or "http://127.0.0.1/"
            metadata = OAuthClientMetadata.model_validate(
                {
                    "redirect_uris": [redirect_uri],
                    "client_name": "AgentOS",
                    "software_version": __version__,
                }
            )
            auth = OAuthClientProvider(
                self.config.url,
                metadata,
                self.oauth_storage(),
                redirect_handler=self._redirect_handler,
                callback_handler=self._callback_handler,
            )

        stack = AsyncExitStack()
        try:
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(
                    auth=auth,
                    headers=self.config.headers,
                    timeout=httpx.Timeout(self.config.tool_timeout_seconds),
                    trust_env=_trust_env(),
                )
            )
            read_stream, write_stream, _ = await stack.enter_async_context(
                streamable_http_client(self.config.url, http_client=http_client)
            )
            session = await stack.enter_async_context(
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=self.config.tool_timeout_seconds),
                )
            )
            await session.initialize()
        except Exception:
            await stack.aclose()
            raise

        self._stack = stack
        self._session = session

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None

    async def list_tools(self) -> list[MCPToolDef]:
        if self._session is None:
            raise RuntimeError("MCP Streamable HTTP client is not connected")
        result = await self._session.list_tools()
        return [
            MCPToolDef(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.inputSchema,
            )
            for tool in result.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        if self._session is None:
            raise RuntimeError("MCP Streamable HTTP client is not connected")
        result = await self._session.call_tool(name, arguments)
        chunks: list[str] = []
        for block in result.content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                chunks.append(text)
                continue
            if hasattr(block, "model_dump_json"):
                chunks.append(block.model_dump_json())
        structured = getattr(result, "structuredContent", None)
        if not chunks and structured is not None:
            chunks.append(json.dumps(structured, ensure_ascii=False))
        return MCPToolResult(
            content="\n".join(chunks),
            is_error=bool(getattr(result, "isError", False)),
        )
