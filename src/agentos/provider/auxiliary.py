"""One path for the LLM calls AgentOS makes on its own behalf.

Not every provider call belongs to a turn. Analysing a PDF the user attached,
describing an image, and — as the harness grows — summarising a transcript or
reviewing what a turn produced are all work AgentOS initiates itself. They are
not the agent's conversation and must not be charged to its prompt cache.

Before this module each of those calls carried its own copy of the plumbing:
which model to use, where the key lives, what a failure means. The copies drift
— `tools/builtin/media.py` had two provider-to-credential mappings side by side,
one that consulted the configured `[llm]` section and one that only read the
environment — and none of them recorded what they spent, so side-task tokens
were invisible in `agentos cost` while still appearing on the bill.

What this owns:

* **Model resolution**, in one documented order, so a task can be pointed at a
  cheaper model without touching the code that calls it.
* **Credentials**, resolved once from the configured provider and then the
  environment, rather than per call site.
* **Accounting.** Every call is recorded under the scope ``aux:<task>``, so its
  cost lands in the session total *and* stays separable from turn cost.
* **Failure shape.** Errors arrive classified through the same taxonomy the
  turn loop uses, so a caller decides to degrade or raise on real information.

What this deliberately does not do: re-enter ``TurnRunner``, touch the session
transcript, or reuse message history carrying cache breakpoints. A side task
that broke the main prompt cache would cost more than it saves.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import structlog

from agentos.provider.failures import ProviderFailureKind, classify_provider_error
from agentos.provider.selector import ProviderConfig, build_provider
from agentos.provider.types import ChatConfig, Message

log = structlog.get_logger(__name__)

# Small, cheap, widely available. Only used when nothing else is configured.
DEFAULT_AUXILIARY_MODEL = "openai/gpt-4o-mini"
DEFAULT_AUXILIARY_TIMEOUT_SECONDS = 120.0

_OPENROUTER_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


@dataclass(frozen=True)
class AuxResult:
    """What an auxiliary call produced, and what it cost."""

    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class AuxiliaryError(RuntimeError):
    """An auxiliary call failed.

    ``kind`` carries the same classification the turn loop uses, so a caller
    can tell "no credentials configured" from "the provider is rate limiting"
    without parsing a message.
    """

    def __init__(self, message: str, *, task: str, kind: ProviderFailureKind | None = None) -> None:
        super().__init__(message)
        self.task = task
        self.kind = kind


@dataclass
class _Resolution:
    provider: str
    model: str
    source: str


def _config_value(config: Any | None, key: str, default: Any = "") -> Any:
    if config is None:
        return default
    if isinstance(config, Mapping):
        return config.get(key, default)
    return getattr(config, key, default)


def _env_scope(task: str) -> tuple[str, str]:
    """Read the ``AGENTOS_<TASK>_*`` override pair for a task.

    These predate this module — `AGENTOS_VISION_MODEL` and friends are how
    operators pin a side-task model today — so they stay the highest-priority
    source rather than being quietly demoted below the new config section.

    ``LLM`` is excluded: ``AGENTOS_LLM_MODEL`` is the general model setting the
    turn loop reads too, not a side-task override. Treating it as one would let
    it outrank ``[auxiliary].model`` and defeat the whole point of the section,
    so it is consulted further down instead.
    """

    scope = task.strip().upper()
    if not scope or scope == "LLM":
        return "", ""
    return (
        str(os.environ.get(f"AGENTOS_{scope}_PROVIDER", "") or "").strip(),
        str(os.environ.get(f"AGENTOS_{scope}_MODEL", "") or "").strip(),
    )


@dataclass
class AuxiliaryClient:
    """Runs side-task LLM calls against a resolved, accounted provider."""

    config: Any | None = None
    llm_config: Any | None = None
    usage_tracker: Any | None = None
    provider_factory: Any = field(default=build_provider)

    # -- resolution ---------------------------------------------------------

    def _task_config(self, task: str) -> Any | None:
        tasks = _config_value(self.config, "tasks", None)
        if isinstance(tasks, Mapping):
            return tasks.get(task)
        return _config_value(tasks, task, None)

    def _resolve_target(
        self,
        task: str,
        *,
        preferred_provider: str = "",
        preferred_model: str = "",
        default_model: str = "",
    ) -> _Resolution:
        """Pick the provider and model for *task*.

        Order, highest first:

        1. ``AGENTOS_<TASK>_PROVIDER`` / ``AGENTOS_<TASK>_MODEL``
        2. ``[auxiliary.tasks.<task>]``
        3. the caller's hint — a capability-aware choice such as the router's
           image-capable tier, which has to outrank a generic default because a
           text model cannot do the job
        4. ``[auxiliary]``
        5. ``AGENTOS_LLM_PROVIDER`` / ``AGENTOS_LLM_MODEL``
        6. the configured ``[llm]`` section
        7. the caller's ``default_model``, then :data:`DEFAULT_AUXILIARY_MODEL`
        """

        task_cfg = self._task_config(task)
        env_provider, env_model = _env_scope(task)

        provider_candidates = (
            (env_provider, "env_scope"),
            (str(_config_value(task_cfg, "provider", "") or "").strip(), "auxiliary_task"),
            (str(preferred_provider or "").strip(), "caller_hint"),
            (str(_config_value(self.config, "provider", "") or "").strip(), "auxiliary"),
            (str(os.environ.get("AGENTOS_LLM_PROVIDER", "") or "").strip(), "env_llm"),
            (str(_config_value(self.llm_config, "provider", "") or "").strip(), "llm_config"),
        )
        model_candidates = (
            (env_model, "env_scope"),
            (str(_config_value(task_cfg, "model", "") or "").strip(), "auxiliary_task"),
            (str(preferred_model or "").strip(), "caller_hint"),
            (str(_config_value(self.config, "model", "") or "").strip(), "auxiliary"),
            (str(os.environ.get("AGENTOS_LLM_MODEL", "") or "").strip(), "env_llm"),
            (str(_config_value(self.llm_config, "model", "") or "").strip(), "llm_config"),
            (str(default_model or "").strip(), "caller_default"),
        )

        provider = next((value for value, _ in provider_candidates if value), "openrouter")
        model = next((value for value, _ in model_candidates if value), DEFAULT_AUXILIARY_MODEL)
        source = next((name for value, name in model_candidates if value), "builtin_default")
        return _Resolution(provider=provider.lower(), model=model, source=source)

    def _credentials(self, provider: str) -> tuple[str, str, str, dict[str, str]]:
        """Find the key, base URL, proxy and routing for *provider*.

        The configured ``[llm]`` section wins when it describes this same
        provider — an operator who set a key there meant it — and the
        environment fills whatever is left.
        """

        configured = str(_config_value(self.llm_config, "provider", "") or "").strip().lower()
        use_configured = bool(configured) and configured == provider

        api_key = str(_config_value(self.llm_config, "api_key", "") or "") if use_configured else ""
        if use_configured and not api_key:
            api_key_env = str(_config_value(self.llm_config, "api_key_env", "") or "")
            if api_key_env:
                api_key = os.environ.get(api_key_env, "")
        base_url = (
            str(_config_value(self.llm_config, "base_url", "") or "") if use_configured else ""
        )
        proxy = str(_config_value(self.llm_config, "proxy", "") or "") if use_configured else ""
        routing = _config_value(self.llm_config, "provider_routing", {}) if use_configured else {}
        if not isinstance(routing, dict):
            routing = {}

        if provider == "anthropic":
            api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
            base_url = base_url or os.environ.get("ANTHROPIC_BASE_URL", "")
        elif provider == "openrouter":
            api_key = (
                api_key
                or os.environ.get("OPENROUTER_API_KEY", "")
                or os.environ.get("OPENAI_API_KEY", "")
            )
            base_url = base_url or os.environ.get(
                "OPENROUTER_BASE_URL", _OPENROUTER_DEFAULT_BASE_URL
            )
        else:
            api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
            base_url = base_url or os.environ.get("OPENAI_BASE_URL", "")

        return api_key, base_url, proxy or os.environ.get("AGENTOS_LLM_PROXY", ""), routing

    def provider_config(
        self,
        task: str,
        *,
        preferred_provider: str = "",
        preferred_model: str = "",
        default_model: str = "",
    ) -> ProviderConfig:
        """Resolve *task* to a fully populated provider config."""

        target = self._resolve_target(
            task,
            preferred_provider=preferred_provider,
            preferred_model=preferred_model,
            default_model=default_model,
        )
        api_key, base_url, proxy, routing = self._credentials(target.provider)
        return ProviderConfig(
            provider=target.provider,
            model=target.model,
            api_key=api_key,
            base_url=base_url,
            proxy=proxy,
            provider_routing=routing,
        )

    # -- execution ----------------------------------------------------------

    def _timeout(self, override: float | None) -> float:
        if override is not None and override > 0:
            return float(override)
        configured = _config_value(self.config, "timeout_seconds", 0.0)
        try:
            value = float(configured or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        return value if value > 0 else DEFAULT_AUXILIARY_TIMEOUT_SECONDS

    def _require_credentials(self, task: str, cfg: ProviderConfig) -> None:
        """Fail early, and legibly, when no key is reachable.

        Otherwise the request goes out with an empty bearer token and the
        provider answers with a transport complaint about a malformed header
        — ``Illegal header value b'Bearer '`` — which tells the operator
        nothing about what to fix. Local backends are exempt: they
        authenticate by reachability, not by key.

        Only the real builder is guarded. A caller that injected its own
        factory has taken over how the client is authenticated, and holding it
        to this rule would reject doubles that need no credentials at all.
        """

        if cfg.api_key or self.provider_factory is not build_provider:
            return
        try:
            from agentos.provider.registry import get_provider_spec

            spec = get_provider_spec(cfg.provider)
        except Exception:  # noqa: BLE001 — an unknown provider fails later, with its own message
            return
        if "api_key" not in spec.required_fields:
            return
        hint = f" Set {spec.env_key}." if spec.env_key else ""
        raise AuxiliaryError(
            f"auxiliary task {task!r} has no API key for provider {cfg.provider!r}.{hint}",
            task=task,
            kind=ProviderFailureKind.AUTH_INVALID,
        )

    def _record(self, task: str, session_key: str | None, done: Any, model: str) -> None:
        tracker = self.usage_tracker
        if tracker is None or not session_key:
            return
        from agentos.engine.usage import usage_scope

        try:
            with usage_scope(f"aux:{task}"):
                tracker.add(
                    session_key,
                    int(getattr(done, "input_tokens", 0) or 0),
                    int(getattr(done, "output_tokens", 0) or 0),
                    model_id=str(getattr(done, "model", "") or model),
                    cache_read_tokens=int(getattr(done, "cached_tokens", 0) or 0),
                    cache_write_tokens=int(getattr(done, "cache_write_tokens", 0) or 0),
                    billed_cost=float(getattr(done, "billed_cost", 0.0) or 0.0),
                )
        except Exception as exc:  # noqa: BLE001 — accounting must not fail the call
            log.warning("auxiliary.usage_record_failed", task=task, error=str(exc))

    async def complete(
        self,
        *,
        task: str,
        messages: list[Message],
        preferred_provider: str = "",
        preferred_model: str = "",
        default_model: str = "",
        chat_config: ChatConfig | None = None,
        timeout: float | None = None,
        session_key: str | None = None,
    ) -> AuxResult:
        """Run one side-task completion and return the assembled text.

        Raises:
            AuxiliaryError: the provider could not be built, the stream failed,
                or the call exceeded its timeout.
        """

        cfg = self.provider_config(
            task,
            preferred_provider=preferred_provider,
            preferred_model=preferred_model,
            default_model=default_model,
        )
        self._require_credentials(task, cfg)
        try:
            provider = self.provider_factory(
                provider=cfg.provider,
                model=cfg.model,
                api_key=cfg.api_key,
                base_url=cfg.base_url,
            )
        except Exception as exc:
            raise AuxiliaryError(
                f"auxiliary task {task!r} could not build provider {cfg.provider!r}: {exc}",
                task=task,
                kind=ProviderFailureKind.AUTH_INVALID,
            ) from exc

        budget = self._timeout(timeout)
        try:
            result = await asyncio.wait_for(
                self._drain(task, provider, messages, chat_config, cfg, session_key),
                timeout=budget,
            )
        except TimeoutError as exc:
            raise AuxiliaryError(
                f"auxiliary task {task!r} timed out after {budget:g}s",
                task=task,
                # A timeout is transient by nature; the taxonomy already routes
                # TRANSPORT_TRANSIENT to a retry rather than a config failure.
                kind=ProviderFailureKind.TRANSPORT_TRANSIENT,
            ) from exc

        log.debug(
            "auxiliary.completed",
            task=task,
            provider=cfg.provider,
            model=cfg.model,
            output_tokens=result.output_tokens,
        )
        return result

    async def _drain(
        self,
        task: str,
        provider: Any,
        messages: list[Message],
        chat_config: ChatConfig | None,
        cfg: ProviderConfig,
        session_key: str | None,
    ) -> AuxResult:
        parts: list[str] = []
        input_tokens = 0
        output_tokens = 0

        async for event in provider.chat(messages=messages, config=chat_config):
            kind = getattr(event, "kind", "")
            if kind == "error":
                code = str(getattr(event, "code", "") or "provider_error")
                message = str(getattr(event, "message", "") or "provider stream failed")
                raise AuxiliaryError(
                    f"auxiliary task {task!r} failed ({code}): {message}",
                    task=task,
                    kind=classify_provider_error(cfg.provider, None, code, message),
                )
            if kind == "done":
                input_tokens = int(getattr(event, "input_tokens", 0) or 0)
                output_tokens = int(getattr(event, "output_tokens", 0) or 0)
                self._record(task, session_key, event, cfg.model)
                continue
            if kind == "thinking_delta":
                # Reasoning is never part of an auxiliary task's answer.
                continue
            text = getattr(event, "text", None)
            if isinstance(text, str):
                parts.append(text)
                continue
            delta = getattr(event, "delta", None)
            if isinstance(delta, str):
                parts.append(delta)

        return AuxResult(
            text="".join(parts),
            provider=cfg.provider,
            model=cfg.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


_client = AuxiliaryClient()


def configure_auxiliary(
    config: Any | None,
    *,
    llm_config: Any | None = None,
    usage_tracker: Any | None = None,
) -> None:
    """Point the shared client at the live configuration.

    Called from gateway boot and again on every config commit, mirroring how
    the media, search and image-generation surfaces are wired.
    """

    global _client
    _client = AuxiliaryClient(
        config=config,
        llm_config=llm_config,
        usage_tracker=usage_tracker if usage_tracker is not None else _client.usage_tracker,
    )


def get_auxiliary_client() -> AuxiliaryClient:
    """Return the shared client. Usable before configuration — it falls back
    to the environment, which is what an unconfigured install has anyway."""

    return _client
