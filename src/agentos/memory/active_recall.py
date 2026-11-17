"""Proactive memory recall before an interactive user turn.

Curated ``MEMORY.md`` / ``USER.md`` reach the prompt as a frozen snapshot, but
everything else in the index (``memory/**/*.md`` notes, indexed sessions, the
knowledge base) is only reachable through ``memory_search`` -- which a model
rarely calls on its own. This module recalls on the model's behalf, in two
lanes, and returns one bounded, fenced block for the turn's dynamic context:

* **Trigger lane** (always, cheap): a lexical search of the curated memory
  notes with the user's message. A hit is injected only when it is a strong
  match by a deterministic score (term coverage of the message and of the
  note's heading, plus the search score), so ordinary turns stay unchanged.
* **Deep lane** (escalation): when the message asks about the past -- "last
  time", "what did we decide", "hôm trước", "còn nhớ ... không" -- and the
  trigger lane found nothing strong, a wider search runs (all sources,
  hybrid) and one small-model call reads only those memory excerpts and
  returns a short summary or ``NONE``. ``mode="always"`` runs it on every
  eligible turn; ``mode="off"`` disables it.

Both lanes are bounded: the trigger lane by ``[memory]
recall_timeout_seconds`` (through :class:`RecallBudget`), the deep lane by
``[memory.recall] deep_timeout_seconds``. Nothing here raises into the turn:
every failure degrades to "no block" (or, for a deep recall that could not
finish, a short note saying so).

Only interactive, user-facing, private sessions are eligible; see
:func:`recall_skip_reason`. A session can be paused at runtime and the last
outcome per session is kept for inspection (:class:`ActiveRecallState`).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import structlog

from agentos.memory.types import MemorySearchOpts, MemorySearchResult, MemorySource, SearchIntent

log = structlog.get_logger(__name__)

RecallMode = Literal["escalate", "always", "off"]
EscalationDecision = Literal["recall", "mode-off", "strong-trigger-hit", "no-recall-intent"]

#: Longest slice of the user message used as a search query.
MAX_QUERY_CHARS = 480
#: Candidates fetched for the trigger lane before scoring.
TRIGGER_CANDIDATES = 12
#: Excerpt budget per deep-lane hit, and for all of them together.
DEEP_EXCERPT_CHARS = 1200
DEEP_EVIDENCE_CHARS = 6000
DEEP_MIN_SCORE = 0.2
#: Lines of surrounding context read around a deep-lane memory-file hit.
DEEP_CONTEXT_LINES = 2

#: Curated root files are already in the system prompt as a snapshot.
_CURATED_ROOT_FILES = frozenset({"memory.md", "user.md"})

#: Run kinds whose turns come from a person typing in a conversation.
_INTERACTIVE_RUN_KINDS = frozenset({"default", "channel_turn"})
#: Prompt modes that deliberately run without private memory.
_NON_INTERACTIVE_BOOTSTRAP_MODES = frozenset(
    {"stateless", "stateless_keep_project_rules", "heartbeat_light", "unattended"}
)
#: Provenance kinds of a turn that a person did not type into this session.
_NON_USER_PROVENANCE = frozenset({"inter_session", "internal_system"})

UNAVAILABLE_NOTE = (
    "Automatic memory recall could not finish for this turn. Do not assume no "
    "relevant memory exists; call memory_search if the answer depends on prior context."
)

# ---------------------------------------------------------------------------
# Recall intent
# ---------------------------------------------------------------------------

_I = re.IGNORECASE

_EN_INTENT = [
    re.compile(p, _I)
    for p in (
        r"\b(?:last time|previously|earlier|the other day|a while (?:ago|back)|used to)\b",
        r"\b(?:do|can|could|would|did) you (?:still )?(?:remember|recall)\b",
        r"\b(?:remember|recall) (?:when|what|which|who|where|why|how)\b",
        r"\b(?:we|you|i) (?:already |previously |once )?"
        r"(?:discussed|decided|agreed|said|talked about|chose|mentioned|settled on|picked)\b",
        r"\b(?:previous|earlier|past|last|prior) "
        r"(?:decision|conversation|chat|discussion|session|meeting)s?\b",
        r"\b(?:yesterday|last (?:week|month|year|night)|"
        r"(?:\d+|a|an|one|two|three|few|couple of) (?:days?|weeks?|months?|years?) ago)\b",
        r"\bwhat (?:did|have|had) (?:we|you|i)\b",
        r"\bwhat do i (?:usually|normally|always)\b",
        r"\b(?:which|when|where|why|how) (?:did|have|had) (?:we|you|i) "
        r"(?:decide|choose|pick|discuss|agree|say|mention|talk|use|do|settle)\b",
        r"\b(?:did|have|had) (?:we|you|i) (?:ever |already )?"
        r"(?:decide|choose|discuss|agree|say|mention|talk)\b",
        r"\bremind (?:me|us) (?:what|which|who|where|why|how|when|about)\b",
        r"\b(?:summarize|review|find|search|look up) (?:my |our |the )?"
        r"(?:past |previous |earlier |old )?(?:conversations?|chats?|discussions?)\b",
        r"\bas (?:we|you|i) (?:discussed|agreed|decided|said|mentioned)\b",
    )
]

# Vietnamese, with diacritics. Ambiguous once accents are stripped (e.g.
# "có nhớ" vs "có nhỏ"), so these run on the original text only.
_VI_TIME_BACK = re.compile(
    r"(?:hôm|bữa) (?:trước|qua|kia|hổm)|lần (?:trước|rồi|gần nhất|cuối)|"
    r"(?:tuần|tháng) (?:trước|rồi)|năm (?:ngoái|trước|rồi)|"
    r"trước (?:đây|đó)|(?:dạo|hồi|mấy hôm|mấy bữa) trước|(?:hồi|lúc|ban) nãy",
    _I,
)
_VI_DISCUSSED = re.compile(
    r"(?:đã|từng) (?:bàn|thảo luận|quyết định|chốt|thống nhất|nói|nhắc|đề cập|chọn|trao đổi)|"
    r"(?:quyết định|chốt|thống nhất|bàn|thảo luận)(?: về)? "
    r"(?:gì|những gì|cái gì|chuyện gì|thế nào|như nào|như thế nào|ra sao)\b",
    _I,
)
_VI_REMEMBER = re.compile(
    r"(?:còn|có) nhớ|nhớ (?:không|ko|hông|chứ|chăng|hok|k)\b|nhắc lại",
    _I,
)
_VI_FUTURE = re.compile(
    r"ngày mai|mai (?:nhé|nha)|tuần sau|tháng sau|năm sau|lần sau|sắp tới|sau này|"
    r"tương lai|tối nay|chiều nay|lát nữa",
    _I,
)
# Vietnamese typed without diacritics: only phrases with no common unaccented
# collision. Run on accent-folded text, so partly-accented input matches too.
_VI_FOLDED = re.compile(
    r"\b(?:hom|bua) (?:truoc|qua|kia)\b|\blan truoc\b|\b(?:tuan|thang) truoc\b|"
    r"\bnam ngoai\b|\btruoc (?:day|do)\b|"
    r"\b(?:da|tung) (?:ban bac|thao luan|quyet dinh|chot|thong nhat|trao doi)\b|"
    r"\b(?:quyet dinh|chot|thong nhat|thao luan) (?:gi|nhung gi|cai gi|the nao|ra sao)\b",
    _I,
)

_OTHER_INTENT = [
    re.compile(p, _I)
    for p in (
        # Spanish
        r"\b(?:recuerdas|te acuerdas|[úu]ltima vez|anteriormente|"
        r"qu[ée] decidimos|de qu[ée] hablamos)\b",
        # Portuguese
        r"\b(?:(?:voc[êe] )?(?:se )?lembra (?:o que|do que|quando|onde|como|qual)|"
        r"o que (?:decidimos|combinamos|conversamos)|semana passada)\b",
        # French
        r"(?:tu te souviens|vous vous souvenez|tu te rappelles|la derni[èe]re fois|"
        r"on avait (?:dit|d[ée]cid[ée]|parl[ée])|qu'est-ce qu'on a d[ée]cid[ée])",
        # German
        r"\b(?:erinnerst du dich|letztes mal|was haben wir (?:besprochen|entschieden|gesagt)|"
        r"wie besprochen)\b",
        # Chinese
        r"(?:还记得|還記得|上次|上回|(?:之前|以前)(?:我们|我們)?(?:聊|说|說|讨论|討論|决定|決定)|"
        r"(?:我们|我們)(?:之前|上次)?(?:决定|決定|讨论|討論)了什么)",
        # Japanese
        r"(?:覚えてる|覚えています|前回|(?:この前|以前).{0,24}(?:話した|言った|決めた))",
        # Korean
        r"(?:기억나|기억해|지난번|저번에|예전에.{0,20}(?:얘기|이야기|말|결정)했)",
        # Russian
        r"(?:помнишь|помните|в прошлый раз|мы (?:обсуждали|решили|договорились))",
    )
]


def fold_accents(text: str) -> str:
    """Lowercase ``text`` and strip diacritics (``đ`` folds to ``d``)."""
    decomposed = unicodedata.normalize("NFD", text.lower().replace("đ", "d"))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def has_recall_intent(message: str) -> bool:
    """Return whether ``message`` asks about prior conversations or decisions.

    Deterministic and cheap: a set of phrase patterns per language. It is the
    gate for the deep lane, so a false positive costs one bounded recall call
    (which may well answer NONE) and a false negative leaves the turn to the
    trigger lane and the model's own ``memory_search``.
    """
    text = " ".join((message or "").split())
    if not text:
        return False
    if any(p.search(text) for p in _EN_INTENT):
        return True
    if any(p.search(text) for p in _OTHER_INTENT):
        return True
    if _VI_TIME_BACK.search(text) or _VI_DISCUSSED.search(text):
        return True
    if _VI_REMEMBER.search(text) and not _VI_FUTURE.search(text):
        # "nhớ ... không" is recall; "nhớ nhắc tôi ngày mai" is a reminder.
        return True
    return bool(_VI_FOLDED.search(fold_accents(text)))


def escalation_decision(mode: str, *, strong_trigger_hit: bool, message: str) -> EscalationDecision:
    """Decide whether the deep lane runs for this turn."""
    if mode == "off":
        return "mode-off"
    if mode == "always":
        return "recall"
    if strong_trigger_hit:
        return "strong-trigger-hit"
    return "recall" if has_recall_intent(message) else "no-recall-intent"


# ---------------------------------------------------------------------------
# Session policy
# ---------------------------------------------------------------------------


def recall_skip_reason(
    *,
    enabled: bool,
    session_key: str | None,
    input_mode: str = "user",
    run_kind: str = "default",
    input_provenance: dict[str, Any] | None = None,
    bootstrap_context_mode: str | None = None,
    subagent_caller: bool = False,
    paused: bool = False,
    message: str = "",
) -> str | None:
    """Return why a turn must not run proactive recall, or ``None`` if it may.

    Eligible turns are typed by a person into an interactive, persistent,
    private conversation. Everything else is skipped: heartbeat / cron /
    background and sub-agent runs, inter-session and child-completion
    deliveries, group and channel conversations (private memory must not be
    surfaced to a shared audience), stateless prompts, and paused sessions.
    """
    if not enabled:
        return "disabled"
    if paused:
        return "paused"
    if input_mode != "user":
        return f"input_mode:{input_mode}"
    if run_kind not in _INTERACTIVE_RUN_KINDS:
        return f"run_kind:{run_kind}"
    provenance_kind = str((input_provenance or {}).get("kind") or "")
    if provenance_kind in _NON_USER_PROVENANCE:
        return f"provenance:{provenance_kind}"
    if subagent_caller:
        return "subagent"
    if bootstrap_context_mode in _NON_INTERACTIVE_BOOTSTRAP_MODES:
        return f"bootstrap:{bootstrap_context_mode}"
    if not session_key:
        return "no_session"
    # Function-local: no agentos.memory module imports agentos.session at
    # module scope (see tests/test_memory_session_import.py).
    from agentos.session.keys import allows_private_memory_prompt_injection

    if not allows_private_memory_prompt_injection(session_key):
        return "session_scope"
    text = (message or "").strip()
    if not text:
        return "empty_message"
    if text.startswith("/"):
        return "command"
    return None


# ---------------------------------------------------------------------------
# Trigger lane
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

# Function words and chit-chat that say nothing about which memory is wanted.
# Matched after accent folding, so the Vietnamese entries are unaccented.
_STOPWORDS = frozenset(
    """
    a an the and or but if then than so of to in on at by for from with about into over
    is are was were be been being am do does did done have has had having can could
    will would shall should may might must i me my mine we us our ours you your yours
    he him his she her they them their it its this that these those there here what
    which who whom whose when where why how all any some no not yes ok okay please
    thanks thank hi hello hey just also very really still again more most much many
    one ever already previously last time remember recall said say tell know think
    did decide decided discuss discussed agreed agree talked mention mentioned earlier
    use using used make made get got want need like going go now today
    toi ban minh chung ta tui bon em anh chi no ho cai gi la va hay thi ma nhe nha a
    khong ko co duoc roi da dang se cua cho voi ve tu den trong ngoai nay do kia nhu
    nao sao the vay nhi oi u uh vang da lan truoc hom qua nho con nhac lai
    """.split()
)


def _terms(text: str, *, limit: int = 32) -> list[str]:
    seen: dict[str, None] = {}
    for word in _WORD_RE.findall(fold_accents(text)):
        if len(word) > 1 and word not in _STOPWORDS:
            seen.setdefault(word, None)
            if len(seen) >= limit:
                break
    return list(seen)


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(fold_accents(text)))


def _heading(text: str) -> str:
    """The chunk's first Markdown heading, else its first non-empty line."""
    first = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
        if not first:
            first = stripped
    return first[:120]


def _coverage(overlap: int, total: int) -> float:
    if total <= 0 or overlap <= 0:
        return 0.0
    if total == 1:
        # One shared word is weak evidence on its own: it clears the default
        # threshold only with a near-perfect search score behind it.
        return 0.6
    return (overlap / total) * 0.7 + min(1.0, overlap / 2) * 0.3


def trigger_match_score(message: str, hit: MemorySearchResult) -> float:
    """Deterministic 0-1 strength of ``hit`` as an answer to ``message``.

    Two coverages, the larger wins: how many of the message's content terms
    the note contains, and how many of the note heading's content terms the
    message contains (a note titled "Deploy process" matches "how do we
    deploy?"). Weighted 0.8, with the clamped search score as the other 0.2.
    """
    message_terms = _terms(message)
    if not message_terms:
        return 0.0
    body = hit.text or hit.snippet or ""
    body_words = _words(body)
    message_cov = _coverage(sum(1 for t in message_terms if t in body_words), len(message_terms))
    heading_terms = _terms(_heading(body))
    message_words = set(message_terms)
    heading_cov = _coverage(sum(1 for t in heading_terms if t in message_words), len(heading_terms))
    relevance = max(0.0, min(1.0, float(hit.score or 0.0)))
    return max(message_cov, heading_cov) * 0.8 + relevance * 0.2


def _is_curated_root(path: str) -> bool:
    return str(path).strip("/").lower() in _CURATED_ROOT_FILES


def select_strong_hits(
    message: str,
    hits: Iterable[MemorySearchResult],
    *,
    min_score: float,
    max_hits: int,
) -> list[tuple[float, MemorySearchResult]]:
    """Strong, trusted trigger matches for ``message``, best first.

    Trusted means a curated memory note (``source=memory``) other than the
    root ``MEMORY.md`` / ``USER.md``, which the prompt already carries.
    """
    scored: list[tuple[float, MemorySearchResult]] = []
    seen: set[str] = set()
    for hit in hits:
        if hit.source is not MemorySource.memory or _is_curated_root(hit.path):
            continue
        if hit.chunk_id in seen:
            continue
        seen.add(hit.chunk_id)
        score = trigger_match_score(message, hit)
        if score >= min_score:
            scored.append((score, hit))
    scored.sort(key=lambda item: (-item[0], item[1].path, item[1].start_line))
    return scored[:max_hits]


def _single_line(text: str) -> str:
    return " ".join(text.split())


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return "…"[:limit]
    cut = text[: limit - 1].rstrip()
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space].rstrip()
    return f"{cut}…"


def _citation(hit: MemorySearchResult) -> str:
    return hit.citation or f"{hit.path}#L{hit.start_line}-L{hit.end_line}"


def format_trigger_hits(hits: Sequence[MemorySearchResult], max_chars: int) -> str:
    """Render trigger hits as one bounded bullet list (no fence)."""
    if not hits:
        return ""
    per_hit = max(80, max_chars // len(hits))
    lines: list[str] = []
    used = 0
    for hit in hits:
        source = f" (Source: {_citation(hit)})"
        excerpt = _truncate(_single_line(hit.text or hit.snippet or ""), per_hit - len(source) - 2)
        line = f"- {excerpt}{source}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Deep lane
# ---------------------------------------------------------------------------

DEEP_RECALL_SYSTEM_PROMPT = """\
You are a memory recall step. Another model is preparing the final answer to the user.
You are given the user's latest message and excerpts retrieved from the agent's memory
(curated notes, indexed past conversations, knowledge base). Your only job is to decide
which of those excerpts would materially help answer the latest message, and to restate
that as a short note for the other model.

Rules:
- Use only the provided excerpts. Do not use outside knowledge and do not guess.
- Treat the latest user message as the query. Return memory only if it clearly helps with it.
- If the connection is weak, indirect, or only topically similar, reply with NONE.
- Facts that go stale (deployments, service health, job status, prices) must keep their date
  if the excerpt has one, and be flagged as possibly stale.
- Write a memory note about the user and past work, not a reply to the user.
- Do not explain your reasoning. No bullets, numbering, labels, XML, JSON, or markdown.

Return exactly one of:
1. NONE
2. one compact plain-text summary under {max_chars} characters

Examples:
User message: What did we decide about the database for billing?
Return: Decided on Postgres over MySQL for billing (2026-10-02 note), mainly for JSONB support.
User message: Explain DNS over HTTPS.
Return: NONE
"""

_NONE_VALUES = frozenset({"none", "none.", "null", "n/a", "no", "nothing", "no relevant memory"})


def normalize_summary(raw: str, max_chars: int) -> str | None:
    """Collapse a deep-lane reply to one line; ``None`` for a NONE answer."""
    line = _single_line(raw or "")
    line = re.sub(r"^(?:return|memory|summary)\s*:\s*", "", line, flags=re.IGNORECASE)
    if not line or line.lower().strip(" .\"'`") in _NONE_VALUES or line.upper().startswith("NONE"):
        return None
    return _truncate(line, max_chars)


async def _expand_memory_hit(workspace_dir: Path | None, hit: MemorySearchResult) -> str:
    """Read the hit's lines plus a little context, like ``memory_get`` would."""
    fallback = hit.text or hit.snippet or ""
    if workspace_dir is None or hit.source is not MemorySource.memory:
        return fallback
    try:
        root = workspace_dir.resolve()
        file_path = (root / hit.path).resolve()
        file_path.relative_to(root)
    except (OSError, ValueError):
        return fallback

    def _read() -> str:
        lines = file_path.read_text(encoding="utf-8", errors="replace").split("\n")
        start = max(0, hit.start_line - 1 - DEEP_CONTEXT_LINES)
        end = min(len(lines), hit.end_line + DEEP_CONTEXT_LINES)
        return "\n".join(lines[start:end])

    try:
        return await asyncio.to_thread(_read)
    except OSError:
        return fallback


async def gather_deep_evidence(
    hits: Sequence[MemorySearchResult], workspace_dir: Path | None
) -> list[tuple[MemorySearchResult, str]]:
    """Bounded excerpts for the deep lane, in hit order."""
    evidence: list[tuple[MemorySearchResult, str]] = []
    used = 0
    for hit in hits:
        excerpt = _truncate(
            (await _expand_memory_hit(workspace_dir, hit)).strip(), DEEP_EXCERPT_CHARS
        )
        if not excerpt:
            continue
        if used + len(excerpt) > DEEP_EVIDENCE_CHARS:
            break
        evidence.append((hit, excerpt))
        used += len(excerpt)
    return evidence


def build_deep_recall_prompt(
    message: str, evidence: Sequence[tuple[MemorySearchResult, str]], *, max_chars: int
) -> tuple[str, str]:
    """``(system, user)`` prompt pair for the deep-lane model call."""
    blocks = [
        f"[{i}] {_citation(hit)} (source: {hit.source.value})\n{excerpt}"
        for i, (hit, excerpt) in enumerate(evidence, 1)
    ]
    user = (
        f"Latest user message:\n{_truncate(message, MAX_QUERY_CHARS * 2)}\n\n"
        "Memory excerpts:\n\n" + "\n\n".join(blocks)
    )
    return DEEP_RECALL_SYSTEM_PROMPT.format(max_chars=max_chars), user


# ---------------------------------------------------------------------------
# Per-session state
# ---------------------------------------------------------------------------


@dataclass
class RecallRecord:
    """What proactive recall did on one turn (for status / debugging)."""

    session_key: str
    status: str  # injected | none | skipped | unavailable | error
    at: float = field(default_factory=time.time)
    reason: str = ""
    decision: str = ""
    trigger_hits: int = 0
    deep_hits: int = 0
    elapsed_ms: int = 0
    injected_chars: int = 0
    block: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ActiveRecallState:
    """Process-wide pause switches and last outcome per session.

    Held in memory: a pause lasts until it is lifted or the gateway restarts.
    Bounded LRU, so an unbounded number of sessions cannot grow it.
    """

    def __init__(self, *, max_sessions: int = 512) -> None:
        self._max = max_sessions
        self._paused: set[str] = set()
        self._records: OrderedDict[str, RecallRecord] = OrderedDict()

    @staticmethod
    def _key(session_key: str) -> str:
        from agentos.session.keys import canonicalize_session_key

        return canonicalize_session_key(session_key) or session_key

    def set_paused(self, session_key: str, paused: bool) -> None:
        key = self._key(session_key)
        if paused:
            self._paused.add(key)
        else:
            self._paused.discard(key)

    def is_paused(self, session_key: str | None) -> bool:
        return bool(session_key) and self._key(str(session_key)) in self._paused

    def paused_sessions(self) -> list[str]:
        return sorted(self._paused)

    def record(self, record: RecallRecord) -> None:
        key = self._key(record.session_key)
        self._records[key] = record
        self._records.move_to_end(key)
        while len(self._records) > self._max:
            self._records.popitem(last=False)

    def last(self, session_key: str) -> RecallRecord | None:
        return self._records.get(self._key(session_key))

    def recent(self, limit: int = 20) -> list[RecallRecord]:
        return list(reversed(self._records.values()))[:limit]

    def clear(self) -> None:
        self._paused.clear()
        self._records.clear()


_STATE = ActiveRecallState()


def get_active_recall_state() -> ActiveRecallState:
    return _STATE


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

CompleteFn = Callable[[str, str], Awaitable[str]]
BudgetFn = Callable[..., Awaitable[str]]


def _setting(config: Any, name: str, default: Any) -> Any:
    value = getattr(config, name, default) if config is not None else default
    return default if value is None else value


def fence_recall_block(inner: str, max_chars: int) -> str:
    """Fence ``inner`` as background memory context, capped at ``max_chars`` total."""
    from agentos.memory.providers.fencing import build_memory_context_block

    if not inner.strip():
        return ""
    overhead = len(build_memory_context_block("x", authoritative=False)) - 1
    body = _truncate(inner, max(40, max_chars - overhead))
    return build_memory_context_block(body, authoritative=False)


@dataclass
class _LaneOneResult:
    text: str = ""
    hits: int = 0


class ActiveRecall:
    """Runs both lanes for one turn and returns the fenced block (or ``""``)."""

    def __init__(self, state: ActiveRecallState | None = None) -> None:
        self.state = state or get_active_recall_state()

    async def _trigger_lane(
        self, retriever: Any, message: str, *, min_score: float, max_hits: int, max_chars: int
    ) -> _LaneOneResult:
        query = _truncate(message, MAX_QUERY_CHARS)
        if not _terms(query):
            return _LaneOneResult()
        opts = MemorySearchOpts(
            max_results=TRIGGER_CANDIDATES, min_score=0.0, source=MemorySource.memory
        )
        # Lexical only: no query embedding, so no remote call on this lane.
        hits = await retriever.search(
            query, opts, intent=SearchIntent.AUTO_RECALL, lexical_only=True
        )
        strong = select_strong_hits(message, hits, min_score=min_score, max_hits=max_hits)
        chosen = [hit for _, hit in strong]
        return _LaneOneResult(text=format_trigger_hits(chosen, max_chars), hits=len(chosen))

    async def _deep_lane(
        self,
        retriever: Any,
        message: str,
        *,
        workspace_dir: Path | None,
        complete: CompleteFn,
        max_results: int,
        max_summary_chars: int,
    ) -> tuple[str | None, int]:
        query = _truncate(message, MAX_QUERY_CHARS)
        opts = MemorySearchOpts(max_results=max_results, min_score=DEEP_MIN_SCORE, source=None)
        hits = [
            hit
            for hit in await retriever.search(query, opts, intent=SearchIntent.AUTO_RECALL)
            if not (hit.source is MemorySource.memory and _is_curated_root(hit.path))
        ]
        evidence = await gather_deep_evidence(hits, workspace_dir)
        if not evidence:
            return None, 0
        system, user = build_deep_recall_prompt(message, evidence, max_chars=max_summary_chars)
        summary = normalize_summary(await complete(system, user), max_summary_chars)
        if summary is None:
            return None, len(evidence)
        sources = ", ".join(_citation(hit) for hit, _ in evidence[:3])
        return f"{summary}\n(Sources searched: {sources})", len(evidence)

    async def recall(
        self,
        *,
        session_key: str,
        message: str,
        retriever: Any,
        config: Any,
        trigger_timeout: float,
        complete: CompleteFn | None,
        workspace_dir: Path | None = None,
        budget: BudgetFn | None = None,
        budget_key: Any = None,
    ) -> str:
        """Run the lanes for one eligible turn; never raises.

        ``budget`` is :meth:`RecallBudget.recall` (or a stand-in): the trigger
        lane runs through it so a slow index search cannot hold the turn past
        ``trigger_timeout``. ``complete(system, user)`` makes the deep-lane
        model call; ``None`` disables the deep lane.
        """
        started = time.monotonic()
        record = RecallRecord(session_key=session_key, status="none")
        try:
            block = await self._recall(
                record,
                message=message,
                retriever=retriever,
                config=config,
                trigger_timeout=trigger_timeout,
                complete=complete,
                workspace_dir=workspace_dir,
                budget=budget,
                budget_key=budget_key,
            )
        except Exception as exc:  # noqa: BLE001 -- recall must never fail the turn
            record.status = "error"
            record.reason = str(exc)[:200]
            block = ""
        record.elapsed_ms = int((time.monotonic() - started) * 1000)
        record.block = block
        record.injected_chars = len(block)
        if block and record.status == "none":
            record.status = "injected"
        self.state.record(record)
        log.info(
            "memory.active_recall",
            session_key=session_key,
            status=record.status,
            decision=record.decision,
            reason=record.reason or None,
            trigger_hits=record.trigger_hits,
            deep_hits=record.deep_hits,
            elapsed_ms=record.elapsed_ms,
            injected_chars=record.injected_chars,
        )
        return block

    async def _recall(
        self,
        record: RecallRecord,
        *,
        message: str,
        retriever: Any,
        config: Any,
        trigger_timeout: float,
        complete: CompleteFn | None,
        workspace_dir: Path | None,
        budget: BudgetFn | None,
        budget_key: Any,
    ) -> str:
        mode = str(_setting(config, "mode", "escalate"))
        max_chars = int(_setting(config, "max_injected_chars", 1800))
        min_score = float(_setting(config, "trigger_min_score", 0.65))
        max_hits = int(_setting(config, "trigger_max_hits", 3))

        lane_one = _LaneOneResult()

        async def _fetch_trigger() -> str:
            nonlocal lane_one
            lane_one = await self._trigger_lane(
                retriever, message, min_score=min_score, max_hits=max_hits, max_chars=max_chars
            )
            return lane_one.text

        if budget is not None:
            trigger_text = await budget(
                budget_key,
                _fetch_trigger,
                timeout=trigger_timeout,
                log_fields={"lane": "active_recall_trigger"},
            )
        else:
            try:
                trigger_text = await asyncio.wait_for(_fetch_trigger(), timeout=trigger_timeout)
            except TimeoutError:
                trigger_text = ""
        record.trigger_hits = lane_one.hits if trigger_text else 0

        decision = escalation_decision(mode, strong_trigger_hit=bool(trigger_text), message=message)
        record.decision = decision
        parts: list[str] = []
        if trigger_text:
            parts.append(trigger_text)

        if decision == "recall" and complete is not None:
            deep_timeout = float(_setting(config, "deep_timeout_seconds", 8.0))
            try:
                summary, record.deep_hits = await asyncio.wait_for(
                    self._deep_lane(
                        retriever,
                        message,
                        workspace_dir=workspace_dir,
                        complete=complete,
                        max_results=int(_setting(config, "deep_max_results", 6)),
                        max_summary_chars=int(_setting(config, "deep_max_summary_chars", 400)),
                    ),
                    timeout=deep_timeout,
                )
            except Exception as exc:  # noqa: BLE001 -- timeout, provider or index failure
                record.status = "unavailable"
                record.reason = (
                    f"deep recall timed out after {deep_timeout:g}s"
                    if isinstance(exc, TimeoutError)
                    else str(exc)[:200]
                )
                summary = None
                if not parts:
                    parts.append(UNAVAILABLE_NOTE)
            if summary:
                parts.append(summary)
        elif decision == "recall":
            record.reason = "deep recall has no model"

        return fence_recall_block("\n\n".join(parts), max_chars) if parts else ""


def message_budget_key(agent_id: str, session_key: str, message: str) -> tuple[str, str, str, str]:
    """:class:`RecallBudget` key for a trigger search: one per session *and* message.

    Keying on the message means a late result is only ever reused for the
    same question, never handed to a different turn's prompt.
    """
    digest = hashlib.sha1(message.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    return ("active_recall", agent_id, session_key, digest)
