"""LLM-judge router strategy: classify turns R0-R3 via a forced tool call.

Self-contained replacement path for the local ML ensemble (``v4_phase3``).
The judge builds its own provider client lazily via ``build_provider`` and
never re-enters TurnRunner, so recursion is structurally impossible. Errors,
unparseable output (after one repair re-prompt), and internal timeout all
degrade to the configured default tier with ``routing_source="judge_unavailable"``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any

import structlog

from agentos.agentos_router.controller import (
    derive_prompt_policy,
    derive_thinking_mode,
    normalize_decisions,
    synthetic_one_hot,
)
from agentos.provider.selector import build_provider
from agentos.provider.types import ChatConfig, Message, ToolDefinition, ToolInputSchema
from agentos.router_tiers import (
    DEFAULT_TEXT_TIER,
    ROUTE_CLASS_TO_TIER,
    TEXT_TIERS,
    TIER_TO_ROUTE_CLASS,
    normalize_text_tier,
)

log = structlog.get_logger(__name__)

_ROUTE_CLASSES: tuple[str, ...] = tuple(ROUTE_CLASS_TO_TIER)

# Fallback for ``routing_timeout_seconds`` when a duck-typed/partial router_cfg
# omits it (real AgentOSRouterConfig always carries the attribute, defaulting to
# 10.0 — gateway/config.py). Both the judge's internal-timeout derivation
# (_resolve_timeout) and the outer router-step budget (engine/runtime.py) MUST
# read this same fallback: the "inner timeout must win over the un-cancellable
# outer wait_for" guarantee depends on the two sites agreeing on the budget.
DEFAULT_ROUTING_TIMEOUT_SECONDS = 10.0

# ---------------------------------------------------------------------------
# Routing flags (pure-Python port of the v4 bundle's rule-based compute_flags,
# with the default flag_rules baked in — the judge must stay self-sufficient
# once the ML bundle is deleted).
# ---------------------------------------------------------------------------

_FLAG_RULES: dict[str, Any] = {
    "high_risk": {
        "keywords_zh": ["生产", "部署", "回滚", "迁移", "删除", "客户", "法务", "财务"],
        "keywords_en": [
            "deploy",
            "rollback",
            "migration",
            "delete",
            "overwrite",
            "production",
            "customer-facing",
        ],
        "keywords_vi": [
            "triển khai",
            "xoá",
            "xóa",
            "ghi đè",
            "khôi phục",
            "chuyển dữ liệu",
            "khách hàng",
            "pháp lý",
            "tài chính",
        ],
    },
    "debug": {
        "keywords": [
            "error",
            "bug",
            "exception",
            "traceback",
            "failed",
            "root cause",
            "报错",
            "根因",
            "修复",
            "lỗi",
            "gỡ lỗi",
            "sửa lỗi",
            "nguyên nhân gốc",
        ],
        "patterns": [r"Traceback \(most recent", r"stderr:", r"FAILED"],
    },
    "repo_arch": {
        "keywords": [
            "repo",
            "codebase",
            "monorepo",
            "architecture",
            "重构",
            "架构",
            "module",
            "dependency",
        ],
    },
    "strict_format": {
        "keywords": ["JSON", "YAML", "CSV", "schema", "只返回", "不要解释", "按格式"],
    },
    "long_context": {
        "char_threshold": 6000,
        "code_block_threshold": 1500,
        "log_block_threshold": 1500,
        "file_ref_threshold": 2,
    },
}

_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_LOG_BLOCK_RE = re.compile(
    r"(\d{4}[-/]\d{2}[-/]\d{2}[\sT]\d{2}:\d{2}.*\n){3,}"
    r"|"
    r"(^\[?(INFO|WARN|ERROR|DEBUG)\]?\s.*\n){3,}",
    re.MULTILINE,
)
_FILE_PATH_RE = re.compile(
    r"(?:^|[\s\"'`(])([a-zA-Z_][\w.-]*/[\w./-]+\.[\w]+)",
    re.MULTILINE,
)


def _has_keyword(text: str, keywords: list[str]) -> bool:
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def compute_flags(text: str) -> dict[str, bool]:
    """Compute the five rule-based routing flags from raw text.

    Pure-Python port of the v4 bundle's ``compute_flags`` using the bundled
    default ``flag_rules``, extended with Vietnamese high-risk/debug keywords
    alongside zh/en. Called on the FULL text before any truncation so a long
    pasted traceback still signals even when the prompt body is elided.
    """
    hr = _FLAG_RULES["high_risk"]
    high_risk = (
        _has_keyword(text, hr["keywords_zh"])
        or _has_keyword(text, hr["keywords_en"])
        or _has_keyword(text, hr["keywords_vi"])
    )

    dbg = _FLAG_RULES["debug"]
    debug = _has_keyword(text, dbg["keywords"]) or any(
        re.search(pattern, text) for pattern in dbg["patterns"]
    )

    repo_arch = _has_keyword(text, _FLAG_RULES["repo_arch"]["keywords"])
    strict_format = _has_keyword(text, _FLAG_RULES["strict_format"]["keywords"])

    lc = _FLAG_RULES["long_context"]
    long_context = (
        len(text) >= lc["char_threshold"]
        or sum(len(m.group()) for m in _CODE_BLOCK_RE.finditer(text))
        >= lc["code_block_threshold"]
        or sum(len(m.group()) for m in _LOG_BLOCK_RE.finditer(text)) >= lc["log_block_threshold"]
        or len(_FILE_PATH_RE.findall(text)) >= lc["file_ref_threshold"]
    )

    return {
        "high_risk": high_risk,
        "long_context": long_context,
        "debug": debug,
        "repo_arch": repo_arch,
        "strict_format": strict_format,
    }


# ---------------------------------------------------------------------------
# Judge model resolution (spec D2)
# ---------------------------------------------------------------------------


# Generic provider id (openai_compat backend, no default base_url → requires an
# explicit base_url) used to build a judge client against a local
# OpenAI-compatible endpoint (Ollama / LM Studio / llama.cpp / vLLM). The real
# endpoint identity comes from ``judge_base_url``.
LOCAL_JUDGE_PROVIDER_ID = "vllm"


def resolve_judge_target(
    router_cfg: object,
    llm_cfg: object,
) -> tuple[str, str, str] | None:
    """Resolve the judge (provider, model, source) without vendor hard-coding.

    Chain: local OpenAI-compatible endpoint (``judge_base_url`` set with an
    explicit ``judge_model``) → explicit ``judge_model`` (+ optional
    ``judge_provider``) → the c0 tier's model → the cheapest available TEXT
    tier skipping ``image_only`` entries → ``None`` (judge unavailable). AUTO
    is strictly ``judge_model is None`` — not empty string.
    """
    llm_provider = str(getattr(llm_cfg, "provider", "") or "").strip().casefold()

    judge_model = getattr(router_cfg, "judge_model", None)
    judge_base_url = str(getattr(router_cfg, "judge_base_url", None) or "").strip()
    # AUTO is strictly ``judge_model is None`` (spec D2), but a blank/whitespace
    # ``judge_model`` (e.g. a hand-edited ``agentos.toml`` with
    # ``judge_model = ""``) must not become an unusable explicit target with an
    # empty model id — treat it as AUTO and fall through to the tier scan.
    if judge_model is not None and str(judge_model).strip():
        # A local OpenAI-compatible endpoint takes precedence: the judge client
        # is built against ``judge_base_url`` with ``judge_api_key`` and needs no
        # cloud credentials, so the provider-match constraint is bypassed. The
        # ``source`` is "local" for observability.
        if judge_base_url:
            return LOCAL_JUDGE_PROVIDER_ID, str(judge_model).strip(), "local"
        judge_provider = (
            str(getattr(router_cfg, "judge_provider", None) or "").strip().casefold()
        )
        return judge_provider or llm_provider, str(judge_model).strip(), "explicit"

    tiers = getattr(router_cfg, "tiers", None)
    if not isinstance(tiers, dict):
        return None
    for tier_name in TEXT_TIERS:
        entry = tiers.get(tier_name)
        if not isinstance(entry, dict) or entry.get("image_only"):
            continue
        model = str(entry.get("model", "") or "").strip()
        if not model:
            continue
        provider = str(entry.get("provider", "") or "").strip().casefold() or llm_provider
        return provider, model, "auto"
    return None


def judge_provider_has_credentials(
    judge_provider: str, llm_cfg: object, source: str | None = None
) -> bool:
    """Whether the resolved judge target has a usable credential source.

    Tier entries carry no credentials, so ``LLMJudgeStrategy._credentials_for``
    only returns non-empty creds when the resolved judge provider equals
    ``llm.provider``. A judge resolved (in AUTO, from a tier's own ``provider``
    field, or explicitly) to a DIFFERENT provider therefore has no credential
    source and degrades to ``judge_unavailable`` on every turn — even though
    ``resolve_judge_target`` happily returns a non-None target. Callers (doctor,
    boot) use this to distinguish a genuinely usable judge from a cross-provider
    one that is silently broken.

    A ``source="local"`` target carries its own credentials via
    ``judge_base_url`` / ``judge_api_key`` and deliberately bypasses the
    provider-match constraint, so it is always considered credentialed.
    """
    if source == "local":
        return True
    llm_provider = str(getattr(llm_cfg, "provider", "") or "").strip().casefold()
    return bool(llm_provider) and judge_provider.strip().casefold() == llm_provider


def _run_coro_blocking(coro: Any) -> Any:
    """Run ``coro`` to completion from either sync or async context.

    ``probe_local_judge`` keeps a synchronous signature (both the interactive CLI
    prompt code and the WebUI/RPC ``upsert_router`` path call it as a plain
    function). Both callers now reach it with NO running loop in the calling
    thread: the CLI runs it from synchronous questionary code, and the RPC
    handler ``onboarding.router.configure`` dispatches ``upsert_router`` onto a
    worker thread via ``asyncio.to_thread`` (so the blocking probe never stalls
    the gateway event loop). ``asyncio.run`` therefore drives the coroutine
    inline in the common case. The already-running-loop branch is retained as a
    defensive fallback for any future in-loop caller: it dispatches the coroutine
    onto a dedicated worker thread with its own event loop rather than letting a
    bare ``asyncio.run`` raise ``RuntimeError`` — but note it still blocks the
    calling thread, so callers on a live event loop must reach it via
    ``asyncio.to_thread`` rather than relying on this branch.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop in this thread — safe to drive one inline.
        return asyncio.run(coro)

    # A loop is already running here (WebUI/RPC path). ``asyncio.run`` would raise,
    # so run the coroutine on its own loop in a separate thread and wait for it.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


def probe_local_judge(base_url: str, model: str, api_key: str | None) -> str | None:
    """Run one cheap test classification against a local judge endpoint.

    Returns ``None`` on success, or a short error string on failure. This is the
    single connectivity check shared by every onboarding surface (interactive
    CLI, WebUI/RPC via ``upsert_router``) so a local-endpoint judge is verified —
    not merely URL-shape validated — before it is persisted (spec D2). It mirrors
    the real judge path: a self-contained :class:`LLMJudgeStrategy` built against
    the local endpoint, so a reachable-but-wrong-model endpoint (one that never
    returns a usable routing decision) is caught here rather than surfacing later
    as ``judge_unavailable`` on every turn.

    Loop-safe: the WebUI/RPC surface reaches this from inside the running gateway
    event loop, so the classify coroutine is driven via :func:`_run_coro_blocking`
    rather than a bare ``asyncio.run`` (which raises inside a running loop).
    """
    from types import SimpleNamespace

    router_cfg = SimpleNamespace(
        tiers={},
        default_tier="c1",
        judge_model=model,
        judge_base_url=base_url,
        judge_api_key=api_key or None,
        judge_short_circuit_enabled=False,
        routing_timeout_seconds=15.0,
    )
    llm_cfg = SimpleNamespace(provider="", api_key="", api_key_env="", base_url="")
    strategy = LLMJudgeStrategy(router_cfg=router_cfg, llm_cfg=llm_cfg)
    try:
        _tier, _confidence, source, _extra = _run_coro_blocking(
            strategy.classify(
                "hello, please classify this test turn", ["c0", "c1", "c2", "c3"]
            )
        )
    except Exception as exc:  # noqa: BLE001 - surface any connectivity failure
        return str(exc) or exc.__class__.__name__
    if source == "judge_unavailable":
        return "the endpoint did not return a usable routing decision"
    return None


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_ELISION_MARKER = "\n[... {omitted} chars omitted ...]\n"
_TRUNCATION_HEAD_CHARS = 800
_TRUNCATION_TAIL_CHARS = 1200
_DEFAULT_INPUT_MAX_CHARS = 4000
_MAX_RECENT_DECISIONS = 5

_HARD_RULES = (
    "Hard rules:\n"
    "- Length is NOT difficulty: a short production/delete/rollback/migration "
    "request is R3.\n"
    "- When torn between two classes, choose the HIGHER one.\n"
    "- Do not route below the session's established tier for a short follow-up "
    "in an ongoing workstream (see RECENT_DECISIONS).\n"
    "- The input may be Vietnamese, English, or Chinese — classify by task "
    "difficulty regardless of language.\n"
    "- Agentic workstream (SIGNALS contains 'agentic') → not below R1."
)

_BOUNDARY_EXAMPLES = (
    "Boundary examples:\n"
    '- R1 vs R2: "Write a Python function that parses this CSV file" → R1; '
    '"Refactor this multi-module parser and explain the design trade-offs" → R2.\n'
    '- R2 vs R3: "Debug this failing unit test" → R2; '
    '"Diagnose this intermittent production data-corruption bug across services '
    'and plan a safe rollback" → R3.'
)

_VIETNAMESE_EXAMPLES = (
    "Vietnamese examples:\n"
    '- "chào bạn, khỏe không?" → R0 (trivial chat)\n'
    '- "viết giúp mình một hàm Python đọc file CSV" → R1 (simple coding)\n'
    '- "xoá bảng users trên database production rồi migrate lại giúp mình" → R3 '
    "(short but production-destructive: high risk)\n"
    '- "debug giúp mình lỗi race condition trong service thanh toán, log ở dưới" '
    "→ R2 (nontrivial debugging)"
)

_DEFAULT_SHORT_CIRCUIT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "hi",
        "hello",
        "hey",
        "yo",
        "thanks",
        "thank you",
        "thx",
        "ty",
        "ok",
        "okay",
        "k",
        "yes",
        "no",
        "cool",
        "nice",
        "great",
        "good morning",
        "good night",
        "bye",
        "goodbye",
        # Vietnamese
        "chào",
        "chào bạn",
        "xin chào",
        "cảm ơn",
        "cám ơn",
        "cảm ơn nhé",
        "cảm ơn bạn",
        "ừ",
        "ừm",
        "vâng",
        "dạ",
        "được",
        "ok cảm ơn",
        "tạm biệt",
        # Chinese
        "你好",
        "谢谢",
        "好的",
        "嗯",
        "再见",
    }
)
_SHORT_CIRCUIT_MAX_CHARS = 20

# Providers whose chat backend cannot honor a forced ``cfg.tool_choice`` (the
# judge's structured-output mechanism, spec D1). On these the judge degrades to
# the text-JSON parse + single repair fallback, raising the judge_unavailable
# rate and reducing determinism. None of the shipped tier profiles resolve to
# one, but an operator may set llm.provider to any of these, so warn at
# judge-resolution time rather than failing silently.
_NON_FORCING_TOOL_CHOICE_PROVIDERS: frozenset[str] = frozenset({"ollama"})

_EMIT_ROUTE_TOOL = ToolDefinition(
    name="emit_route",
    description="Emit the routing decision for the current user turn.",
    input_schema=ToolInputSchema(
        properties={
            "route_class": {
                "type": "string",
                "enum": list(_ROUTE_CLASSES),
                "description": "Route class for the current turn.",
            },
            "confidence": {
                "type": "number",
                "description": "Self-assessed confidence in [0, 1].",
            },
            "reason": {
                "type": "string",
                "description": "One short sentence explaining the decision.",
            },
        },
        required=["route_class", "confidence", "reason"],
    ),
)

_FORCED_TOOL_CHOICE = {"type": "function", "function": {"name": "emit_route"}}

_REPAIR_PROMPT = (
    "Your previous reply was not parseable. Respond with ONLY a JSON object, "
    'no prose, no code fences: {"route_class": "R0"|"R1"|"R2"|"R3", '
    '"confidence": <number 0-1>, "reason": "<short string>"}'
)

def _iter_json_object_candidates(text: str, *, string_aware: bool = True):
    """Yield candidate ``{...}`` spans, brace-balanced and string-aware.

    A greedy first-``{``-to-last-``}`` match breaks whenever brace-bearing prose
    precedes the verdict — extremely common with reasoning/local judges
    (``<think>the user wants {json}</think>{"route_class":"R2",...}`` or
    ``Maybe {R1?} ... {valid json}``): the greedy span includes the stray brace,
    ``json.loads`` fails, and the parse returns None. This is the OPERATIVE parse
    path for exactly the judge targets that cannot honor a forced tool_choice
    (Ollama / local OpenAI-compatible endpoints), where reasoning traces wrap the
    output in stray braces.

    Instead, walk the text and emit each top-level brace-balanced object span in
    order, ignoring braces inside JSON strings. The caller tries each candidate
    and takes the first that ``json.loads`` accepts, so a stray ``{maybe R1?}``
    before the real object is simply skipped.

    String state is tracked ONLY inside an object (``depth > 0``), where it must
    shield braces embedded in JSON string values. Outside any object
    (``depth == 0``) quotes are prose and are ignored: an UNBALANCED double-quote
    in a reasoning/local judge's think-trace before the verdict
    (``the user said "delete prod … {"route_class":"R2",…}``) must NOT trap the
    scanner in string state, which would swallow the real verdict's braces and
    return None. Prose-level quotes carry no structure, so skipping them here is
    always safe.

    When ``string_aware`` is False, string state is never entered — the scan is a
    pure brace balancer. This is the fallback for the pathological case where a
    stray quote AND stray braces interleave INSIDE what looks like an object span
    (``he said "{oops {"route_class":"R2"}``): string-aware scanning can still be
    trapped there, so ``_extract_verdict_from_text`` retries with a brace-only
    scan when the string-aware pass yields no verdict.
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for idx, ch in enumerate(text or ""):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if string_aware and ch == '"':
            # Only enter string state while inside an object; a bare quote in
            # prose (depth == 0) is ignored so an unbalanced trace quote can't
            # hide the verdict that follows.
            if depth > 0:
                in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    yield text[start : idx + 1]
                    start = -1


def _truncate_body(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    # ``max_chars`` may be configured below HEAD+TAIL (its floor is 1000, but the
    # fixed head/tail budget is 2000). Splitting unconditionally would overlap the
    # head and tail slices — duplicating the middle and yielding a negative
    # ``omitted`` count. When the budget is too small to fit a head+tail split
    # plus the elision marker, just hard-truncate to ``max_chars``.
    marker_len = len(_ELISION_MARKER.format(omitted=len(text)))
    if max_chars < _TRUNCATION_HEAD_CHARS + _TRUNCATION_TAIL_CHARS + marker_len:
        return text[:max_chars]
    head = text[:_TRUNCATION_HEAD_CHARS]
    tail = text[-_TRUNCATION_TAIL_CHARS:]
    omitted = len(text) - len(head) - len(tail)
    return head + _ELISION_MARKER.format(omitted=omitted) + tail


def _find_valid_tier(start_tier: str, valid_tiers: list[str]) -> str:
    if not valid_tiers:
        return DEFAULT_TEXT_TIER
    tiers = list(TEXT_TIERS)
    start_idx = tiers.index(start_tier) if start_tier in tiers else 1
    # Prefer the nearest valid tier at or above the desired tier.
    for idx in range(start_idx, len(tiers)):
        if tiers[idx] in valid_tiers:
            return tiers[idx]
    # The desired tier is above every valid tier: clamp to the HIGHEST valid
    # tier (scan downward), never down to the cheapest — a high-risk/hard turn
    # must not silently collapse to the cheapest available model.
    for tier in reversed(tiers):
        if tier in valid_tiers:
            return tier
    return valid_tiers[0]


def _iter_json_dicts(text: str, *, string_aware: bool = True):
    """Yield each brace-balanced top-level object in ``text`` that decodes to a dict.

    Skips stray braces in prose / reasoning traces (a span that isn't valid JSON
    or isn't a dict is dropped) and preserves document order.
    """
    for candidate in _iter_json_object_candidates(text or "", string_aware=string_aware):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            yield parsed


def _extract_verdict_from_text(text: str) -> _JudgeVerdict | None:
    """Return the first embedded JSON object that parses to a VALID verdict.

    Verdict validity (``route_class`` in R0-R3) is checked per-candidate, so a
    valid verdict preceded by any OTHER JSON object — a reasoning/analysis blob
    (``{"analysis": ...}``) or a self-correction (``{"route_class": "R5", ...}``)
    — is recovered instead of degrading to ``judge_unavailable``. This is the
    operative path for judge targets that cannot honor a forced tool_choice
    (Ollama / local OpenAI-compatible / reasoning judges), which routinely emit a
    non-verdict object before the real verdict. Non-verdict but valid-JSON dicts
    are skipped rather than committed to.

    Two passes: the primary string-aware scan (braces inside JSON string values
    are shielded), then a brace-only fallback if that yields nothing. The
    fallback rescues the pathological case where a stray double-quote and stray
    braces interleave inside a would-be object span and trap the string-aware
    scanner — a real risk for reasoning/local judges whose think-trace emits an
    unbalanced quote right before the verdict. Trading the (rare) chance of a
    brace-in-string false split for recovering the verdict beats degrading to
    ``judge_unavailable`` and silently dropping a high-risk turn to the default
    tier.
    """
    for string_aware in (True, False):
        for parsed in _iter_json_dicts(text or "", string_aware=string_aware):
            verdict = _parse_verdict(parsed)
            if verdict is not None:
                return verdict
    return None


@dataclass(frozen=True)
class _JudgeVerdict:
    route_class: str
    confidence: float
    reason: str


def _parse_verdict(payload: dict[str, Any] | None) -> _JudgeVerdict | None:
    if not isinstance(payload, dict):
        return None
    route_class = str(payload.get("route_class", "") or "").strip().upper()
    if route_class not in _ROUTE_CLASSES:
        return None
    try:
        confidence = float(payload.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))
    reason = str(payload.get("reason", "") or "").strip()
    return _JudgeVerdict(route_class=route_class, confidence=confidence, reason=reason)


class _JudgeCallError(RuntimeError):
    """Provider stream reported an error event."""


class LLMJudgeStrategy:
    """History-aware router strategy backed by a small LLM judge call.

    Implements the same ``classify()`` contract as ``V4Phase3Strategy``. The
    provider client is built lazily from the resolved judge target; every
    failure mode collapses to the configured default tier with
    ``routing_source="judge_unavailable"``.
    """

    requires_history = True
    source = "llm_judge"

    def __init__(
        self,
        router_cfg: object | None = None,
        llm_cfg: object | None = None,
        *,
        provider_factory: Any | None = None,
    ) -> None:
        self._router_cfg = router_cfg
        self._llm_cfg = llm_cfg
        self._provider_factory = provider_factory or self._default_provider_factory
        self._provider: Any | None = None
        self._target: tuple[str, str, str] | None = None
        self._resolved = False

        self._input_max_chars = max(
            1000,
            int(getattr(router_cfg, "judge_input_max_chars", None) or _DEFAULT_INPUT_MAX_CHARS),
        )
        short_circuit_cfg = getattr(router_cfg, "judge_short_circuit_enabled", None)
        self._short_circuit_enabled = True if short_circuit_cfg is None else bool(short_circuit_cfg)
        allowlist = getattr(router_cfg, "judge_short_circuit_allowlist", None)
        # The config field is additive ("Extra ... phrases"): a configured list
        # EXTENDS the built-in default set rather than replacing it, so an
        # operator adding one phrase never silently loses the built-in
        # greetings/acks (including the Vietnamese/Chinese terms).
        if allowlist:
            extra = frozenset(
                str(item).strip().casefold() for item in allowlist if str(item).strip()
            )
            self._short_circuit_allowlist = _DEFAULT_SHORT_CIRCUIT_ALLOWLIST | extra
        else:
            self._short_circuit_allowlist = _DEFAULT_SHORT_CIRCUIT_ALLOWLIST
        self._timeout = self._resolve_timeout(router_cfg)

    @staticmethod
    def _resolve_timeout(router_cfg: object | None) -> float:
        # The outer router step (runtime.py) bounds the whole call with
        # asyncio.wait_for(routing_timeout_seconds) on a NON-cancellable
        # to_thread worker. The judge's own internal timeout must therefore be
        # provably below that outer budget so it — not the un-cancellable outer
        # wait_for — is the operative timeout; otherwise the worker thread (and
        # its in-flight provider HTTP call + shared-state mutation) is orphaned
        # and keeps running after the step gives up. Both sites read the same
        # DEFAULT_ROUTING_TIMEOUT_SECONDS fallback so a duck-typed router_cfg
        # missing the attribute can never desync the two budgets.
        budget = float(
            getattr(router_cfg, "routing_timeout_seconds", None)
            or DEFAULT_ROUTING_TIMEOUT_SECONDS
        )
        # Aim ~0.5s / 20% below budget, but for tiny budgets the 0.5s floor
        # could meet or exceed budget; the final min() with budget*0.9 keeps the
        # ceiling STRICTLY below the outer budget so the inner timeout always
        # wins even at small configured values.
        ceiling = min(max(0.5, min(budget * 0.8, budget - 0.5)), budget * 0.9)
        explicit = getattr(router_cfg, "judge_timeout_seconds", None)
        if explicit:
            # Clamp an operator-supplied timeout under the outer budget even
            # when they set judge_timeout_seconds >= routing_timeout_seconds
            # (config only validates gt=0.0), so the inner timeout always wins.
            # Apply the 0.1s lower bound only when it does not exceed the
            # ceiling: for a tiny budget the ceiling can be < 0.1s, so a fixed
            # 0.1 floor would push the inner timeout back up to == budget and
            # orphan the worker thread (finding #3). Clamp the floor to the
            # ceiling so the result always stays strictly below the outer budget.
            floor = min(0.1, ceiling)
            timeout = min(max(floor, float(explicit)), ceiling)
        else:
            timeout = ceiling
        # Defensive invariant: the inner timeout must stay strictly below the
        # outer budget or the orphaned-worker guarantee is void.
        assert timeout < budget, (timeout, budget)
        return timeout

    @staticmethod
    def _default_provider_factory(provider: str, model: str, api_key: str, base_url: str) -> Any:
        return build_provider(provider=provider, model=model, api_key=api_key, base_url=base_url)

    # -- provider resolution ------------------------------------------------

    def _resolve_target(self) -> tuple[str, str, str] | None:
        if not self._resolved:
            self._target = resolve_judge_target(self._router_cfg, self._llm_cfg)
            self._resolved = True
            if self._target is None:
                log.warning("llm_judge.no_judge_target")
            else:
                provider, model, source = self._target
                # base_url only for a local endpoint (redact any api key; never
                # log judge_api_key).
                base_url = (
                    str(getattr(self._router_cfg, "judge_base_url", "") or "").strip()
                    if source == "local"
                    else None
                )
                log.info(
                    "llm_judge.judge_resolved",
                    provider=provider,
                    model=model,
                    source=source,
                    base_url=base_url,
                )
                if "moonshot" in provider.lower() or "kimi" in model.lower():
                    # Moonshot silently drops non-1.0 temperature; the judge
                    # pins temperature=0.0, so determinism degrades there.
                    log.warning(
                        "llm_judge.moonshot_temperature_unpinned",
                        provider=provider,
                        model=model,
                    )
                if provider.strip().casefold() in _NON_FORCING_TOOL_CHOICE_PROVIDERS:
                    # This provider cannot force cfg.tool_choice, so the D1
                    # structured-output contract is not guaranteed — the judge
                    # falls back to text-JSON parsing and the judge_unavailable
                    # rate rises. Surface it instead of degrading silently.
                    log.warning(
                        "llm_judge.forced_tool_choice_unsupported",
                        provider=provider,
                        model=model,
                    )
        return self._target

    def _ensure_provider(self) -> Any | None:
        if self._provider is not None:
            return self._provider
        target = self._resolve_target()
        if target is None:
            return None
        provider_name, model, source = target
        api_key, base_url = self._credentials_for(provider_name, source)
        try:
            self._provider = self._provider_factory(provider_name, model, api_key, base_url)
        except Exception as exc:  # noqa: BLE001 - degrade to judge_unavailable
            log.warning(
                "llm_judge.provider_build_failed",
                provider=provider_name,
                model=model,
                error=str(exc),
            )
            return None
        return self._provider

    def _credentials_for(self, provider_name: str, source: str = "") -> tuple[str, str]:
        """Return (api_key, base_url) for the resolved judge target.

        A ``source="local"`` target points at a local OpenAI-compatible endpoint
        (``judge_base_url``): it carries its own ``judge_api_key`` (a placeholder
        when unset — local endpoints typically need none) and bypasses the
        credential-must-match-``llm.provider`` constraint. Otherwise tier entries
        carry no credentials, so the judge inherits ``llm.*`` credentials only
        when its provider matches ``llm.provider``.
        """
        if source == "local":
            base_url = str(getattr(self._router_cfg, "judge_base_url", "") or "").strip()
            # Local OpenAI-compatible servers usually accept any bearer token;
            # supply a harmless placeholder when judge_api_key is unset so the
            # openai_compat client always has a non-empty Authorization header.
            api_key = str(getattr(self._router_cfg, "judge_api_key", "") or "") or "sk-local"
            return api_key, base_url
        llm_provider = str(getattr(self._llm_cfg, "provider", "") or "").strip().casefold()
        if provider_name.strip().casefold() != llm_provider:
            return "", ""
        api_key = str(getattr(self._llm_cfg, "api_key", "") or "")
        if not api_key:
            api_key_env = str(getattr(self._llm_cfg, "api_key_env", "") or "")
            if api_key_env:
                api_key = os.environ.get(api_key_env, "")
        base_url = str(getattr(self._llm_cfg, "base_url", "") or "")
        return api_key, base_url

    # -- classify contract ---------------------------------------------------

    async def classify(
        self,
        message: str,
        valid_tiers: list[str],
        routing_history: list[dict] | None = None,
        prev_assistant_text: str | None = None,
        prev_assistant_usage: dict | None = None,
        history_user_texts: list[str] | None = None,
        flags_text_override: str | None = None,
        tool_defs: list | None = None,
    ) -> tuple[str, float, str, dict]:
        """Classify a turn into AgentOS tier format via the LLM judge."""
        flags_text = flags_text_override or message
        flags = compute_flags(flags_text)
        flags["agentic"] = bool(tool_defs)

        short = self._short_circuit(message, valid_tiers, flags)
        if short is not None:
            return short

        try:
            verdict = await asyncio.wait_for(
                self._judge(message, routing_history or [], flags, tool_defs),
                timeout=self._timeout,
            )
        except TimeoutError:
            log.warning("llm_judge.timeout", timeout_seconds=self._timeout)
            return self._unavailable_classify(valid_tiers, flags, reason="judge timeout")
        except Exception as exc:  # noqa: BLE001 - degrade to judge_unavailable
            log.warning("llm_judge.call_failed", error=str(exc), exc_info=True)
            return self._unavailable_classify(valid_tiers, flags, reason=str(exc))

        if verdict is None:
            return self._unavailable_classify(
                valid_tiers, flags, reason="unparseable judge output"
            )

        tier = ROUTE_CLASS_TO_TIER.get(verdict.route_class, DEFAULT_TEXT_TIER)
        if tier not in valid_tiers:
            tier = _find_valid_tier(tier, valid_tiers)
        final_route_class = TIER_TO_ROUTE_CLASS.get(tier, verdict.route_class)
        extra = self._build_extra(
            route_class=verdict.route_class,
            final_route_class=final_route_class,
            confidence=1.0,
            flags=flags,
            reason=verdict.reason or "llm judge decision",
        )
        # Judge self-reported confidence is uncalibrated: return fixed 1.0 so
        # the deterministic confidence gate stays inert (spec D3).
        return tier, 1.0, self.source, extra

    # -- internals -----------------------------------------------------------

    def _short_circuit(
        self,
        message: str,
        valid_tiers: list[str],
        flags: dict[str, bool],
    ) -> tuple[str, float, str, dict] | None:
        if not self._short_circuit_enabled:
            return None
        if flags.get("agentic"):
            # Spec D1: an agentic workstream is never routed below R1. The
            # short-circuit only ever yields R0, so a bare ack ("ok", "dạ")
            # mid-workstream would violate that floor — defer to the judge,
            # whose "agentic workstream → not below R1" rubric rule holds it.
            return None
        stripped = message.strip()
        if not stripped or len(stripped) > _SHORT_CIRCUIT_MAX_CHARS:
            return None
        normalized = stripped.casefold().rstrip("!.?~ ")
        if normalized not in self._short_circuit_allowlist:
            return None
        tier = _find_valid_tier(ROUTE_CLASS_TO_TIER["R0"], valid_tiers)
        final_route_class = TIER_TO_ROUTE_CLASS.get(tier, "R0")
        extra = self._build_extra(
            route_class="R0",
            final_route_class=final_route_class,
            confidence=1.0,
            flags=flags,
            reason="greeting/ack short-circuit",
        )
        return tier, 1.0, self.source, extra

    def _unavailable_classify(
        self,
        valid_tiers: list[str],
        flags: dict[str, bool],
        *,
        reason: str,
    ) -> tuple[str, float, str, dict]:
        default = (
            normalize_text_tier(getattr(self._router_cfg, "default_tier", None))
            or DEFAULT_TEXT_TIER
        )
        tier = _find_valid_tier(default, valid_tiers)
        route_class = TIER_TO_ROUTE_CLASS.get(tier, "R1")
        extra = self._build_extra(
            route_class=route_class,
            final_route_class=route_class,
            confidence=0.0,
            flags=flags,
            reason=f"judge_unavailable: {reason}",
        )
        return tier, 0.0, "judge_unavailable", extra

    def _build_extra(
        self,
        *,
        route_class: str,
        final_route_class: str,
        confidence: float,
        flags: dict[str, bool],
        reason: str,
    ) -> dict[str, Any]:
        probs = synthetic_one_hot(ROUTE_CLASS_TO_TIER.get(final_route_class, DEFAULT_TEXT_TIER))
        thinking_mode = derive_thinking_mode(probs, flags)
        prompt_policy = derive_prompt_policy(probs, flags)
        thinking_mode, prompt_policy = normalize_decisions(thinking_mode, prompt_policy)
        return {
            "route_class": route_class,
            "top1_label": route_class,
            "final_route_class": final_route_class,
            "confidence": confidence,
            "thinking_mode": thinking_mode,
            "prompt_policy": prompt_policy,
            "flags": dict(flags),
            "reason": reason,
            "probabilities": None,
            "margin": None,
            "difficulty": None,
        }

    async def _judge(
        self,
        message: str,
        routing_history: list[dict],
        flags: dict[str, bool],
        tool_defs: list | None,
    ) -> _JudgeVerdict | None:
        provider = self._ensure_provider()
        if provider is None:
            return None

        system = self._system_prompt()
        user_text = self._user_prompt(message, routing_history, flags, tool_defs)
        messages = [Message(role="user", content=user_text)]

        tool_args, text = await self._call_provider(
            provider, messages, system=system, use_tools=True
        )
        verdict = _parse_verdict(tool_args)
        if verdict is None:
            verdict = _extract_verdict_from_text(text)
        if verdict is not None:
            return verdict

        # One repair re-prompt: replay the judge's raw output and demand JSON.
        repair_messages = [
            *messages,
            Message(role="assistant", content=text or "(no output)"),
            Message(role="user", content=_REPAIR_PROMPT),
        ]
        _tool_args, repair_text = await self._call_provider(
            provider, repair_messages, system=system, use_tools=False
        )
        return _extract_verdict_from_text(repair_text)

    async def _call_provider(
        self,
        provider: Any,
        messages: list[Message],
        *,
        system: str,
        use_tools: bool,
    ) -> tuple[dict[str, Any] | None, str]:
        cfg = ChatConfig(
            max_tokens=400,
            temperature=0.0,
            system=system,
            thinking=False,
            timeout=self._timeout,
            tool_choice=_FORCED_TOOL_CHOICE if use_tools else None,
        )
        tools = [_EMIT_ROUTE_TOOL] if use_tools else None
        tool_args: dict[str, Any] | None = None
        chunks: list[str] = []
        async for event in provider.chat(messages, tools=tools, config=cfg):
            kind = getattr(event, "kind", "")
            if kind == "error":
                raise _JudgeCallError(getattr(event, "message", "") or "provider error")
            if (
                kind == "tool_use_end"
                and getattr(event, "tool_name", "") == _EMIT_ROUTE_TOOL.name
                and tool_args is None
            ):
                arguments = getattr(event, "arguments", None)
                if isinstance(arguments, dict):
                    tool_args = dict(arguments)
            elif kind == "text_delta":
                text = getattr(event, "text", "") or ""
                if text:
                    chunks.append(text)
        return tool_args, "".join(chunks)

    def _system_prompt(self) -> str:
        rubric_lines = ["Route classes (cheapest to most capable):"]
        tiers = getattr(self._router_cfg, "tiers", None)
        tiers = tiers if isinstance(tiers, dict) else {}
        for tier_name in TEXT_TIERS:
            entry = tiers.get(tier_name)
            if isinstance(entry, dict) and entry.get("image_only"):
                continue
            description = ""
            if isinstance(entry, dict):
                description = str(entry.get("description", "") or "").strip()
            route_class = TIER_TO_ROUTE_CLASS.get(tier_name, tier_name)
            rubric_lines.append(
                f"- {route_class} (tier {tier_name}): {description or 'no description'}"
            )
        return (
            "You are the AgentOS routing judge. Classify the user's CURRENT turn "
            "into exactly one route class by task difficulty and risk.\n"
            + "\n".join(rubric_lines)
            + "\n"
            + _BOUNDARY_EXAMPLES
            + "\n"
            + _HARD_RULES
            + "\n"
            + _VIETNAMESE_EXAMPLES
            + "\n"
            "Call the emit_route tool with route_class, confidence (0-1), and a "
            "short reason."
        )

    def _user_prompt(
        self,
        message: str,
        routing_history: list[dict],
        flags: dict[str, bool],
        tool_defs: list | None,
    ) -> str:
        signals = [name for name in flags if flags[name] and name != "agentic"]
        signals.append(f"chars={len(message)}")
        if tool_defs:
            signals.append("agentic")
            signals.append(f"tools={len(tool_defs)}")
        lines = [f"[SIGNALS: {', '.join(signals)}]"]

        decisions = []
        for entry in routing_history[-_MAX_RECENT_DECISIONS:]:
            route_class = entry.get("final_route_class") or entry.get("route_class")
            if route_class:
                decisions.append(str(route_class))
        if decisions:
            lines.append(f"[RECENT_DECISIONS: {', '.join(decisions)}]")

        lines.append("[USER_TURN]")
        lines.append(_truncate_body(message, self._input_max_chars))
        return "\n".join(lines)
