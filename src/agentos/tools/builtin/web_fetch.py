"""web_fetch built-in tool: fetch a URL and extract readable content."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from urllib.parse import urljoin

import httpx
import structlog
from cachetools import TTLCache

from agentos.env import trust_env as _trust_env
from agentos.result_budget import (
    DEFAULT_TOOL_RUN_BUDGET_POLICY,
    ToolRunBudgetPolicy,
)
from agentos.sandbox.integration import sandboxed
from agentos.tools.registry import tool
from agentos.tools.ssrf import validate_http_url_for_fetch
from agentos.tools.ssrf_client import ssrf_guarded_client
from agentos.tools.types import SSRFBlockedError, current_tool_context

log = structlog.get_logger(__name__)

# 15-minute cache keyed by (url, extract_mode)
_cache: TTLCache = TTLCache(maxsize=256, ttl=900)

# Escalate to Firecrawl when local readability returns None or content below
# this threshold. Keeps free local path as the default, reserves the paid SaaS
# path for JS-heavy / anti-bot pages where readability struggles.
_READABILITY_ESCALATION_MIN_CHARS = 200

_DEFAULT_HEADERS: dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
}

_UA_PRIMARY = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_UA_FALLBACK = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)

_TRANSIENT_STATUSES: frozenset[int] = frozenset({403, 408, 425, 429, 500, 502, 503, 504})
_RETRY_DELAY_SECONDS = 0.25
_WEB_FETCH_DEFAULT_MAX_CHARS = 20_000
_WEB_FETCH_MAX_CHARS_ENV = "AGENTOS_WEB_FETCH_MAX_CHARS"
_MAX_REDIRECTS = 5

# Hard ceiling on the number of response bytes web_fetch will buffer into
# memory, independent of the display cap (max_chars). max_chars only truncates
# what the model sees; without a download cap, a single unbounded response
# body (chunked encoding with no content-length, or a lying content-length) is
# read fully into RAM via response.text, so one malicious URL can exhaust the
# process. 1 MiB covers every realistic page; the display cap then decides how
# much of that is returned.
_WEB_FETCH_DOWNLOAD_LIMIT_BYTES = 1_048_576
_WEB_FETCH_DOWNLOAD_LIMIT_ENV = "AGENTOS_WEB_FETCH_DOWNLOAD_LIMIT"
_STREAM_CHUNK_BYTES = 65_536


def _check_ssrf(url: str) -> None:
    """Raise ValueError if the URL resolves to a private/internal address."""
    validate_http_url_for_fetch(url)


def _html_to_markdown(html: str) -> str:
    import html2text

    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = False
    h.body_width = 0
    return h.handle(html)


def _markdown_to_text(markdown: str) -> str:
    """Strip markdown formatting to plain text, preserving paragraph breaks.

    ``html2text`` parses HTML, not Markdown (issue #2482): feeding it the
    markdown we already extracted treats its blank-line paragraph breaks as
    ordinary whitespace and folds every paragraph into one run-on line,
    while leaving markdown syntax (``**bold**``, ``[text](url)``,
    ``# Heading``) untranslated.

    A hand-rolled regex pass over the same text isn't a safe replacement
    either: matching something like ``\\*{1,3}(.*?)\\*{1,3}`` as "emphasis"
    also matches an ordinary multiplication sign (``5 * 3``), silently
    swallowing everything up to the next unrelated asterisk in the text.
    What counts as an emphasis delimiter (flanking whitespace/punctuation,
    paired vs. stray markers) is exactly what a real CommonMark parser
    already gets right, so this walks markdown-it's token stream instead of
    re-deriving those rules with regex.
    """
    if not markdown:
        return ""
    from markdown_it import MarkdownIt

    parser = MarkdownIt("commonmark")
    return _render_tokens_as_text(parser.parse(markdown)).strip()


def _render_tokens_as_text(tokens: list[Any]) -> str:
    """Render a markdown-it token stream as plain text.

    Block-level content (paragraphs, headings, list items, code blocks) is
    joined with blank lines; inline formatting markers (emphasis, links,
    images) are dropped, keeping only their literal text.
    """
    # Each block is (text, is_list_item): adjacent list items are joined by a
    # single newline, everything else by a blank line, so a list reads as a
    # tight list rather than being spaced out like separate paragraphs.
    blocks: list[tuple[str, bool]] = []
    current: list[str] = []
    list_stack: list[dict[str, Any]] = []
    pending_prefix = ""
    in_list_item = False

    def flush() -> None:
        nonlocal pending_prefix, in_list_item
        text = "".join(current).strip()
        current.clear()
        if text:
            blocks.append((pending_prefix + text, in_list_item))
        pending_prefix = ""
        in_list_item = False

    def walk_inline(children: list[Any]) -> None:
        for child in children:
            if child.type in ("text", "code_inline", "html_inline"):
                current.append(child.content)
            elif child.type == "softbreak":
                current.append(" ")
            elif child.type == "hardbreak":
                current.append("\n")
            elif child.children:
                walk_inline(child.children)

    for token in tokens:
        if token.type == "inline":
            walk_inline(token.children or [])
        elif token.type in (
            "paragraph_close",
            "heading_close",
            "blockquote_close",
            "list_item_close",
        ):
            flush()
        elif token.type in ("fence", "code_block", "html_block"):
            flush()
            content = token.content.rstrip("\n")
            if content:
                blocks.append((content, False))
        elif token.type == "bullet_list_open":
            list_stack.append({"ordered": False})
        elif token.type == "ordered_list_open":
            start = token.attrGet("start")
            list_stack.append({"ordered": True, "next": start if isinstance(start, int) else 1})
        elif token.type in ("bullet_list_close", "ordered_list_close"):
            if list_stack:
                list_stack.pop()
        elif token.type == "list_item_open" and list_stack:
            top = list_stack[-1]
            if top["ordered"]:
                pending_prefix = f"{top['next']}. "
                top["next"] += 1
            else:
                pending_prefix = "- "
            in_list_item = True

    flush()
    rendered: list[str] = []
    prev_is_list_item = False
    for text, is_list_item in blocks:
        if rendered:
            rendered.append("\n" if is_list_item and prev_is_list_item else "\n\n")
        rendered.append(text)
        prev_is_list_item = is_list_item
    return "".join(rendered)


async def _try_firecrawl(url: str, api_key: str) -> tuple[str, str] | None:
    """Try Firecrawl API. Returns (content, extractor) or None."""
    try:
        async with httpx.AsyncClient(timeout=30.0, trust_env=_trust_env()) as client:
            resp = await client.post(
                "https://api.firecrawl.dev/v1/scrape",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"url": url, "formats": ["markdown"]},
            )
            data = resp.json()
            if data.get("success"):
                return data["data"]["markdown"], "firecrawl"
            log.warning("web_fetch.firecrawl_unsuccessful", url=url, response=data)
    except Exception as exc:
        log.warning("web_fetch.firecrawl_error", url=url, error=str(exc))
    return None


def _try_readability(html: str) -> tuple[str, str, str] | None:
    """Try readability-lxml. Returns (title, content_markdown, extractor) or None."""
    try:
        from readability import Document

        doc = Document(html)
        title = doc.title()
        summary_html = doc.summary()
        content = _html_to_markdown(summary_html)
        return title, content, "readability"
    except Exception:
        return None


def _try_html2text(html: str) -> tuple[str, str, str]:
    """html2text fallback — always succeeds."""
    content = _html_to_markdown(html)
    return "", content, "html2text"


def _resolve_default_max_chars() -> int:
    """Return default output cap for omitted max_chars."""
    raw = os.environ.get(_WEB_FETCH_MAX_CHARS_ENV, "").strip()
    if not raw:
        return _WEB_FETCH_DEFAULT_MAX_CHARS
    try:
        value = int(raw)
    except ValueError:
        return _WEB_FETCH_DEFAULT_MAX_CHARS
    return value if value >= 100 else _WEB_FETCH_DEFAULT_MAX_CHARS


def _resolve_download_limit_bytes() -> int:
    """Resolve the hard download cap from env or the built-in default."""
    raw = os.environ.get(_WEB_FETCH_DOWNLOAD_LIMIT_ENV, "").strip()
    if not raw:
        return _WEB_FETCH_DOWNLOAD_LIMIT_BYTES
    try:
        value = int(raw)
    except ValueError:
        return _WEB_FETCH_DOWNLOAD_LIMIT_BYTES
    return value if value >= 65_536 else _WEB_FETCH_DOWNLOAD_LIMIT_BYTES


def _resolve_effective_max_chars(max_chars: int | None) -> int | None:
    """Resolve explicit max_chars or the default cap for omitted values.

    An explicit value below the documented minimum (100) is clamped up to
    it, not dropped to "no cap" — ``_apply_max_chars`` treats ``None`` as
    unlimited, so returning ``None`` here for e.g. ``max_chars=1`` used to
    mean the *smallest* request came back with the *entire* untruncated
    page, the opposite of what the caller asked for (see issue #1400).
    """
    max_allowed = _active_run_budget_policy().max_single_fetch_chars
    if max_chars is not None:
        clamped = max(max_chars, 100)
        return min(clamped, max_allowed) if max_allowed is not None else clamped
    default = _resolve_default_max_chars()
    return min(default, max_allowed) if max_allowed is not None else default


def _active_run_budget_policy() -> ToolRunBudgetPolicy:
    ctx = current_tool_context.get()
    policy = getattr(ctx, "tool_run_budget_policy", None) if ctx is not None else None
    if isinstance(policy, ToolRunBudgetPolicy):
        return policy
    return DEFAULT_TOOL_RUN_BUDGET_POLICY


@tool(
    name="web_fetch",
    description=(
        "Fetch a URL and extract readable content as markdown or plain text. "
        "Uses a multi-extractor pipeline (readability → Firecrawl escalation → "
        "html2text). Includes SSRF protection and a 15-minute response cache."
    ),
    params={
        "url": {
            "type": "string",
            "description": "HTTP or HTTPS URL to fetch.",
        },
        "extract_mode": {
            "type": "string",
            "description": 'Extraction format: "markdown" (default) or "text".',
            "enum": ["markdown", "text"],
        },
        "max_chars": {
            "type": "integer",
            "description": (
                "Maximum characters to return (minimum 100). "
                "Defaults to 20,000 when omitted; override default with "
                "AGENTOS_WEB_FETCH_MAX_CHARS."
            ),
            "minimum": 100,
        },
    },
    required=["url"],
    result_budget_class="external",
)
@sandboxed(
    kind="web.fetch",
    argv_factory=lambda a: (
        "web_fetch",
        str(a.get("url", "")),
        str(a.get("extract_mode", "markdown")),
    ),
    record_payload=False,
)
async def web_fetch(
    url: str,
    extract_mode: str = "markdown",
    max_chars: int | None = None,
) -> str:
    # --- SSRF guard ---
    _check_ssrf(url)
    from agentos.tools.builtin.web import _sensitive_body_block, _sensitive_url_marker

    marker = _sensitive_url_marker(url)
    if marker is not None:
        return _sensitive_body_block("web_fetch", marker)

    effective_max_chars = _resolve_effective_max_chars(max_chars)

    # --- Cache lookup ---
    cache_key = (url, extract_mode)
    if cache_key in _cache:
        cached: dict[str, Any] = dict(_cache[cache_key])
        return json.dumps(_apply_max_chars(cached, effective_max_chars), ensure_ascii=False)

    # --- Fetch ---
    title = ""
    content_type = ""
    final_url = url
    status = 0
    raw_html = ""
    body_truncated = False

    async def _do_fetch(user_agent: str) -> tuple[int, str, str, str, bool]:
        headers = dict(_DEFAULT_HEADERS)
        headers["User-Agent"] = user_agent
        # The guarded client re-validates at connect time, so the address this
        # socket dials is the one _check_ssrf approved — a rebinding domain
        # cannot answer differently for the guard and for the connection.
        async with ssrf_guarded_client(
            timeout=30.0,
            follow_redirects=False,
            trust_env=_trust_env(),
            headers=headers,
        ) as client:
            current_url = url
            response: httpx.Response | None = None
            for _redirect_count in range(_MAX_REDIRECTS + 1):
                _check_ssrf(current_url)
                marker = _sensitive_url_marker(current_url)
                if marker is not None:
                    raise ValueError("Blocked redirect URL containing sensitive data")

                response = await client.send(client.build_request("GET", current_url), stream=True)
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break
                location = response.headers.get("location")
                if not location:
                    break
                await response.aclose()
                current_url = urljoin(str(response.url), location)
            else:
                raise ValueError(f"Too many redirects (>{_MAX_REDIRECTS})")

            assert response is not None
            try:
                # Stream the body with a hard byte ceiling so an unbounded
                # response can never be buffered fully into memory; max_chars
                # only caps what is returned to the model, not what is
                # downloaded.
                download_limit = _resolve_download_limit_bytes()
                # Snapshot the charset advertised in Content-Type before we
                # start streaming — httpx derives response.encoding from
                # those headers, and we have to honour the server's charset
                # (e.g. text/plain; charset=iso-8859-1) instead of assuming
                # UTF-8. Falling back to utf-8 matches httpx's own default.
                response_encoding = response.encoding or "utf-8"
                total = 0
                chunks: list[bytes] = []
                truncated = False
                async for chunk in response.aiter_bytes(_STREAM_CHUNK_BYTES):
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= download_limit:
                        truncated = True
                        break

                raw_body = b"".join(chunks)
                raw_text = raw_body.decode(response_encoding, errors="replace")

                return (
                    response.status_code,
                    str(response.url),
                    response.headers.get("content-type", ""),
                    raw_text,
                    truncated,
                )
            finally:
                await response.aclose()

    last_error: str | None = None
    for attempt_idx, user_agent in enumerate((_UA_PRIMARY, _UA_FALLBACK)):
        try:
            status, final_url, content_type, raw_html, body_truncated = await _do_fetch(user_agent)
        except SSRFBlockedError:
            raise
        except httpx.TimeoutException:
            raise
        except Exception as exc:
            last_error = str(exc)
            if attempt_idx == 0:
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
                continue
            result: dict[str, Any] = {
                "url": url,
                "final_url": url,
                "status": 0,
                "content_type": "",
                "title": "",
                "extract_mode": extract_mode,
                "extractor": "none",
                "truncated": False,
                "length": 0,
                "text": "",
                "error": last_error,
            }
            return json.dumps(result, ensure_ascii=False)

        is_transient = status in _TRANSIENT_STATUSES
        is_empty_success = 200 <= status < 300 and not raw_html.strip()
        if attempt_idx == 0 and (is_transient or is_empty_success):
            await asyncio.sleep(_RETRY_DELAY_SECONDS)
            continue
        break

    # --- Non-HTML: return as-is ---
    is_html = "html" in content_type.lower()
    if not is_html:
        result = {
            "url": url,
            "final_url": final_url,
            "status": status,
            "content_type": content_type,
            "title": "",
            "extract_mode": extract_mode,
            "extractor": "raw",
            "truncated": body_truncated,
            "length": len(raw_html),
            "text": _wrap_content(final_url, raw_html),
        }
        _cache[cache_key] = result
        return json.dumps(_apply_max_chars(result, effective_max_chars), ensure_ascii=False)

    # --- Error HTTP status: return empty ---
    if status >= 400:
        hint = (
            "rate-limited or blocked upstream; try a different URL from search results, "
            "retry after a brief delay, or use another source"
            if status in _TRANSIENT_STATUSES
            else "HTTP error from upstream; try a different URL or adjust the path"
        )
        result = {
            "url": url,
            "final_url": final_url,
            "status": status,
            "content_type": content_type,
            "title": "",
            "extract_mode": extract_mode,
            "extractor": "none",
            "truncated": False,
            "length": 0,
            "text": "",
            "error": hint,
        }
        if status not in _TRANSIENT_STATUSES:
            _cache[cache_key] = result
        return json.dumps(result, ensure_ascii=False)

    # --- Extraction pipeline ---
    # Try local extractors first (zero-cost, handles ~90% of mainstream pages),
    # escalate to Firecrawl only when readability misses (JS-heavy / anti-bot
    # sites), and fall back to html2text for everything else.
    extracted_content = ""
    extractor_used = "html2text"

    # 1. readability-lxml (local, free, main-content extraction)
    rd_result = _try_readability(raw_html)
    if rd_result is not None:
        title, extracted_content, extractor_used = rd_result

    # 2. Firecrawl escalation — only when readability returns nothing or too
    # little content (SaaS call, requires API key)
    readability_short = len(extracted_content) < _READABILITY_ESCALATION_MIN_CHARS
    firecrawl_key = os.environ.get("FIRECRAWL_API_KEY", "")
    if firecrawl_key and readability_short:
        log.info(
            "web_fetch.firecrawl_escalation",
            url=url,
            readability_chars=len(extracted_content),
            reason="readability_miss" if rd_result is None else "readability_short",
        )
        fc_result = await _try_firecrawl(url, firecrawl_key)
        if fc_result is not None:
            extracted_content, extractor_used = fc_result

    # 3. html2text fallback — always succeeds on valid HTML
    if not extracted_content:
        title, extracted_content, extractor_used = _try_html2text(raw_html)

    # --- Mode conversion ---
    if extract_mode == "text":
        extracted_content = _markdown_to_text(extracted_content)

    result = {
        "url": url,
        "final_url": final_url,
        "status": status,
        "content_type": content_type,
        "title": title,
        "extract_mode": extract_mode,
        "extractor": extractor_used,
        "truncated": body_truncated,
        "length": len(extracted_content),
        "text": _wrap_content(final_url, extracted_content),
    }
    _cache[cache_key] = result
    return json.dumps(_apply_max_chars(result, effective_max_chars), ensure_ascii=False)


def _wrap_content(source: str, content: str) -> str:
    """Wrap fetched page content in the shared ``<untrusted>`` envelope.

    Delegating to :func:`wrap_untrusted_boundary` keeps web content under
    the same tag the system prompt teaches and the dispatch-layer
    origin-trace refusal enforces, instead of a bespoke envelope neither
    recognizes. Boundary-only escaping keeps the extracted markdown
    readable.
    """
    from agentos.safety.injection_guard import wrap_untrusted_boundary

    return wrap_untrusted_boundary(content, source)


def _extract_inner(wrapped: str) -> str:
    """Extract content from inside the <untrusted> envelope."""
    start_tag_end = wrapped.find(">")
    end_tag_start = wrapped.rfind("</untrusted>")
    if start_tag_end == -1 or end_tag_start == -1:
        return wrapped
    return wrapped[start_tag_end + 1 : end_tag_start]


def _apply_max_chars(result: dict[str, Any], max_chars: int | None) -> dict[str, Any]:
    """Return a display copy with max_chars applied.

    The cache stores untruncated content so callers can later request a larger
    explicit cap without waiting for cache expiry.
    """
    if max_chars is None:
        return dict(result)

    output = dict(result)
    inner = _extract_inner(str(output.get("text", "")))
    if len(inner) <= max_chars:
        output["original_length"] = len(inner)
        output["returned_length"] = len(inner)
        return output

    source = str(output.get("final_url") or output.get("url") or "")
    output["text"] = _wrap_content(source, inner[:max_chars])
    output["truncated"] = True
    output["original_length"] = len(inner)
    output["returned_length"] = max_chars
    output["length"] = len(inner)
    return output
