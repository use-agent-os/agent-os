"""Message provenance and the cross-model replay rule.

A session can change model between requests: a router tier change, a
failover to the next provider in the chain, or ``/model``. Two things in a
stored assistant message are only valid for the model (or API family) that
produced it:

* **Signed or encrypted reasoning.** Anthropic ``thinking`` signatures and
  ``redacted_thinking`` data, OpenRouter ``reasoning_details`` and Gemini
  ``thought_signature`` values are verified by the issuer. Another model
  rejects them, or the request silently degrades.
* **Tool-call ids.** Each API accepts its own id charset and length (Anthropic
  ``[A-Za-z0-9_-]``, OpenAI at most 40 characters, Mistral exactly nine
  alphanumerics). An id minted by one provider can be invalid for another.

Every assistant message records its :class:`MessageProvenance` when it is
built. Right before a provider turns the history into its wire format it calls
:func:`prepare_replay` with its own identity as the target. Messages from the
same model pass through untouched (signatures kept, so prompt caching and
preserved thinking keep working). Messages from another model lose their
signed reasoning (plain text and ``reasoning_content`` stay) and have their
tool-call ids rewritten to the target's format, with the matching tool results
rewritten the same way so every call/result pair still lines up. A message
without provenance (persisted before it was recorded) counts as "same model".

The rewrite is a pure function of the messages and the target, so request
N+1 repeats request N's prefix byte for byte; stored history is never changed.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agentos.provider.protocol import provider_metadata
from agentos.provider.types import (
    ContentBlockRedactedThinking,
    ContentBlockText,
    ContentBlockThinking,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    MessageProvenance,
)

__all__ = [
    "ToolIdFormat",
    "model_api_family",
    "prepare_replay",
    "provenance_for_provider",
    "same_origin",
    "tool_id_format_for",
]

# Vendor prefix of a routed model id (``anthropic/claude-...``) -> family.
_VENDOR_FAMILIES: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "azure-openai": "openai",
    "google": "google",
    "x-ai": "xai",
    "xai": "xai",
    "deepseek": "deepseek",
    "minimax": "minimax",
    "mistralai": "mistral",
    "mistral": "mistral",
    "qwen": "qwen",
    "alibaba": "qwen",
    "moonshotai": "moonshot",
    "moonshot": "moonshot",
    "z-ai": "zhipu",
    "zhipuai": "zhipu",
    "meta-llama": "meta",
}

# Substrings of the bare model name -> family, checked in order.
_NAME_FAMILIES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"claude"), "anthropic"),
    (re.compile(r"^(?:chatgpt|codex|gpt|o[1-9](?:-|$))|gpt-"), "openai"),
    (re.compile(r"gemini|gemma"), "google"),
    (re.compile(r"grok"), "xai"),
    (re.compile(r"deepseek"), "deepseek"),
    (re.compile(r"minimax"), "minimax"),
    (re.compile(r"mistral|mixtral|codestral|magistral|devstral|pixtral|ministral"), "mistral"),
    (re.compile(r"qwen|qwq"), "qwen"),
    (re.compile(r"kimi|moonshot"), "moonshot"),
    (re.compile(r"glm|zhipu"), "zhipu"),
    (re.compile(r"llama"), "meta"),
)

# Provider kind -> family, for model ids that name no family themselves.
# Gateways serving many families (OpenRouter, OpenCAP, ...) are absent on
# purpose: their kind says nothing about who signed a message.
_PROVIDER_FAMILIES: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "openai_responses": "openai",
    "azure": "openai",
    "gemini": "google",
    "deepseek": "deepseek",
    "minimax": "minimax",
    "mistral": "mistral",
    "dashscope": "qwen",
    "bailian_coding": "qwen",
    "moonshot": "moonshot",
    "zhipu": "zhipu",
}


def _normalize_model_id(model: str | None) -> str:
    return (model or "").strip().lower().lstrip("~")


def model_api_family(model: str | None, provider_kind: str | None = None) -> str:
    """The API family of *model* (``anthropic``, ``openai``, ...), or ``""``.

    Read from the model id first (vendor prefix, then the bare name), then from
    *provider_kind* for single-family providers. ``""`` means unknown.
    """
    normalized = _normalize_model_id(model)
    if "/" in normalized:
        vendor, _, name = normalized.partition("/")
        family = _VENDOR_FAMILIES.get(vendor)
        if family:
            return family
        normalized = name.rsplit("/", 1)[-1]
    if normalized:
        for pattern, family in _NAME_FAMILIES:
            if pattern.search(normalized):
                return family
    return _PROVIDER_FAMILIES.get((provider_kind or "").strip().lower(), "")


def provenance_for_provider(provider: object | None) -> MessageProvenance | None:
    """The :class:`MessageProvenance` a message from *provider* should carry.

    Uses the provider's public metadata, so a failover wrapper reports the
    provider it currently delegates to, i.e. the one that actually answered.
    ``None`` when the provider exposes no model id.
    """
    metadata = provider_metadata(provider)
    model = (metadata.model or "").strip()
    if not model:
        return None
    kind = (metadata.provider_kind or metadata.provider_name or "").strip()
    return MessageProvenance(
        provider=kind or None,
        model=model,
        api=model_api_family(model, kind) or None,
    )


def same_origin(source: MessageProvenance | None, target: MessageProvenance | None) -> bool:
    """Whether a message from *source* may be replayed unchanged to *target*.

    Unknown provenance on either side counts as the same model. Otherwise the
    model ids must match and, when both are known, so must the API families.
    The provider kind is not compared: the same model reached through another
    route accepts its own signatures.
    """
    if source is None or target is None:
        return True
    source_model = _normalize_model_id(source.model)
    target_model = _normalize_model_id(target.model)
    if not source_model or not target_model:
        return True
    if source_model != target_model:
        return False
    if source.api and target.api and source.api != target.api:
        return False
    return True


@dataclass(frozen=True)
class ToolIdFormat:
    """The tool-call ids a target API accepts."""

    #: Characters an id may contain (one regex character class).
    allowed: str = "A-Za-z0-9_-"
    max_length: int = 64
    #: Exact length the API requires, if any.
    exact_length: int | None = None

    def accepts(self, tool_id: str) -> bool:
        if not tool_id:
            return False
        if self.exact_length is not None and len(tool_id) != self.exact_length:
            return False
        if len(tool_id) > self.max_length:
            return False
        return re.fullmatch(f"[{self.allowed}]+", tool_id) is not None

    def normalize(self, tool_id: str) -> str:
        """*tool_id* rewritten to this format; identical when already valid.

        Deterministic and context free: the same id always maps to the same
        result, so a request built twice is byte-identical. A short digest of
        the original id keeps distinct ids distinct after characters are
        replaced or the id is shortened.
        """
        if self.accepts(tool_id):
            return tool_id
        digest = hashlib.sha256(tool_id.encode("utf-8")).hexdigest()
        if self.exact_length is not None:
            return digest[: self.exact_length]
        cleaned = re.sub(f"[^{self.allowed}]+", "_", tool_id).strip("_")
        suffix = digest[:10]
        head = cleaned[: max(0, self.max_length - len(suffix) - 1)].rstrip("_")
        return f"{head}_{suffix}" if head else f"call_{suffix}"[: self.max_length]


#: Anthropic Messages API (also the format Claude behind a gateway expects).
ANTHROPIC_TOOL_IDS = ToolIdFormat(max_length=64)
#: OpenAI Chat Completions / Responses: at most 40 characters.
OPENAI_TOOL_IDS = ToolIdFormat(max_length=40)
#: Mistral: exactly nine letters or digits.
MISTRAL_TOOL_IDS = ToolIdFormat(allowed="A-Za-z0-9", max_length=9, exact_length=9)


def tool_id_format_for(target: MessageProvenance | None) -> ToolIdFormat:
    """The tool-id format of the API family *target* belongs to."""
    family = (target.api if target else None) or ""
    if family == "mistral":
        return MISTRAL_TOOL_IDS
    if family == "openai":
        return OPENAI_TOOL_IDS
    return ANTHROPIC_TOOL_IDS


_REASONING_BLOCKS = (ContentBlockThinking, ContentBlockRedactedThinking)


def _foreign_assistant_view(
    message: Message,
    *,
    tool_ids: ToolIdFormat,
    id_map: dict[str, str],
) -> Message | None:
    """*message* as another model may see it; ``None`` drops the message."""
    if isinstance(message.content, str):
        if message.reasoning_details is None:
            return message
        return message.model_copy(update={"reasoning_details": None})

    blocks: list[Any] = []
    dropped_thinking: list[str] = []
    changed = message.reasoning_details is not None
    for block in message.content:
        if isinstance(block, _REASONING_BLOCKS):
            if isinstance(block, ContentBlockThinking) and block.thinking.strip():
                dropped_thinking.append(block.thinking)
            changed = True
            continue
        if isinstance(block, ContentBlockToolUse):
            new_id = tool_ids.normalize(block.id)
            if new_id != block.id:
                id_map[block.id] = new_id
            else:
                id_map.pop(block.id, None)
            if new_id != block.id or block.thought_signature is not None:
                block = block.model_copy(update={"id": new_id, "thought_signature": None})
                changed = True
        blocks.append(block)

    if not changed:
        return message
    if not blocks:
        # A reply that was only reasoning: keep its plain text so the turn is
        # not lost, or drop the message when there is nothing readable left.
        if not dropped_thinking:
            return None
        blocks = [ContentBlockText(text="\n\n".join(dropped_thinking))]
    return message.model_copy(update={"content": blocks, "reasoning_details": None})


def _remap_tool_results(message: Message, id_map: dict[str, str]) -> Message:
    if isinstance(message.content, str):
        return message
    changed = False
    blocks: list[Any] = []
    for block in message.content:
        if isinstance(block, ContentBlockToolResult) and block.tool_use_id in id_map:
            block = block.model_copy(update={"tool_use_id": id_map[block.tool_use_id]})
            changed = True
        blocks.append(block)
    return message.model_copy(update={"content": blocks}) if changed else message


def _note_native_tool_ids(message: Message, id_map: dict[str, str]) -> None:
    """A same-model call reusing an id ends the earlier foreign mapping."""
    if isinstance(message.content, str):
        return
    for block in message.content:
        if isinstance(block, ContentBlockToolUse):
            id_map.pop(block.id, None)


def prepare_replay(
    messages: Sequence[Message],
    target: MessageProvenance | None,
    *,
    tool_ids: ToolIdFormat | None = None,
) -> list[Message]:
    """*messages* as they may be sent to *target* (see the module docstring).

    Returns a new list only when something changed; otherwise the input list
    itself (when it is a list), so the common same-model path costs nothing.
    """
    if target is None:
        return messages if isinstance(messages, list) else list(messages)
    id_format = tool_ids or tool_id_format_for(target)
    id_map: dict[str, str] = {}
    out: list[Message] = []
    changed = False
    for message in messages:
        view: Message | None = message
        if message.role == "assistant":
            if same_origin(message.provenance, target):
                _note_native_tool_ids(message, id_map)
            else:
                view = _foreign_assistant_view(message, tool_ids=id_format, id_map=id_map)
        elif id_map:
            view = _remap_tool_results(message, id_map)
        if view is not message:
            changed = True
        if view is not None:
            out.append(view)
    if not changed:
        return messages if isinstance(messages, list) else list(messages)
    return out
