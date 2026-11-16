"""MCP Streamable HTTP transport with optional OAuth 2.1 token persistence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from agentos import __version__
from agentos.mcp.client import MCPSessionClient
from agentos.mcp.http import assert_supported_mcp_url, mcp_http_client
from agentos.mcp.types import MCPServerConfig
from agentos.paths import state_dir as default_state_dir


class MCPDependencyError(RuntimeError):
    """Raised when the required MCP SDK is missing from the installation."""


class MCPAuthorizationRequiredError(RuntimeError):
    """The server wants a browser sign-in, which only the ``mcp.oauth.start`` flow can run.

    The client that serves tools has no browser redirect to offer. Without a
    handler of its own the SDK raises ``No redirect handler provided``, which
    tells nobody what to do.
    """


class FileOAuthStorage:
    """Persist MCP OAuth credentials in a private, server-scoped JSON file."""

    def __init__(self, server_name: str, server_url: str, root: str | None = None) -> None:
        base = Path(root).expanduser() if root else default_state_dir()
        safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "-", server_name).strip("-") or "server"
        digest = hashlib.sha256(server_url.encode("utf-8")).hexdigest()[:12]
        self.path = base / "mcp" / f"{safe_name}-{digest}.oauth.json"
        self._tokens: Any = None
        self._client_info: Any = None
        # Wall-clock time the access token stops working. ``expires_in`` alone
        # is relative to an issue time the SDK never stores, so a provider that
        # loads these tokens back would treat them as valid forever.
        self._expires_at: float | None = None
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
            return
        self._expires_at = self._restore_expiry(payload.get("expires_at"))

    def _restore_expiry(self, stamp: Any) -> float | None:
        if isinstance(stamp, int | float) and not isinstance(stamp, bool):
            return float(stamp)
        lifetime = _token_lifetime(self._tokens)
        if lifetime is None:
            return None
        # Written before the stamp existed: the file was last saved when these
        # tokens arrived, so age them from there.
        try:
            return self.path.stat().st_mtime + lifetime
        except OSError:
            return None

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        payload = {
            "tokens": self._tokens.model_dump(mode="json") if self._tokens else None,
            "expires_at": self._expires_at,
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
        lifetime = _token_lifetime(tokens)
        self._expires_at = None if lifetime is None else time.time() + lifetime
        self._write()

    def token_expiry_time(self) -> float | None:
        """When the saved access token expires (epoch seconds), or None if it never does."""
        self._load()
        return self._expires_at if self._tokens else None

    def clear_tokens(self) -> None:
        """Forget the saved tokens but keep the client registration for the next sign-in."""
        self._load()
        if self._tokens is None:
            return
        self._tokens = None
        self._expires_at = None
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
        self._expires_at = None
        self._loaded = True
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _token_lifetime(tokens: Any) -> float | None:
    expires_in = getattr(tokens, "expires_in", None)
    if expires_in is None:
        return None
    try:
        return float(expires_in)
    except (TypeError, ValueError):
        return None


_provider_class: type | None = None


def _oauth_provider_class() -> type:
    """``OAuthClientProvider`` that keeps a loaded token's expiry.

    The SDK only learns when a token expires from a token response it handled
    itself; ``_initialize`` loads saved tokens with no expiry at all. A client
    that serves tools from tokens another client obtained therefore never
    refreshes them, and once the server rejects the stale token the SDK goes
    straight to a full browser sign-in, which that client cannot run. Restoring
    the saved expiry lets the SDK refresh ahead of time, as it does in-process.
    """
    global _provider_class
    if _provider_class is None:
        from mcp.client.auth import OAuthClientProvider

        class StoredExpiryOAuthClientProvider(OAuthClientProvider):
            async def _initialize(self) -> None:
                await super()._initialize()
                storage = self.context.storage
                if isinstance(storage, FileOAuthStorage):
                    self.context.token_expiry_time = storage.token_expiry_time()

        _provider_class = StoredExpiryOAuthClientProvider
    return _provider_class


class MCPStreamableHTTPClient(MCPSessionClient):
    """MCP SDK-backed Streamable HTTP client."""

    transport_label = "MCP Streamable HTTP"

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        redirect_handler: Callable[[str], Awaitable[None]] | None = None,
        callback_handler: Callable[[], Awaitable[tuple[str, str | None]]] | None = None,
    ) -> None:
        super().__init__(config)
        # The SDK refuses to start a sign-in unless both handlers exist, so the
        # serving client gets a pair whose redirect explains what to do.
        self._redirect_handler = redirect_handler or self._sign_in_required
        self._callback_handler = callback_handler or self._no_sign_in_callback

    async def _sign_in_required(self, _authorization_url: str) -> None:
        """Stand-in redirect for a client that has no browser to send anyone to.

        Reached only once a refresh failed or the server rejected the token
        outright, so the saved tokens are dropped: status then reports the
        server as signed out and the console offers sign-in instead of a
        connect that would fail the same way.
        """
        if self.config.url:
            self.oauth_storage().clear_tokens()
        raise MCPAuthorizationRequiredError(
            f"MCP server {self.config.name!r} needs to be signed in again: its saved "
            "OAuth session expired or was revoked. Sign in to it from Settings > MCP."
        )

    async def _no_sign_in_callback(self) -> tuple[str, str | None]:
        raise MCPAuthorizationRequiredError(
            f"MCP server {self.config.name!r} needs to be signed in again from Settings > MCP."
        )

    def oauth_storage(self) -> FileOAuthStorage:
        if not self.config.url:
            raise ValueError("Streamable HTTP MCP server requires a URL")
        return FileOAuthStorage(
            self.config.name,
            self.config.url,
            self.config.state_dir,
        )

    async def _open_session(self, stack: AsyncExitStack) -> Any:
        if not self.config.url:
            raise ValueError("Streamable HTTP MCP server requires a URL")
        # Checked here as well as inside ``mcp_http_client`` so an unusable URL
        # fails before the OAuth provider is built from it.
        assert_supported_mcp_url(self.config.url)

        try:
            from mcp.client.session import ClientSession
            from mcp.client.streamable_http import streamable_http_client
            from mcp.shared.auth import OAuthClientMetadata
        except ImportError as exc:
            raise MCPDependencyError(
                "Streamable HTTP support is unavailable because the MCP SDK is missing. "
                "Reinstall or upgrade AgentOS."
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
            auth = _oauth_provider_class()(
                self.config.url,
                metadata,
                self.oauth_storage(),
                redirect_handler=self._redirect_handler,
                callback_handler=self._callback_handler,
            )

        http_client = await stack.enter_async_context(
            mcp_http_client(
                self.config.url,
                auth=auth,
                headers=self.config.headers,
                timeout=httpx.Timeout(self.config.tool_timeout_seconds),
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
                message_handler=self._on_session_message,
            )
        )
        self._capture_initialize(await session.initialize())
        return session
