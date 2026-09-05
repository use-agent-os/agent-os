"""Deterministic task-type detection for router tier policy.

The Pilot classifier scores *reasoning difficulty*, not task type, and it was
trained on an English-only corpus (see ``scripts/pilot_router/DATA.md``). Two
consequences motivate this module:

* Translation is scored as ordinary work — an English "translate this
  paragraph" lands on ``R1``/``c1`` even though an operator's policy may be
  that translation never needs more than the cheapest tier.
* Non-English input is out of distribution, so the same request drifts by a
  tier or more depending only on the language it happens to be written in.

Both are policy questions rather than model-accuracy questions, so they are
answered deterministically here instead of by retraining. This module is a
pure function: no I/O, no config loading, no model. The engine step owns when
to consult it and which ceiling to apply.

**Detection contract.** A message is ``"translate"`` when a translate verb
matches in the instruction window. Every detected translation is treated the
same — asking for an explanation alongside the translation, or for a poem's
metre to survive it, does not change the verdict, because the operator policy
this serves is that translation as such never needs more than the ceiling
tier.

Detection is nevertheless deliberately asymmetric — a miss only forfeits a
saving, a false positive sends real work to the cheapest tier — so anything
that is not translation returns ``None``. Three rules keep that honest:

* Only **verb** forms are matched, never the noun. ``translate this to
  French`` is an instruction; ``the address translation bug`` is not, and a
  developer's message should never be mistaken for the former.
* Overloaded verbs carry explicit negative guards. Vietnamese ``dịch`` also
  appears in ``giao dịch`` (transaction), ``dịch vụ`` (service), ``dịch
  bệnh`` (epidemic); Thai ``แปล`` is a prefix of ``แปลก`` (strange).
* A **programming language** as the target is a port, not a translation.
  "Translate this Python module to Rust" is a request to write code, and it
  is the one case that shares the verb without sharing the task.

**Instruction window.** Only the first and last paragraph of a message are
examined, each length-capped. Instructions bracket a pasted body rather than
hide inside it, and paragraph boundaries — not a raw character count — are
what separate the two: an article being translated routinely contains the word
"explain" or "analysis" in its body, and reading those as escalation signals
would suppress most genuine requests. The same window bounds verb matching, so
a document that merely *mentions* translation does not trip the detector
either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Task type ids this module can return.
TASK_TYPE_TRANSLATE = "translate"

#: Caps on the leading and trailing paragraph that form the instruction window.
#: Generous enough for a multi-sentence instruction, short enough that a body
#: run together with its instruction contributes only its opening.
INSTRUCTION_HEAD_CHARS = 600
INSTRUCTION_TAIL_CHARS = 300

#: A blank line (optionally carrying whitespace) separates paragraphs.
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")

# --------------------------------------------------------------------------- #
# Translate verbs, per language
# --------------------------------------------------------------------------- #
# Each entry is (language_tag, pattern), matched case-insensitively against the
# scan window. Noun forms (translation / traduction / Übersetzung / перевод)
# are deliberately absent: they are what appears in technical prose, and
# matching them is how "fix the address translation bug" becomes a c0 task.

_TRANSLATE_PATTERNS: tuple[tuple[str, str], ...] = (
    # English — imperative/infinitive only.
    ("en", r"\btranslate\b"),
    # Vietnamese — "dịch" is heavily overloaded, so require the verb sense:
    # either an explicit direction ("sang/ra/qua/thành ...") close by, or an
    # object/politeness particle that only the verb takes.
    (
        "vi",
        r"(?<!\bgiao )(?<!\bphiên )\bdịch\b"
        r"(?!\s*(?:vụ|bệnh|chuyển|tễ|hạch|giả|thuật))"
        r"(?=[^\n]{0,80}?\b(?:sang|ra|qua|thành)\b"
        r"|\s*(?:giúp|hộ|dùm|giùm|đoạn|câu|bài|văn bản|tài liệu|toàn bộ|nội dung|cái|này))",
    ),
    # Chinese — simplified and traditional.
    ("zh", r"翻译|翻譯|译成|譯成|翻成|译为|譯為"),
    # Japanese — 翻訳 or the て/plain form of 訳す (bare 訳 is "reason").
    ("ja", r"翻訳|訳して|訳す"),
    # Korean — 번역.
    ("ko", r"번역"),
    # Thai — แปล, but not แปลก ("strange") or แปลง ("convert").
    ("th", r"แปล(?![กง])"),
    # Indonesian/Malay.
    ("id", r"\bterjemah\w*\b|\balih\s?bahasa\w*\b"),
    # French — verb forms only.
    ("fr", r"\btradui(?:s|re|sez|t|sons)\b"),
    # Spanish — verb forms only.
    ("es", r"\btraduce\b|\btraducir\b|\btraduzca\b|\btraducid\b|\btraduzcan\b"),
    # German — verb forms only, incl. the ue- transliteration.
    ("de", r"\b(?:ü|ue)bersetz(?:e|en|t|st)\b"),
    # Portuguese — verb forms only.
    ("pt", r"\btraduza\b|\btraduzir\b|\btraduz\b|\btraduzam\b"),
    # Russian — imperative and infinitive only (noun "перевод" excluded).
    ("ru", r"\bперевед(?:и|ите)\b|\bперевести\b"),
    # Arabic — ترجم and its inflections.
    ("ar", r"ترجم\w*"),
    # Hindi — अनुवाद (used with a light verb).
    ("hi", r"अनुवाद"),
)

_TRANSLATE_RES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (lang, re.compile(pattern, re.IGNORECASE)) for lang, pattern in _TRANSLATE_PATTERNS
)

# --------------------------------------------------------------------------- #
# Not-a-translation guard
# --------------------------------------------------------------------------- #

#: A programming language named alongside the verb: this is a port/rewrite that
#: happens to be phrased as "translate", and it is a request to write code. This
#: is the only guard — it exists because the task is different, not because the
#: translation is judged hard. Suppression is the safe direction: it forfeits a
#: saving and hands the turn back to ordinary model routing.
_CODE_TARGET_RE = re.compile(
    r"\b(?:python|javascript|typescript|golang|rust|java|kotlin|swift|scala"
    r"|haskell|ruby|php|perl|sql|bash|powershell|matlab|fortran|cobol"
    r"|react|vue|svelte|jquery|regex|assembly|solidity"
    r"|dart|elixir|erlang|clojure|lua|zig)\b"
    r"|\b(?:c\+\+|c#)(?!\w)"
    r"|(?<!\w)\.net\b",
    re.IGNORECASE,
)

BLOCK_CODE_TARGET = "programming_language_target"


@dataclass(frozen=True, slots=True)
class TaskTypeVerdict:
    """Outcome of task-type detection for one message.

    ``task_type`` is ``None`` when nothing matched or when an escalation signal
    suppressed a match. ``matched_language`` and ``evidence`` name the pattern
    that fired, so a routing decision stays explainable after the fact;
    ``blocked_by`` names the escalation signal when one suppressed an
    otherwise-matching verb.
    """

    task_type: str | None = None
    matched_language: str | None = None
    evidence: str | None = None
    blocked_by: str | None = None


def instruction_window(message: str) -> str:
    """Return the leading and trailing paragraph of *message*, length-capped.

    A single-paragraph message contributes only its opening; otherwise the
    first paragraph's head and the last paragraph's tail are joined, and
    everything between them — the pasted body — is excluded.
    """
    blocks = [block.strip() for block in _PARAGRAPH_SPLIT_RE.split(message)]
    blocks = [block for block in blocks if block]
    if not blocks:
        return ""
    head = blocks[0][:INSTRUCTION_HEAD_CHARS]
    if len(blocks) == 1:
        return head
    return f"{head}\n{blocks[-1][-INSTRUCTION_TAIL_CHARS:]}"


def detect_task_type(message: str) -> TaskTypeVerdict:
    """Classify *message* into a coarse task type, or ``None`` when unsure.

    Only ``"translate"`` is recognised today. See the module docstring for the
    detection contract and why it errs toward returning ``None``.
    """
    if not message or not message.strip():
        return TaskTypeVerdict()

    window = instruction_window(message)

    match: re.Match[str] | None = None
    language: str | None = None
    for lang, pattern in _TRANSLATE_RES:
        found = pattern.search(window)
        if found is not None:
            match, language = found, lang
            break

    if match is None:
        return TaskTypeVerdict()

    evidence = match.group(0).strip()[:40]

    # Checked inside the instruction window, not the whole message: a document
    # being translated may well mention Python without being a porting request.
    if _CODE_TARGET_RE.search(window) is not None:
        return TaskTypeVerdict(
            matched_language=language,
            evidence=evidence,
            blocked_by=BLOCK_CODE_TARGET,
        )

    return TaskTypeVerdict(
        task_type=TASK_TYPE_TRANSLATE,
        matched_language=language,
        evidence=evidence,
    )
