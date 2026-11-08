"""Tool-result projection, capping and argument rehydration for the agent loop.

Moved verbatim out of ``agentos.engine.agent`` (OpenSpec change
``refactor-agent-harness-core``, Phase 3). ``ToolResultProjectionMixin`` keeps
the methods' ``self`` access to the agent's config and per-turn state; a later
step turns it into a composed object.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Protocol

import structlog

from agentos.context_budget import ContextBudgetClass, ContextBudgetGovernor
from agentos.engine.tool_result_store import (
    ToolResultRecord,
    ToolResultStore,
    ToolResultStoreBudgetError,
)
from agentos.engine.tool_token_estimate import estimate_tokens as get_approx_tokens
from agentos.engine.transcript import (
    COMPACTED_TOOL_ARGUMENT_MARKERS as _COMPACTED_TOOL_ARGUMENT_MARKERS,
)
from agentos.engine.transcript import (
    HISTORICAL_TOOL_ARGUMENT_PROJECTION_PREFIX as _HISTORICAL_TOOL_ARGUMENT_PROJECTION_PREFIX,
)
from agentos.engine.transcript import (
    INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY as _INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY,
)
from agentos.engine.transcript import (
    INVALID_PROVIDER_CONTEXT_PROJECTION_PREFIX as _INVALID_PROVIDER_CONTEXT_PROJECTION_PREFIX,
)
from agentos.engine.transcript import (
    TOOL_ARGUMENT_PROJECTION_PREFIX as _TOOL_ARGUMENT_PROJECTION_PREFIX,
)
from agentos.engine.transcript import (
    MessageSizeCache,
    history_argument_projection,
)
from agentos.engine.types import (
    AgentConfig,
    ToolCall,
    ToolResult,
)
from agentos.execution_status import (
    mark_execution_status_truncated,
    runtime_execution_status,
)
from agentos.provider import (
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
)
from agentos.result_budget import (
    ToolResultBudgetClass,
    compact_tool_result_content,
    resolve_budget_class,
)

logger = structlog.get_logger("agentos.engine.agent")


_LARGE_JSON_TOOL_FIELD_KEYS: frozenset[str] = frozenset({"body", "body_base64"})


_LARGE_JSON_TOOL_FIELD_CHARS = 20_000


def _large_json_field_replacement(value: str) -> dict[str, object]:
    return {
        "omitted": True,
        "omitted_chars": len(value),
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "reason": "large_tool_result_field",
    }


def _omit_large_json_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, dict):
        changed = False
        sanitized_dict: dict[str, Any] = {}
        for key, item in value.items():
            if (
                key in _LARGE_JSON_TOOL_FIELD_KEYS
                and isinstance(item, str)
                and len(item) > _LARGE_JSON_TOOL_FIELD_CHARS
            ):
                sanitized_dict[key] = _large_json_field_replacement(item)
                changed = True
                continue
            sanitized, child_changed = _omit_large_json_value(item)
            sanitized_dict[key] = sanitized
            changed = changed or child_changed
        return sanitized_dict, changed
    if isinstance(value, list):
        changed = False
        sanitized_list: list[Any] = []
        for item in value:
            sanitized, child_changed = _omit_large_json_value(item)
            sanitized_list.append(sanitized)
            changed = changed or child_changed
        return sanitized_list, changed
    return value, False


_PENDING_APPROVAL_STATUSES: frozenset[str] = frozenset({"approval_required", "approval_pending"})


_PROVIDER_CONTEXT_PROJECTION_REUSED_REASON = "provider_context_projection_reused"


def _omit_large_json_tool_fields(content: str) -> tuple[str, bool]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return content, False
    sanitized, changed = _omit_large_json_value(parsed)
    if not changed:
        return content, False
    return json.dumps(sanitized, ensure_ascii=False, indent=2), True


def _pending_approval_payload(content: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("status") not in _PENDING_APPROVAL_STATUSES:
        return None
    approval_id = payload.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id:
        return None
    return payload


def _tool_result_content_has_artifact(content: str) -> bool:
    try:
        payload = json.loads(content)
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("artifact"), dict) or isinstance(payload.get("artifacts"), list):
        return True
    return payload.get("status") in {"published", "already_published"}


class ToolResultProjectionHost(Protocol):
    """What :class:`ToolResultProjector` reads from the agent it serves."""

    config: AgentConfig
    _session_key: str | None
    _message_sizes: MessageSizeCache
    _turn_base_len: int
    _turn_reset: bool

    def _context_budget_governor(self) -> ContextBudgetGovernor: ...

    def _write_turn_call_log(self, kind: str, **payload: Any) -> None: ...

    def _tokenjuice_tool_reduction(
        self,
        *,
        tool_name: str,
        content: str,
        is_error: bool,
        tool_use_id: str,
        arguments: dict[str, Any] | None = None,
        command: str | None = None,
        cwd: str | None = None,
        max_inline_chars: int | None = None,
    ) -> str | None: ...


class ToolResultProjector:
    """Projection and capping of tool results and tool-call arguments.

    Composed into :class:`agentos.engine.agent.Agent` (``agent._projection``);
    it reads the agent's config and turn state through the
    :class:`ToolResultProjectionHost` it is built with.
    """

    def __init__(self, host: ToolResultProjectionHost) -> None:
        self._host = host

    @property
    def config(self) -> AgentConfig:
        return self._host.config

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _context_budget_class(
        budget_class: ToolResultBudgetClass | None,
    ) -> ContextBudgetClass:
        if budget_class is ToolResultBudgetClass.EXTERNAL:
            return ContextBudgetClass.EXTERNAL
        if budget_class is ToolResultBudgetClass.ARTIFACT:
            return ContextBudgetClass.ARTIFACT
        if budget_class is ToolResultBudgetClass.ERROR:
            return ContextBudgetClass.ERROR
        if budget_class is ToolResultBudgetClass.CONTROL:
            return ContextBudgetClass.CONTROL
        return ContextBudgetClass.LOCAL

    @staticmethod
    def _tool_call_string_arg(
        tool_call: ToolCall | None,
        *names: str,
    ) -> str | None:
        if tool_call is None:
            return None
        for name in names:
            value = tool_call.arguments.get(name)
            if isinstance(value, str) and value.strip():
                return value
        return None

    @staticmethod
    def _has_provider_context_argument_marker(arguments: dict[str, Any]) -> bool:
        return arguments.get(_INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY) is True or any(
            arguments.get(marker) is True for marker in _COMPACTED_TOOL_ARGUMENT_MARKERS
        )

    def _tool_result_provider_request_max_chars(
        self,
        budget_class: ToolResultBudgetClass | None = None,
    ) -> int:
        return self._host._context_budget_governor().tool_result_provider_chars_for(
            self._context_budget_class(budget_class)
        )

    def _tool_result_projection_for_provider(
        self,
        content: str,
        *,
        tool_use_id: str,
        tool_name: str,
        reason: str,
        max_preview_chars: int,
    ) -> str:
        max_preview_chars = max(0, int(max_preview_chars))
        if max_preview_chars > 0:
            max_preview_chars = max(1, min(max_preview_chars, 4_000))
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        stored = self._store_tool_result_snapshot(
            content,
            tool_use_id=tool_use_id,
            tool_name=tool_name,
        )
        handle_line = f"tool_result_handle: {stored.handle}\n" if stored is not None else ""
        if max_preview_chars <= 0:
            head = ""
            tail = ""
        elif len(content) <= max_preview_chars:
            head = content
            tail = ""
        else:
            head_chars = max(1, int(max_preview_chars * 0.65))
            tail_chars = max(0, max_preview_chars - head_chars)
            head = content[:head_chars]
            tail = content[-tail_chars:] if tail_chars else ""
        omitted = max(0, len(content) - len(head) - len(tail))
        projection = (
            "[tool_result_projection]\n"
            f"tool: {tool_name}\n"
            f"tool_use_id: {tool_use_id}\n"
            f"original_chars: {len(content)}\n"
            f"sha256: {digest}\n"
            f"{handle_line}"
            f"omitted_chars: {omitted}\n"
            f"reason: {reason}.\n"
            f"head:\n{head}"
        )
        if tail:
            projection += f"\n...\ntail:\n{tail}"
        return projection

    def _store_tool_result_snapshot(
        self,
        content: str,
        *,
        tool_use_id: str,
        tool_name: str,
    ) -> ToolResultRecord | None:
        if not self.config.tool_result_store_dir:
            return None
        session_id = self.config.tool_result_store_session_id or self._host._session_key
        session_key = self.config.tool_result_store_session_key or self._host._session_key
        agent_id = self.config.tool_result_store_agent_id
        if not agent_id and session_key:
            from agentos.session.keys import parse_agent_id

            agent_id = parse_agent_id(session_key)
        if not session_id or not session_key or not agent_id:
            self.config.metadata["tool_result_store_skips"] = (
                self.config.metadata.get("tool_result_store_skips", 0) + 1
            )
            return None
        try:
            record = ToolResultStore(self.config.tool_result_store_dir).write(
                content,
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                session_id=session_id,
                session_key=session_key,
                agent_id=agent_id,
                max_bytes=self.config.tool_result_store_max_bytes,
                disk_budget_bytes=self.config.tool_result_store_disk_budget_bytes,
                retention_seconds=self.config.tool_result_store_retention_seconds,
            )
        except ToolResultStoreBudgetError as exc:
            self.config.metadata["tool_result_store_skips"] = (
                self.config.metadata.get("tool_result_store_skips", 0) + 1
            )
            logger.info(
                "tool_result_store.skipped",
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                reason=str(exc),
            )
            return None
        except Exception as exc:  # pragma: no cover - storage must not break turns
            logger.warning(
                "tool_result_store.write_failed",
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                error=str(exc),
            )
            return None
        self.config.metadata["tool_result_store_writes"] = (
            self.config.metadata.get("tool_result_store_writes", 0) + 1
        )
        return record

    async def _project_tool_result_for_llm(
        self,
        result: ToolResult,
        *,
        tool_call: ToolCall | None = None,
    ) -> ToolResult:
        guarded_content, guarded = _omit_large_json_tool_fields(result.content)
        if guarded:
            result = ToolResult(
                tool_use_id=result.tool_use_id,
                tool_name=result.tool_name,
                content=guarded_content,
                is_error=result.is_error,
                artifacts=list(result.artifacts),
                execution_status=(
                    mark_execution_status_truncated(result.execution_status)
                    if result.execution_status is not None
                    else None
                ),
                terminates_turn=result.terminates_turn,
            )
            self.config.metadata["tool_json_guard_applied"] = True
            self.config.metadata["tool_json_guard_calls"] = (
                self.config.metadata.get("tool_json_guard_calls", 0) + 1
            )

        self.config.metadata["tool_projection_attempts"] = (
            self.config.metadata.get("tool_projection_attempts", 0) + 1
        )
        projected_content = self._host._tokenjuice_tool_reduction(
            tool_name=result.tool_name,
            content=result.content,
            is_error=result.is_error,
            tool_use_id=result.tool_use_id,
            arguments=tool_call.arguments if tool_call is not None else None,
            command=self._tool_call_string_arg(tool_call, "command"),
            cwd=self._tool_call_string_arg(tool_call, "workdir", "cwd"),
        )
        if projected_content is None:
            self.config.metadata["tool_projection_noops"] = (
                self.config.metadata.get("tool_projection_noops", 0) + 1
            )
            self._host._write_turn_call_log(
                "tool_projection_noop",
                tool_use_id=result.tool_use_id,
                name=result.tool_name,
                original_chars=len(result.content),
            )
            return result

        stored = self._store_tool_result_snapshot(
            result.content,
            tool_use_id=result.tool_use_id,
            tool_name=result.tool_name,
        )
        stored_handle = stored.handle if stored is not None else None
        if stored is not None:
            projected_content = (
                "[tool_result_projection]\n"
                f"tool_result_handle: {stored.handle}\n"
                f"sha256: {stored.sha256}\n"
                f"original_chars: {stored.chars}\n"
                f"{projected_content}"
            )

        tokens_before = get_approx_tokens(result.content)
        tokens_after = get_approx_tokens(projected_content)
        self.config.metadata["tool_projection_applied"] = True
        self.config.metadata["tool_projection_calls"] = (
            self.config.metadata.get("tool_projection_calls", 0) + 1
        )
        self.config.metadata["tool_projection_tokens_before"] = (
            self.config.metadata.get("tool_projection_tokens_before", 0) + tokens_before
        )
        self.config.metadata["tool_projection_tokens_after"] = (
            self.config.metadata.get("tool_projection_tokens_after", 0) + tokens_after
        )
        self.config.metadata["tool_projection_tokens_saved"] = self.config.metadata.get(
            "tool_projection_tokens_saved", 0
        ) + max(0, tokens_before - tokens_after)

        self._host._write_turn_call_log(
            "tool_projection_applied",
            tool_use_id=result.tool_use_id,
            name=result.tool_name,
            tool_result_handle=stored_handle,
            original_chars=len(result.content),
            projected_chars=len(projected_content),
        )
        return ToolResult(
            tool_use_id=result.tool_use_id,
            tool_name=result.tool_name,
            content=projected_content,
            is_error=result.is_error,
            artifacts=list(result.artifacts),
            execution_status=result.execution_status,
            terminates_turn=result.terminates_turn,
        )

    async def _canonicalize_tool_result(
        self,
        result: ToolResult,
        *,
        tool_call: ToolCall | None = None,
    ) -> ToolResult:
        return await self._project_tool_result_for_llm(result, tool_call=tool_call)

    async def _project_tool_result_for_delivery(
        self,
        result: ToolResult,
        *,
        tool_call: ToolCall | None = None,
    ) -> ToolResult:
        if _pending_approval_payload(result.content) is not None:
            return result
        return await self._project_tool_result_for_llm(
            result,
            tool_call=tool_call,
        )

    def _shrink_tool_result_for_transcript(
        self,
        block: ContentBlockToolResult,
        tool_name: str,
    ) -> ContentBlockToolResult:
        """Project one oversized tool result to its budget before first send.

        The full payload goes to the on-disk tool-result store (the projection
        names its handle); the transcript keeps the projection forever. Error
        and artifact-producing results are never projected.
        """
        if not isinstance(block.content, str) or block.is_error:
            return block
        content = block.content
        budget_class = resolve_budget_class(tool_name)
        result_cap = self._tool_result_provider_request_max_chars(budget_class)
        if result_cap <= 0 or len(content) <= result_cap:
            return block
        if _tool_result_content_has_artifact(content):
            return block
        if budget_class is ToolResultBudgetClass.CONTROL:
            replacement = compact_tool_result_content(
                tool_name=tool_name,
                content=content,
                max_preview_chars=160,
                budget_class=budget_class,
                is_error=block.is_error,
            )
        else:
            replacement = self._tool_result_projection_for_provider(
                content,
                tool_use_id=block.tool_use_id,
                tool_name=tool_name or "tool",
                reason=(
                    "external tool result compacted for provider request context"
                    if budget_class is ToolResultBudgetClass.EXTERNAL
                    else "tool result compacted for provider request context"
                ),
                max_preview_chars=min(result_cap, 4_000),
            )
        if len(replacement) >= len(content):
            return block
        self.config.metadata["tool_provider_guard_projection_applied"] = True
        self.config.metadata["tool_provider_guard_projection_calls"] = (
            self.config.metadata.get("tool_provider_guard_projection_calls", 0) + 1
        )
        self.config.metadata["tool_projection_applied"] = True
        self.config.metadata["tool_projection_calls"] = (
            self.config.metadata.get("tool_projection_calls", 0) + 1
        )
        return ContentBlockToolResult(
            tool_use_id=block.tool_use_id,
            content=replacement,
            is_error=block.is_error,
            execution_status=(
                mark_execution_status_truncated(block.execution_status)
                if block.execution_status is not None
                else None
            ),
        )

    def _history_tool_payload_cap(self) -> int:
        """Max chars a tool result / argument keeps once its turn is over.

        ``[compaction] history_tool_result_max_chars``; ``0`` (default) means
        2% of the context window in chars, clamped to 4k-12k: a finished
        turn's 120k-char file read is not worth replaying at full size on
        every later request, and its full text stays in the tool-result store.
        """
        configured = self._positive_int(getattr(self.config, "history_tool_result_max_chars", 0))
        if configured is not None:
            return configured
        window_chars = int(self.config.context_window_tokens) * 4
        return max(4_000, min(12_000, int(window_chars * 0.02)))

    def _cap_turn_for_history(self, messages: list[Message]) -> None:
        """Bound this turn's tool payloads once, at the turn boundary.

        During the turn every result was sent at its per-class budget. Before
        the turn is persisted, results and tool-call argument fields over the
        history cap become head/tail projections that name their tool-result
        store handle. Earlier turns were capped when they ended and are never
        touched again, so the next request re-sends this turn's tail once (one
        cache write) and every later request repeats it byte for byte.
        """
        start = 2 if self._host._turn_reset else self._host._turn_base_len
        if start >= len(messages):
            return
        cap = self._history_tool_payload_cap()
        tool_names: dict[str, str] = {}
        for index in range(start, len(messages)):
            message = messages[index]
            if not isinstance(message.content, list):
                continue
            blocks: list[Any] = []
            changed = False
            for block in message.content:
                replacement = block
                if isinstance(block, ContentBlockToolUse):
                    tool_names[block.id] = block.name
                    replacement = self._cap_tool_use_arguments(block, cap)
                elif isinstance(block, ContentBlockToolResult):
                    replacement = self._cap_tool_result(
                        block, cap, tool_names.get(block.tool_use_id, "tool")
                    )
                changed = changed or replacement is not block
                blocks.append(replacement)
            if changed:
                messages[index] = Message(
                    role=message.role,
                    content=blocks,
                    reasoning_content=message.reasoning_content,
                    reasoning_details=message.reasoning_details,
                    provenance=message.provenance,
                )
                self._host._message_sizes.retain_only(messages)

    def _cap_tool_result(
        self,
        block: ContentBlockToolResult,
        cap: int,
        tool_name: str,
    ) -> ContentBlockToolResult:
        content = block.content
        if not isinstance(content, str) or len(content) <= cap:
            return block
        if _tool_result_content_has_artifact(content):
            return block
        projection = self._tool_result_projection_for_provider(
            content,
            tool_use_id=block.tool_use_id,
            tool_name=tool_name,
            reason="tool result from an earlier turn, shortened for history",
            max_preview_chars=min(cap, 4_000),
        )
        if len(projection) >= len(content):
            return block
        return ContentBlockToolResult(
            tool_use_id=block.tool_use_id,
            content=projection,
            is_error=block.is_error,
            execution_status=(
                mark_execution_status_truncated(block.execution_status)
                if block.execution_status is not None
                else None
            ),
        )

    def _cap_tool_use_arguments(self, block: ContentBlockToolUse, cap: int) -> ContentBlockToolUse:
        oversized = [
            key for key, value in block.input.items() if isinstance(value, str) and len(value) > cap
        ]
        if not oversized:
            return block
        arguments = dict(block.input)
        for key in oversized:
            value = str(block.input[key])
            stored = self._store_tool_result_snapshot(
                value,
                tool_use_id=f"{block.id}:{key}",
                tool_name=block.name,
            )
            arguments[key] = history_argument_projection(
                tool_name=block.name,
                tool_use_id=block.id,
                field_name=key,
                value=value,
                handle=stored.handle if stored is not None else None,
            )
        return ContentBlockToolUse(
            id=block.id,
            name=block.name,
            input=arguments,
            thought_signature=block.thought_signature,
        )

    @staticmethod
    def _is_provider_context_projection_reuse_result(result: ToolResult) -> bool:
        status: Mapping[str, Any] = result.execution_status or {}
        return bool(
            result.is_error
            and isinstance(status, dict)
            and status.get("reason") == _PROVIDER_CONTEXT_PROJECTION_REUSED_REASON
        )

    @staticmethod
    def _parse_tool_argument_projection(value: str) -> dict[str, str] | None:
        if not value.startswith(_TOOL_ARGUMENT_PROJECTION_PREFIX):
            return None
        metadata: dict[str, str] = {}
        for line in value.splitlines()[1:]:
            if line in {"head:", "tail:"}:
                break
            key, separator, raw_value = line.partition(":")
            if not separator:
                continue
            metadata[key.strip()] = raw_value.strip()
        return metadata

    @staticmethod
    def _provider_projection_placeholder(tool_name: str, field: str) -> str:
        return (
            f"[invalid_provider_context_projection:{tool_name}.{field}] "
            "provider-only compacted tool argument omitted; regenerate the real "
            "argument instead of copying provider context."
        )

    @staticmethod
    def _provider_compacted_arguments_placeholder(
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            _INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY: True,
            "tool": tool_name,
            "reason": "provider_context_omitted",
        }

    def _sanitize_projected_tool_call_arguments(self, tc: ToolCall) -> ToolCall:
        if self._has_provider_context_argument_marker(tc.arguments):
            return ToolCall(
                tool_use_id=tc.tool_use_id,
                tool_name=tc.tool_name,
                arguments=self._provider_compacted_arguments_placeholder(
                    tc.tool_name,
                    tc.arguments,
                ),
                synthetic_from_text=tc.synthetic_from_text,
                origin_trace=tc.origin_trace,
            )
        sanitized = dict(tc.arguments)
        changed = False
        for argument_name, value in tc.arguments.items():
            if not isinstance(value, str) or not value.startswith(
                (
                    _TOOL_ARGUMENT_PROJECTION_PREFIX,
                    _HISTORICAL_TOOL_ARGUMENT_PROJECTION_PREFIX,
                    _INVALID_PROVIDER_CONTEXT_PROJECTION_PREFIX,
                )
            ):
                continue
            sanitized[argument_name] = self._provider_projection_placeholder(
                tc.tool_name,
                argument_name,
            )
            changed = True
        if not changed:
            return tc
        return ToolCall(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            arguments=sanitized,
            synthetic_from_text=tc.synthetic_from_text,
            origin_trace=tc.origin_trace,
        )

    def _projection_rehydrate_error(
        self,
        tc: ToolCall,
        *,
        field: str,
        reason: str,
    ) -> ToolResult:
        self.config.metadata["tool_argument_projection_rehydrate_failures"] = (
            self.config.metadata.get(
                "tool_argument_projection_rehydrate_failures",
                0,
            )
            + 1
        )
        self._host._write_turn_call_log(
            "tool_argument_projection_rehydrate_failed",
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            field=field,
            reason=reason,
        )
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content=(
                f"The {tc.tool_name}.{field} input was not available in executable "
                "form. The tool was not run; regenerate the complete argument and "
                "retry the tool call."
            ),
            is_error=True,
            execution_status=runtime_execution_status(
                "error",
                reason=_PROVIDER_CONTEXT_PROJECTION_REUSED_REASON,
            ),
        )

    def _provider_compacted_arguments_error(
        self,
        tc: ToolCall,
        *,
        reason: str,
    ) -> ToolResult:
        self.config.metadata["tool_argument_projection_rehydrate_failures"] = (
            self.config.metadata.get(
                "tool_argument_projection_rehydrate_failures",
                0,
            )
            + 1
        )
        self._host._write_turn_call_log(
            "tool_argument_projection_rehydrate_failed",
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            reason=reason,
        )
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content=(
                f"The {tc.tool_name} arguments were not available in executable "
                "form. The tool was not run; regenerate the complete arguments and "
                "retry the tool call."
            ),
            is_error=True,
            execution_status=runtime_execution_status(
                "error",
                reason=_PROVIDER_CONTEXT_PROJECTION_REUSED_REASON,
            ),
        )

    def _rehydrate_projected_tool_arguments(
        self,
        tc: ToolCall,
    ) -> ToolCall | ToolResult:
        if self._has_provider_context_argument_marker(tc.arguments):
            return self._provider_compacted_arguments_error(
                tc,
                reason="provider_compacted_arguments_reused",
            )
        for argument_name, value in tc.arguments.items():
            if not isinstance(value, str) or not value.startswith(
                (
                    _TOOL_ARGUMENT_PROJECTION_PREFIX,
                    _HISTORICAL_TOOL_ARGUMENT_PROJECTION_PREFIX,
                    _INVALID_PROVIDER_CONTEXT_PROJECTION_PREFIX,
                )
            ):
                continue
            return self._projection_rehydrate_error(
                tc,
                field=argument_name,
                reason="provider_projection_reused",
            )
        return tc
