"""Web built-in tools: http_request, web_search."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

import httpx

from agentos.env import trust_env as _trust_env
from agentos.redact import credential_text_marker, secret_header_marker, secret_literal_marker
from agentos.sandbox.integration import sandboxed
from agentos.search.types import SearchProviderError, SearchResult
from agentos.tools.path_policy import reject_foreign_host_path
from agentos.tools.registry import tool
from agentos.tools.ssrf import assert_not_metadata_endpoint
from agentos.tools.ssrf_client import ssrf_guarded_client, validate_metadata_only_address
from agentos.tools.types import ToolError, UnsupportedURLSchemeError, current_tool_context


def _validate_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsupportedURLSchemeError(url)
    # Cloud metadata endpoints hand out instance credentials to anything that
    # can reach them, which makes them the first stop for an SSRF payload and
    # never a legitimate agent target. Ordinary private addresses stay
    # reachable — unlike web_fetch, http_request is the tool people point at a
    # local dev server on purpose.
    assert_not_metadata_endpoint(url)


_TEXT_BODY_LIMIT = 10_000
_BINARY_BODY_LIMIT = 1_000_000
# Hard ceiling on the number of response bytes http_request will buffer into
# memory, independent of the display cap (_BINARY_BODY_LIMIT / _TEXT_BODY_LIMIT).
# Those caps only truncate what the model sees; without a download cap, a single
# unbounded response body (chunked encoding with no content-length, or a lying
# content-length) is read fully into RAM via response.content, so one
# attacker-influenced URL (search results, links inside fetched pages, user
# input) can exhaust the process. 1 MiB covers every realistic response; the
# display cap then decides how much of that is returned.
_DOWNLOAD_LIMIT_BYTES = 1_000_000
_DOWNLOAD_LIMIT_ENV = "AGENTOS_HTTP_DOWNLOAD_LIMIT"
_STREAM_CHUNK_BYTES = 65_536
_FETCH_DIR_NAME = ".fetch"


def _sensitive_body_marker(body: str | None) -> str | None:
    """Return a marker when *body* carries credential material.

    Delegates to :func:`agentos.redact.secret_literal_marker`, which matches on
    credential *values* — a PEM block, a vendor-prefixed key, a DSN password —
    rather than on field names. Names cannot carry this decision: in a web3
    payload ``sellToken`` is an asset and ``x-api-key`` is how the API
    authenticates, while a pasted key announces itself in neither.
    """
    return secret_literal_marker(body)


def _sensitive_url_marker(url: str) -> str | None:
    parsed = urlparse(url)
    # URL userinfo is a legitimate credential carrier (RFC 3986), and the
    # HTTP client underneath converts it into an ``Authorization: Basic``
    # header on the wire — so a credential placed there egresses to whatever
    # host the URL names without ever appearing in the path or query. Check
    # both components percent-decoded: the client decodes userinfo before
    # sending, so ``sk%2Dant-…`` reaches the wire as ``sk-ant-…``.
    for part in (parsed.username, parsed.password):
        if part and secret_literal_marker(unquote(part)) is not None:
            return "sensitive_url_userinfo"
    for segment in parsed.path.split("/"):
        if secret_literal_marker(unquote(segment)) is not None:
            return "sensitive_url_path"
    if not parsed.query:
        return None
    for _key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if secret_literal_marker(value) is not None:
            return "sensitive_query"
    return None


def _sensitive_headers_marker(headers: dict[str, str] | None) -> str | None:
    return secret_header_marker(headers)


def _sensitive_body_block(tool_name: str, marker: str) -> str:
    payload = {
        "status": "blocked",
        "reason": "sensitive_payload",
        "tool": tool_name,
        "sensitive_payload": marker,
        "message": (
            f"Refusing to send credential material ({marker}) over the wire. "
            "Reference the value instead of pasting it — a shell command can "
            "use $NAME, and a skill that declares the variable under "
            "metadata.requires.env gets it in the child environment without "
            "the literal ever entering this transcript. Set "
            "AGENTOS_SENSITIVE_PAYLOAD_DISABLED=1 before starting AgentOS to "
            "turn this check off entirely."
        ),
        "retryable": False,
    }
    return json.dumps(payload, ensure_ascii=False)


def _is_text_response_content_type(content_type: str) -> bool:
    normalized = content_type.lower().split(";", 1)[0].strip()
    if normalized.startswith("text/"):
        return True
    return (
        normalized in {"application/json", "application/xml", "application/xhtml+xml"}
        or normalized.endswith("+json")
        or normalized.endswith("+xml")
        or "json" in normalized
        or "xml" in normalized
    )


def _resolve_download_limit_bytes() -> int:
    """Resolve the hard download cap from env or the built-in default."""
    raw = os.environ.get(_DOWNLOAD_LIMIT_ENV, "").strip()
    if not raw:
        return _DOWNLOAD_LIMIT_BYTES
    try:
        value = int(raw)
    except ValueError:
        return _DOWNLOAD_LIMIT_BYTES
    return (
        min(value, _DOWNLOAD_LIMIT_BYTES) if value >= _STREAM_CHUNK_BYTES else _DOWNLOAD_LIMIT_BYTES
    )


def _fetch_workspace_dir() -> Path:
    ctx = current_tool_context.get()
    if ctx is not None and ctx.workspace_dir:
        return Path(ctx.workspace_dir).expanduser().resolve()
    return Path.cwd().resolve()


def _fetch_root() -> Path:
    return (_fetch_workspace_dir() / _FETCH_DIR_NAME).resolve()


def _resolve_fetch_output_path(digest: str, output_path: str | None) -> Path:
    if output_path is None:
        root = _fetch_root()
        return root / f"{digest}.bin"

    raw = output_path.strip()
    if not raw:
        raise ToolError("output_path must not be empty")

    reject_foreign_host_path(raw, platform=os.name)
    root = _fetch_root()
    requested = Path(raw).expanduser()
    if requested.drive and not requested.is_absolute():
        raise ToolError("output_path must be an absolute path or a relative .fetch path")
    candidate = requested if requested.is_absolute() else root / requested
    resolved = candidate.resolve(strict=False)
    if resolved == root or not resolved.is_relative_to(root):
        raise ToolError(f"output_path must stay inside {root}")
    if resolved.exists() and resolved.is_dir():
        raise ToolError("output_path must name a file, not a directory")
    return resolved


def _save_http_response_body(raw_body: bytes, output_path: str | None) -> tuple[Path, str]:
    digest = hashlib.sha256(raw_body).hexdigest()
    path = _resolve_fetch_output_path(digest, output_path)
    if output_path is not None and path.exists():
        raise ToolError("output_path already exists")
    if output_path is None and path.exists():
        return path, digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw_body)
    return path, digest


@tool(
    name="http_request",
    description=(
        "Make an HTTP request. Use output_path to save a response under the workspace "
        ".fetch directory; otherwise responses are returned as bounded metadata."
    ),
    params={
        "url": {"type": "string", "description": "HTTP or HTTPS URL."},
        "method": {"type": "string", "description": "HTTP method (default: GET)."},
        "headers": {
            "type": "object",
            "description": "Request headers.",
            "additionalProperties": {"type": "string"},
        },
        "body": {"type": "string", "description": "Request body (for POST/PUT/PATCH)."},
        "timeout": {"type": "number", "description": "Request timeout in seconds (default 30)."},
        "output_path": {
            "type": "string",
            "description": "Optional file name/path inside the workspace .fetch directory.",
        },
    },
    required=["url"],
    result_budget_class="external",
)
@sandboxed(
    kind="network.http",
    argv_factory=lambda a: (
        "http_request",
        str(a.get("method", "GET")).upper(),
        str(a.get("url", "")),
        str(a.get("output_path", "")),
    ),
    record_payload=False,
)
async def http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    timeout: float = 30.0,
    output_path: str | None = None,
) -> str:
    _validate_http_url(url)
    marker = _sensitive_url_marker(url)
    if marker is not None:
        return _sensitive_body_block("http_request", marker)
    marker = _sensitive_headers_marker(headers)
    if marker is not None:
        return _sensitive_body_block("http_request", marker)
    method_upper = method.upper()
    # Scan whatever body is present rather than only the methods that
    # conventionally carry one: DELETE and even GET accept a body, and an
    # exfiltration path is not going to pick a method out of politeness.
    marker = _sensitive_body_marker(body)
    if marker is not None:
        return _sensitive_body_block("http_request", marker)

    try:
        import httpx
    except ImportError:
        return "[error] httpx not installed. Run: pip install httpx"

    content: bytes | None = body.encode() if body else None

    # Metadata-only policy at connect time: http_request keeps reaching
    # localhost and LAN services on purpose, but a rebinding domain must not be
    # able to swap a public answer for the instance-credential endpoint between
    # the URL check and the socket.
    async with ssrf_guarded_client(
        timeout=timeout,
        trust_env=_trust_env(),
        validator=validate_metadata_only_address,
    ) as client:
        response = await client.send(
            client.build_request(
                method=method_upper,
                url=url,
                headers=headers or {},
                content=content,
            ),
            stream=True,
        )
        try:
            # Stream the body with a hard byte ceiling so an unbounded response
            # can never be buffered fully into memory; the display caps
            # (_BINARY_BODY_LIMIT / _TEXT_BODY_LIMIT) only decide what is
            # returned. A timeout bounds time, not bytes: on a fast pipe
            # gigabytes arrive inside the window, so one attacker-influenced
            # URL can OOM the process.
            #
            # The whole read MUST stay inside this ``async with`` block:
            # ``AsyncClient.__aexit__`` closes the transport pool, and once it
            # does, ``response.aiter_bytes`` raises ``httpx.ReadError`` because
            # the underlying socket is gone. Reviewer #509 caught this as a
            # 100% production failure — every real request raised ReadError
            # after the previous layout exited the block before iterating.
            download_limit = _resolve_download_limit_bytes()
            total = 0
            chunks: list[bytes] = []
            download_capped = False
            stream_truncated = False
            try:
                async for chunk in response.aiter_bytes(_STREAM_CHUNK_BYTES):
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= download_limit:
                        download_capped = True
                        break
            except httpx.RemoteProtocolError:
                # Server dropped the connection mid-body (e.g. truncated
                # chunked response). Return what we got rather than crashing
                # the whole tool call.
                stream_truncated = True
            raw_body = b"".join(chunks)
            # Snapshot response metadata while the connection is still open so
            # downstream consumers don't depend on the closed transport.
            status_code = response.status_code
            response_url = str(response.url)
            response_headers = dict(response.headers)
            response_encoding = response.encoding or "utf-8"
            content_type = response_headers.get("content-type", "")
        finally:
            await response.aclose()

    from agentos.safety.injection_guard import wrap_untrusted_boundary

    is_text = _is_text_response_content_type(content_type)
    should_save = output_path is not None

    if should_save:
        saved_path, digest = _save_http_response_body(raw_body, output_path)
        preview = (
            wrap_untrusted_boundary(
                raw_body[:_TEXT_BODY_LIMIT].decode(response_encoding, "replace"),
                response_url,
            )
            if is_text
            else None
        )
        result = {
            "status": status_code,
            "url": response_url,
            "headers": response_headers,
            "content_type": content_type,
            "body": None,
            "body_base64": None,
            "body_truncated": stream_truncated,
            "body_base64_truncated": download_capped or stream_truncated,
            "body_saved": True,
            "body_omitted_reason": "saved_to_file",
            "body_preview": preview,
            "path": str(saved_path),
            "size": len(raw_body),
            "sha256": digest,
            "download_capped": download_capped,
        }
        return json.dumps(result, ensure_ascii=False, indent=2)

    capped = raw_body[:_BINARY_BODY_LIMIT]
    body_base64 = base64.b64encode(capped).decode("ascii")
    body_base64_truncated = (
        download_capped or stream_truncated or len(raw_body) > _BINARY_BODY_LIMIT
    )
    if is_text:
        text_body = raw_body.decode(response_encoding, "replace")
        body = wrap_untrusted_boundary(text_body[:_TEXT_BODY_LIMIT], response_url)
        body_truncated = download_capped or stream_truncated or len(text_body) > _TEXT_BODY_LIMIT
    else:
        body = None
        body_truncated = False

    result = {
        "status": status_code,
        "url": response_url,
        "headers": response_headers,
        "content_type": content_type,
        "body": body,
        "body_base64": body_base64,
        "body_truncated": body_truncated,
        "body_base64_truncated": body_base64_truncated,
        "body_saved": False,
        "path": None,
        "size": len(raw_body),
        "sha256": hashlib.sha256(raw_body).hexdigest(),
        "download_capped": download_capped,
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


# Active search provider name — set during boot
_active_provider: str = "duckduckgo"
_active_max_results: int = 5
_active_search_proxy: str = ""
_active_search_api_key: str = ""
_active_search_use_env_proxy: bool = False
_active_search_fallback_policy: str = "off"
_active_search_diagnostics: bool = False


def configure_search(
    provider_name: str,
    max_results: int = 5,
    *,
    api_key: str = "",
    proxy: str = "",
    use_env_proxy: bool = False,
    fallback_policy: str = "off",
    diagnostics: bool = False,
) -> None:
    global _active_provider, _active_max_results, _active_search_proxy
    global _active_search_api_key, _active_search_use_env_proxy, _active_search_fallback_policy
    global _active_search_diagnostics
    _active_provider = provider_name
    _active_max_results = max_results
    _active_search_api_key = api_key.strip()
    _active_search_proxy = proxy.strip()
    _active_search_use_env_proxy = bool(use_env_proxy)
    _active_search_fallback_policy = (
        fallback_policy if fallback_policy in {"off", "network"} else "off"
    )
    _active_search_diagnostics = bool(diagnostics)


def reset_search_runtime() -> None:
    """Restore process-wide search configuration to boot defaults."""
    configure_search("duckduckgo")


def get_active_provider() -> str:
    return _active_provider


def is_search_api_key_configured(provider_name: str | None = None) -> bool:
    provider = provider_name or _active_provider
    if provider == _active_provider and _active_search_api_key:
        return True
    try:
        from agentos.search.registry import get_provider_spec

        spec = get_provider_spec(provider)
    except Exception:
        return False
    return bool(spec.env_key and os.environ.get(spec.env_key))


def get_search_proxy() -> str:
    return _active_search_proxy


def get_search_use_env_proxy() -> bool:
    return _active_search_use_env_proxy


def get_search_fallback_policy() -> str:
    return _active_search_fallback_policy


def get_search_diagnostics() -> bool:
    return _active_search_diagnostics


def _format_search_error(provider_name: str, exc: Exception) -> tuple[str, str]:
    error_class = type(exc).__name__
    raw = str(exc).strip()
    if raw:
        return error_class, raw
    if error_class == "ConnectTimeout":
        return (
            error_class,
            (
                f"{provider_name} search request timed out. Configure search_proxy "
                "or switch search_provider to duckduckgo."
            ),
        )
    return error_class, f"{provider_name} search failed with {error_class}."


def _search_provider_kwargs(provider_name: str) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "proxy": _active_search_proxy,
        "use_env_proxy": _active_search_use_env_proxy,
    }
    if provider_name in {"brave", "tavily"} and _active_search_api_key:
        kwargs["api_key"] = _active_search_api_key
    if _active_search_diagnostics or provider_name == "duckduckgo":
        kwargs["diagnostics"] = _active_search_diagnostics
    return kwargs


def _ensure_builtin_search_providers() -> None:
    import agentos.search.providers.brave  # noqa: F401
    import agentos.search.providers.duckduckgo  # noqa: F401
    import agentos.search.providers.tavily  # noqa: F401


def _search_success_payload(payload: dict) -> dict:
    result = dict(payload)
    result["ok"] = True
    if "fallback_from" in result:
        result["fallbackFrom"] = result["fallback_from"]
    return result


def _search_failure_payload(payload: dict, *, retryable: bool = False) -> dict:
    result = dict(payload)
    message = str(result.get("error") or "")
    error_kind = str(result.get("error_kind") or "unknown")
    error_class = str(result.get("error_class") or "")
    result["ok"] = False
    result["errorMessage"] = message
    result["error"] = {
        "kind": error_kind,
        "class": error_class,
        "message": message,
        "retryable": retryable,
    }
    return result


def search_runtime_status(provider_name: str | None = None) -> dict:
    from agentos.search.registry import get_provider, get_provider_spec

    _ensure_builtin_search_providers()
    provider = provider_name or _active_provider
    spec = get_provider_spec(provider)
    api_key_configured = is_search_api_key_configured(provider)
    configured = (not spec.requires_api_key) or api_key_configured
    error: str | None = None
    buildable = False
    try:
        get_provider(provider, **_search_provider_kwargs(provider))
        buildable = True
    except Exception as exc:  # noqa: BLE001 - diagnostic surface
        error = str(exc)
    return {
        "activeProvider": _active_provider,
        "provider": provider,
        "configured": configured,
        "runtimeSupported": spec.runtime_supported,
        "requiresApiKey": spec.requires_api_key,
        "apiKeyConfigured": api_key_configured,
        "maxResults": _active_max_results,
        "proxyConfigured": bool(_active_search_proxy),
        "useEnvProxy": bool(_active_search_use_env_proxy),
        "fallbackPolicy": _active_search_fallback_policy,
        "diagnostics": bool(_active_search_diagnostics),
        "buildable": buildable,
        "error": error,
    }


async def run_web_search_payload(
    query: str,
    max_results: int | None = None,
    *,
    provider_name: str | None = None,
) -> dict:
    from agentos.search.registry import get_provider

    _ensure_builtin_search_providers()
    provider_name = provider_name or _active_provider
    # A search query goes to a third party that has no business with any
    # credential in it, so this boundary uses the broader check: unlike an
    # authenticated API call, there is no reading of ``API_KEY=<value>`` that
    # makes it a legitimate thing to search for.
    marker = credential_text_marker(query)
    if marker is not None:
        return _search_failure_payload(
            {
                "query": "[redacted]",
                "provider": provider_name,
                "results": [],
                "error_class": "SensitiveInput",
                "error": _sensitive_body_block("web_search", marker),
                "error_kind": "invalid_request",
            },
            retryable=False,
        )

    limit = max_results or _active_max_results
    attempts: list[dict[str, str]] | None = [] if _active_search_diagnostics else None
    try:
        provider = get_provider(
            provider_name,
            **_search_provider_kwargs(provider_name),
        )
        results = await provider.search(query, max_results=limit)
        if attempts is not None:
            attempts.append({"provider": provider_name, "status": "success"})
        return _search_success_payload(_search_payload(query, provider_name, results))
    except Exception as exc:
        classified = _classify_search_error(provider_name, exc)
        if attempts is not None:
            attempts.append(
                {
                    "provider": provider_name,
                    "status": "error",
                    "error_kind": classified.kind if classified else "unknown",
                }
            )

        should_fallback = (
            _active_search_fallback_policy == "network"
            and provider_name != "duckduckgo"
            and classified is not None
            and classified.kind in {"timeout", "network"}
        )
        if should_fallback:
            try:
                fallback_provider = get_provider(
                    "duckduckgo",
                    **_search_provider_kwargs("duckduckgo"),
                )
                results = await fallback_provider.search(query, max_results=limit)
                if attempts is not None:
                    attempts.append({"provider": "duckduckgo", "status": "success"})
                return _search_success_payload(
                    _search_payload(
                        query,
                        "duckduckgo",
                        fallback_from=provider_name,
                        attempts=attempts,
                        results=results,
                    )
                )
            except Exception as fallback_exc:
                if attempts is not None:
                    fallback_classified = _classify_search_error("duckduckgo", fallback_exc)
                    attempts.append(
                        {
                            "provider": "duckduckgo",
                            "status": "error",
                            "error_kind": (
                                fallback_classified.kind if fallback_classified else "unknown"
                            ),
                        }
                    )

        return _search_failure_payload(
            _search_error_payload(query, provider_name, exc, attempts=attempts),
            retryable=bool(classified and classified.retryable),
        )


def _classify_search_error(provider_name: str, exc: Exception) -> SearchProviderError | None:
    if isinstance(exc, SearchProviderError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return SearchProviderError(
            provider=provider_name,
            kind="timeout",
            message=str(exc) or "Search request timed out.",
            retryable=True,
        )
    if isinstance(exc, httpx.NetworkError):
        return SearchProviderError(
            provider=provider_name,
            kind="network",
            message=str(exc) or "Search network request failed.",
            retryable=True,
        )
    return None


def _search_payload(
    query: str,
    provider_name: str,
    results: list[SearchResult],
    *,
    fallback_from: str = "",
    attempts: list[dict[str, str]] | None = None,
) -> dict:
    from agentos.safety.injection_guard import wrap_untrusted_boundary

    def _build_result(r: SearchResult) -> dict:
        source_url = r.url or "unknown-source"
        return {
            "title": wrap_untrusted_boundary(r.title, source_url),
            "url": r.url,
            "snippet": wrap_untrusted_boundary(r.snippet, source_url),
            "source": r.source or provider_name,
        }

    payload = {
        "query": query,
        "provider": provider_name,
        "results": [_build_result(r) for r in results],
    }
    if fallback_from:
        payload["fallback_from"] = fallback_from
    if attempts is not None:
        payload["attempts"] = attempts
    return payload


def _search_error_payload(
    query: str,
    provider_name: str,
    exc: Exception,
    *,
    attempts: list[dict[str, str]] | None = None,
) -> dict:
    error_class, error_message = _format_search_error(provider_name, exc)
    payload: dict[str, Any] = {
        "query": query,
        "provider": provider_name,
        "results": [],
        "error_class": error_class,
        "error": error_message,
    }
    classified = _classify_search_error(provider_name, exc)
    if classified is not None:
        payload["error_kind"] = classified.kind
    if attempts is not None:
        payload["attempts"] = attempts
    return payload


@tool(
    name="web_search",
    description="Search the web and return results with titles, URLs, and snippets.",
    params={
        "query": {"type": "string", "description": "Search query."},
        "max_results": {
            "type": "integer",
            "description": "Maximum number of results to return.",
        },
    },
    required=["query"],
    result_budget_class="external",
)
@sandboxed(
    kind="web.fetch",
    argv_factory=lambda a: ("web_search", str(a.get("query", "")), str(a.get("max_results", ""))),
    record_payload=False,
)
async def web_search(query: str, max_results: int | None = None) -> str:
    payload = await run_web_search_payload(query, max_results)
    tool_payload = dict(payload)
    tool_payload.pop("ok", None)
    tool_payload.pop("fallbackFrom", None)
    tool_payload.pop("errorMessage", None)
    if isinstance(tool_payload.get("error"), dict):
        tool_payload["error"] = tool_payload["error"].get("message", "")
    return json.dumps(tool_payload, ensure_ascii=False, indent=2)
