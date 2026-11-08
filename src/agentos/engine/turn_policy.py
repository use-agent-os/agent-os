"""Pure turn policy resolution helpers."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any, TypeGuard

from agentos.engine.types import AgentConfig


@dataclass(frozen=True)
class TurnPolicy:
    max_iterations: int
    max_iterations_source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_turn_policy(
    *,
    session_key: str,
    explicit_max_iterations: int | None = None,
    session_manager: Any = None,
    gateway_config: Any = None,
    env: Mapping[str, str] | None = None,
    agent_default: int | None = None,
) -> TurnPolicy:
    """Resolve loop limits with the existing precedence order."""

    if explicit_max_iterations is not None:
        if _non_bool_int(explicit_max_iterations) and explicit_max_iterations >= 0:
            return TurnPolicy(int(explicit_max_iterations), "explicit argument")
        raise ValueError("max_iterations must be an integer >= 0")

    session_value = _session_agent_max_iterations(session_manager, session_key)
    if _non_bool_int(session_value) and session_value >= 0:
        return TurnPolicy(int(session_value), "session config")

    raw_env = (env or os.environ).get("AGENTOS_AGENT_MAX_ITERATIONS")
    if raw_env is not None and raw_env.strip():
        try:
            env_value = int(raw_env.strip())
        except ValueError:
            env_value = None
        if env_value is not None and env_value >= 0:
            return TurnPolicy(env_value, "env AGENTOS_AGENT_MAX_ITERATIONS")

    config_value = getattr(gateway_config, "agent_max_iterations", None)
    if isinstance(config_value, int) and not isinstance(config_value, bool):
        config_iterations = int(config_value)
        if config_iterations >= 0:
            return TurnPolicy(config_iterations, "gateway config")

    return TurnPolicy(
        AgentConfig().max_iterations if agent_default is None else int(agent_default),
        "AgentConfig default",
    )


def _session_agent_max_iterations(session_manager: Any, session_key: str) -> Any:
    getter = getattr(session_manager, "get_session_config", None)
    if not callable(getter):
        return None
    try:
        session_cfg = getter(session_key)
    except Exception:  # noqa: BLE001 - resolver fallback mirrors runtime behavior
        return None
    return getattr(session_cfg, "agent_max_iterations", None) if session_cfg is not None else None


def _non_bool_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int)


# ---------------------------------------------------------------------------
# Per-turn runtime budgets (#3819)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Knob:
    """One per-turn budget and where it is read from."""

    #: ``turn_runner.invalid_<name>`` is the warning for an unusable value.
    name: str
    session_attrs: tuple[str, ...]
    env_var: str
    config_attrs: tuple[str, ...]
    integer: bool = False
    #: Zero is not allowed (a request timeout of 0 makes no sense).
    positive: bool = False
    #: Message of the ``ValueError`` for an unusable explicit argument.
    explicit_error: str = ""


_ITERATION_TIMEOUT = _Knob(
    name="agent_iteration_timeout",
    session_attrs=("agent_iteration_timeout_seconds",),
    env_var="AGENTOS_AGENT_ITERATION_TIMEOUT",
    config_attrs=("agent_iteration_timeout_seconds",),
    explicit_error="iteration_timeout must be a non-negative number",
)
_TOOL_TIMEOUT = _Knob(
    name="agent_tool_timeout",
    session_attrs=("agent_tool_timeout_seconds",),
    env_var="AGENTOS_AGENT_TOOL_TIMEOUT",
    config_attrs=("agent_tool_timeout_seconds",),
    explicit_error="tool_timeout must be a non-negative number",
)
_REQUEST_TIMEOUT = _Knob(
    name="agent_request_timeout",
    session_attrs=("agent_request_timeout_seconds",),
    env_var="AGENTOS_AGENT_REQUEST_TIMEOUT",
    config_attrs=("agent_request_timeout_seconds",),
    positive=True,
    explicit_error="request_timeout must be a positive number",
)
_MAX_PROVIDER_RETRIES = _Knob(
    name="agent_max_provider_retries",
    session_attrs=("agent_max_provider_retries",),
    env_var="AGENTOS_AGENT_MAX_PROVIDER_RETRIES",
    config_attrs=("agent_max_provider_retries",),
    integer=True,
    explicit_error="max_provider_retries must be an integer >= 0",
)
_RUNTIME_TIMEOUT_ATTRS = ("agent_runtime_timeout_seconds", "llm_timeout_seconds")


class TurnPolicyResolver:
    """Resolve a turn's runtime budgets from one precedence chain.

    Every budget is read the same way: an explicit argument, then the
    session config, then the environment, then the gateway config, then the
    default. An unusable value at any level is logged as
    ``turn_runner.invalid_<budget>`` with its ``source`` and skipped. An
    unusable explicit argument raises ``ValueError``. The whole-turn runtime
    timeout keeps its legacy rules: no explicit argument, the
    ``llm_timeout_seconds`` alias, and its own warning names.
    """

    def __init__(
        self,
        *,
        session_manager: Any,
        config: Any,
        log: Any,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._config = config
        self._log = log
        self._env = os.environ if env is None else env

    # -- budgets ----------------------------------------------------------

    def runtime_timeout(self, session_key: str, *, default: float) -> float:
        """Whole-turn runtime timeout; ``0`` disables the runtime budget."""
        session_cfg = self._session_config(session_key)
        for attr in _RUNTIME_TIMEOUT_ATTRS:
            value = getattr(session_cfg, attr, None)
            if _usable(value, integer=False, positive=False):
                return float(value)
        raw = self._env_raw("AGENTOS_TURN_TIMEOUT")
        if raw is not None:
            try:
                parsed = float(raw)
            except ValueError:
                self._log.warning("turn_runner.invalid_runtime_timeout", raw=raw)
            else:
                if parsed >= 0:
                    return parsed
                self._log.warning("turn_runner.negative_runtime_timeout", value=parsed)
        for attr in _RUNTIME_TIMEOUT_ATTRS:
            value = getattr(self._config, attr, None)
            if _usable(value, integer=False, positive=False):
                return float(value)
        return default

    def iteration_timeout(self, session_key: str, explicit: float | None = None) -> float:
        return float(
            self._resolve(
                _ITERATION_TIMEOUT, session_key, explicit, lambda: AgentConfig().iteration_timeout
            )
        )

    def tool_timeout(self, session_key: str, explicit: float | None = None) -> float:
        return float(
            self._resolve(_TOOL_TIMEOUT, session_key, explicit, lambda: AgentConfig().tool_timeout)
        )

    def request_timeout(
        self, session_key: str, explicit: float | None, *, default: Callable[[], float]
    ) -> float:
        """Single LLM request timeout; ``default`` is only called when nothing is set."""
        return float(self._resolve(_REQUEST_TIMEOUT, session_key, explicit, default))

    def max_provider_retries(self, session_key: str, explicit: int | None = None) -> int:
        return int(
            self._resolve(
                _MAX_PROVIDER_RETRIES,
                session_key,
                explicit,
                lambda: AgentConfig().max_provider_retries,
            )
        )

    def max_iterations(self, session_key: str, explicit: int | None = None) -> TurnPolicy:
        """The loop budget and where it came from (see :func:`resolve_turn_policy`)."""
        if explicit is not None:
            return resolve_turn_policy(session_key=session_key, explicit_max_iterations=explicit)
        event = "turn_runner.invalid_agent_max_iterations"
        session_value = getattr(self._session_config(session_key), "agent_max_iterations", None)
        if session_value is not None and not _usable(session_value, integer=True, positive=False):
            self._log.warning(event, source="session", value=session_value)
        raw = self._env_raw("AGENTOS_AGENT_MAX_ITERATIONS")
        if raw is not None:
            try:
                parsed = int(raw)
            except ValueError:
                self._log.warning(event, source="env", raw=raw)
            else:
                if parsed < 0:
                    self._log.warning(event, source="env", value=parsed)
        config_value = getattr(self._config, "agent_max_iterations", None)
        if config_value is not None and not _usable(config_value, integer=True, positive=False):
            self._log.warning(event, source="config", value=config_value)
        return resolve_turn_policy(
            session_key=session_key,
            explicit_max_iterations=None,
            session_manager=self._session_manager,
            gateway_config=self._config,
            env=self._env,
        )

    # -- the shared chain -------------------------------------------------

    def _resolve(
        self, knob: _Knob, session_key: str, explicit: Any, default: Callable[[], Any]
    ) -> Any:
        convert = int if knob.integer else float
        if explicit is not None:
            if _usable(explicit, integer=knob.integer, positive=knob.positive):
                return convert(explicit)
            raise ValueError(knob.explicit_error)
        event = f"turn_runner.invalid_{knob.name}"
        session_cfg = self._session_config(session_key)
        for attr in knob.session_attrs:
            value = getattr(session_cfg, attr, None)
            if _usable(value, integer=knob.integer, positive=knob.positive):
                return convert(value)
            if value is not None:
                self._log.warning(event, source="session", value=value)
        raw = self._env_raw(knob.env_var)
        if raw is not None:
            try:
                parsed = convert(raw)
            except ValueError:
                self._log.warning(event, source="env", raw=raw)
            else:
                if parsed > 0 or (parsed == 0 and not knob.positive):
                    return parsed
                self._log.warning(event, source="env", value=parsed)
        for attr in knob.config_attrs:
            value = getattr(self._config, attr, None)
            if _usable(value, integer=knob.integer, positive=knob.positive):
                return convert(value)
            if value is not None:
                self._log.warning(event, source="config", value=value)
        return default()

    def _session_config(self, session_key: str) -> Any:
        """The session's config, or ``None`` (a failing lookup is ignored)."""
        manager = self._session_manager
        if manager is None or not hasattr(manager, "get_session_config"):
            return None
        try:
            return manager.get_session_config(session_key)
        except Exception:  # noqa: BLE001 - a broken session config never fails a turn
            return None

    def _env_raw(self, name: str) -> str | None:
        value = self._env.get(name)
        if value is None or not value.strip():
            return None
        return value.strip()


def _usable(value: Any, *, integer: bool, positive: bool) -> TypeGuard[int | float]:
    if isinstance(value, bool):
        return False
    if integer:
        if not isinstance(value, int):
            return False
    elif not isinstance(value, int | float):
        return False
    return bool(value > 0) if positive else bool(value >= 0)
