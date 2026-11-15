"""The operator-approved values of the operator-only config settings.

An edit of the config file that touches an operator-only setting
(``agentos.config_roots``: the trading limits, hooks, the tool policy,
``gateway_control``, ...) is *held* by the running gateway: on a
host without the OS sandbox the file is a same-user write away for an agent's
shell, so the watcher never makes such an edit live. A restart used to read
the whole file as the operator's, which turned "held" into "applied at the
next restart" -- and a restart is something an agent can wait for, or cause.

This module closes that: it keeps, next to the operator secret's state root,
the values of every operator-only setting as the operator last approved them
(``config-approval.toml``, 0600, on the agent's sensitive-path floor). At boot,
a setting the file holds differently from the approval is not applied: the
gateway runs on the approved value, logs the setting as held, and keeps
holding it -- across any number of restarts -- until the operator applies the
file with ``agentos config reload`` (an RPC the gateway refuses to every agent
binding, or, while no gateway runs, a direct approval from the operator's
terminal).

The approval moves forward only through a path that is the operator's:

* a config RPC write (``commit_config``), which already refuses an agent the
  operator-only settings;
* the operator's ``config.reload`` (``reload_config_from_disk`` with
  ``include_operator_only``);
* a config write from the AgentOS CLI or onboarding while no gateway owns the
  state directory (so no agent shell runs) and outside an agent's session --
  and then only for the settings that write changed, and only where the file
  held no unapproved edit of that setting already: rewriting a file never
  launders an edit somebody else left in it.

The first boot with no approval file adopts the file as it is (there is
nothing to compare against). Like the rest of the floor this is not an OS
boundary: a same-user process that can write this file directly is not held
back by it. It is what stops a held edit from being applied by a restart.
"""

from __future__ import annotations

import copy
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from agentos.config_roots import OPERATOR_ONLY_CONFIG_ROOTS

log = structlog.get_logger(__name__)

APPROVAL_FILENAME = "config-approval.toml"
_VERSION = 1
_ABSENT: Any = object()
_ENFORCED_ATTR = "_operator_config_approval_enforced"


class ConfigApprovalError(ValueError):
    """The approval file exists but cannot be read; nothing is adopted silently."""


@dataclass(frozen=True)
class ConfigApproval:
    """The approved raw (TOML) value of each operator-only root the file set."""

    sections: dict[str, Any]
    config_path: str | None = None


def approval_path() -> Path:
    """Where the approval lives: the AgentOS home, beside ``wallets/``."""
    from agentos.paths import default_agentos_home

    return default_agentos_home() / APPROVAL_FILENAME


# ── raw payload helpers ─────────────────────────────────────────────────────


def _get_raw(payload: dict[str, Any], root: str) -> Any:
    node: Any = payload
    for part in root.split("."):
        if not isinstance(node, dict) or part not in node:
            return _ABSENT
        node = node[part]
    return node


def _set_raw(payload: dict[str, Any], root: str, value: Any) -> None:
    parts = root.split(".")
    node = payload
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            if value is _ABSENT:
                return
            child = {}
            node[part] = child
        node = child
    if value is _ABSENT:
        node.pop(parts[-1], None)
    else:
        node[parts[-1]] = copy.deepcopy(value)


def raw_sections(payload: dict[str, Any]) -> dict[str, Any]:
    """The raw value of every operator-only root present in *payload*."""
    sections: dict[str, Any] = {}
    for root in OPERATOR_ONLY_CONFIG_ROOTS:
        value = _get_raw(payload, root)
        if value is not _ABSENT:
            sections[root] = copy.deepcopy(value)
    return sections


def _with_sections(payload: dict[str, Any], sections: dict[str, Any]) -> dict[str, Any]:
    """*payload* with every operator-only root replaced by its approved value."""
    composite = copy.deepcopy(payload)
    for root in OPERATOR_ONLY_CONFIG_ROOTS:
        _set_raw(composite, root, sections.get(root, _ABSENT))
    return composite


def _migrated(payload: dict[str, Any]) -> dict[str, Any]:
    from agentos.gateway.config_migration import migrate_config_payload

    return migrate_config_payload(payload).payload


def read_file_payload(path: str | Path | None) -> dict[str, Any]:
    """The migrated TOML payload of *path*; ``{}`` when there is no file."""
    import tomllib

    if not path:
        return {}
    target = Path(path).expanduser()
    if not target.is_file():
        return {}
    with open(target, "rb") as fh:
        data = tomllib.load(fh)
    return _migrated(data if isinstance(data, dict) else {})


# ── semantics ───────────────────────────────────────────────────────────────


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _dump(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dump(v) for v in value]
    return value


def _get_attr(target: Any, root: str) -> Any:
    node = target
    for part in root.split("."):
        node = getattr(node, part)
    return node


def _set_attr(target: Any, root: str, value: Any) -> None:
    parts = root.split(".")
    node = target
    for part in parts[:-1]:
        node = getattr(node, part)
    setattr(node, parts[-1], value)


def _semantic(payload: dict[str, Any]) -> dict[str, Any]:
    """What each operator-only root *means* in *payload* (defaults filled in).

    Comparing meaning, not text, keeps a re-serialized file (a writer that
    spells out a default, reorders keys) from counting as a change, and keeps
    a planted value from counting as the writer's own just because the writer
    re-serialized it. Validated without the environment: the same process
    environment applies to every side of a comparison.
    """
    from agentos.gateway.config import GatewayConfig

    cfg = GatewayConfig.model_validate(_migrated(payload))
    return {root: _dump(_get_attr(cfg, root)) for root in OPERATOR_ONLY_CONFIG_ROOTS}


def held_roots(payload: dict[str, Any], approval: ConfigApproval) -> list[str]:
    """The operator-only roots *payload* holds differently from *approval*."""
    current = _semantic(payload)
    approved = _semantic(_with_sections(payload, approval.sections))
    return [root for root in OPERATOR_ONLY_CONFIG_ROOTS if current[root] != approved[root]]


# ── the approval file ───────────────────────────────────────────────────────


def load_approval(path: Path | None = None) -> ConfigApproval | None:
    """The stored approval; ``None`` when there is none yet.

    Raises :class:`ConfigApprovalError` for a file that exists but does not
    parse: treating it as absent would adopt whatever the config file says.
    """
    import tomllib

    target = path or approval_path()
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigApprovalError(f"cannot read {target.name}: {exc}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigApprovalError(f"cannot parse {target.name}: {exc}") from exc
    sections = data.get("sections")
    if data.get("version") != _VERSION or not isinstance(sections, dict):
        raise ConfigApprovalError(f"{target.name} is not a version {_VERSION} approval")
    config_path = data.get("config_path")
    return ConfigApproval(
        sections={str(k): v for k, v in sections.items() if k in OPERATOR_ONLY_CONFIG_ROOTS},
        config_path=str(config_path) if isinstance(config_path, str) else None,
    )


def write_approval(
    sections: dict[str, Any], config_path: str | Path | None, *, path: Path | None = None
) -> Path:
    """Store *sections* as the approved values, atomically and ``0600``."""
    import tomli_w

    target = path or approval_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {
        "version": _VERSION,
        "approved_at": int(time.time()),
        "sections": {
            root: copy.deepcopy(sections[root])
            for root in OPERATOR_ONLY_CONFIG_ROOTS
            if root in sections
        },
    }
    if config_path:
        document["config_path"] = str(config_path)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            tomli_w.dump(document, fh)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def approve_payload(payload: dict[str, Any], config_path: str | Path | None) -> None:
    """The operator applied the file: its operator-only roots are the approval."""
    write_approval(raw_sections(_migrated(payload)), config_path)


# ── who may move the approval forward ───────────────────────────────────────


def _state_root(payload: dict[str, Any]) -> Path:
    from agentos.paths import default_agentos_home

    state_dir = payload.get("state_dir")
    if isinstance(state_dir, str) and state_dir:
        return Path(state_dir).expanduser()
    return default_agentos_home() / "state"


def approval_refusal(payload: dict[str, Any]) -> str | None:
    """Why this process may not approve operator-only settings, or ``None``.

    An agent's session (its shell carries an agent token) never approves. A
    process other than the gateway that owns the state directory does not
    either: while a gateway runs, an agent's shell may be running too, and
    the gateway itself decides who the operator is (``config.reload``).
    """
    if in_agent_session():
        return "this command runs in an agent's session"
    pid = owning_gateway_pid(payload)
    if pid is not None:
        return f"the gateway (pid {pid}) is running"
    return None


def in_agent_session() -> bool:
    """Whether this process runs in an agent's session (it holds an agent token)."""
    from agentos.gateway.agent_surface import AGENT_TOKEN_ENV

    return bool(os.environ.get(AGENT_TOKEN_ENV))


def owning_gateway_pid(payload: dict[str, Any]) -> int | None:
    """The pid of a live gateway, other than this process, that owns the state."""
    from agentos.application.gateway_pidlock import live_gateway_pid

    return live_gateway_pid(_state_root(payload))


def record_write(
    before: dict[str, Any], after: dict[str, Any], config_path: str | Path | None
) -> list[str]:
    """Move the approval forward after a config write; return what stays held.

    *before* is the file as it was on disk, *after* what the writer stored.
    A root the writer changed is approved when this process may approve and
    the file held no unapproved edit of that root before the write. Every
    other difference from the approval stays held, and is returned.
    """
    try:
        approval = load_approval()
    except ConfigApprovalError as exc:
        log.warning("config.approval_unreadable", error=str(exc))
        approval = None
    refusal = approval_refusal(after)
    if approval is None:
        # No approval to protect yet: the gateway's first boot adopts the file.
        if refusal is None:
            try:
                write_approval(raw_sections(after), config_path)
            except OSError as exc:
                log.warning("config.approval_unwritable", error=str(exc))
        return []
    from pydantic import ValidationError

    try:
        new = _semantic(after)
        approved = _semantic(_with_sections(after, approval.sections))
    except (ValidationError, ValueError, TypeError) as exc:
        # Nothing to compare against: approve nothing; boot reports the rest.
        log.warning("config.approval_unusable", error=str(exc))
        return []
    try:
        previous: dict[str, Any] | None = _semantic(before) if before else approved
    except (ValidationError, ValueError, TypeError):
        # The file before this write did not validate: it approves nothing.
        previous = None
    sections = dict(approval.sections)
    held: list[str] = []
    approved_now: list[str] = []
    for root in OPERATOR_ONLY_CONFIG_ROOTS:
        if new[root] == approved[root]:
            continue
        if refusal is None and previous is not None and previous[root] == approved[root]:
            value = _get_raw(after, root)
            if value is _ABSENT:
                sections.pop(root, None)
            else:
                sections[root] = copy.deepcopy(value)
            approved_now.append(root)
        else:
            held.append(root)
    if approved_now:
        try:
            write_approval(sections, config_path)
        except OSError as exc:
            log.warning("config.approval_unwritable", error=str(exc))
            held = [r for r in OPERATOR_ONLY_CONFIG_ROOTS if r in held or r in approved_now]
    if held:
        log.warning(
            "config.approval_held",
            roots=",".join(held),
            reason=refusal or "the file already held an unapproved edit",
            hint="run `agentos config reload` from your own terminal to apply them",
        )
    return held


# ── boot ────────────────────────────────────────────────────────────────────


def enforce_approval(config: Any, *, adopt_missing: bool = True) -> list[str]:
    """Run *config* on the approved operator-only values; return the held roots.

    Called on the loaded config at gateway boot, before anything reads the
    trading limits, the auth posture or the hooks. A root the file holds
    differently from the approval keeps the approved value (environment
    overrides still apply on top, exactly as they would to the file). With no
    approval yet, *adopt_missing* stores the file's values as the first one.

    Raises :class:`agentos.gateway.config.GatewayConfigError` when the
    approval file is unreadable or the approved values no longer validate:
    the gateway refuses to start rather than guess.
    """
    from pydantic import ValidationError

    from agentos.gateway.config import GatewayConfig, GatewayConfigError

    if getattr(config, _ENFORCED_ATTR, False):
        return list(getattr(config, "_operator_config_held", ()))
    config_path = getattr(config, "config_path", None)
    try:
        payload = read_file_payload(config_path)
    except (OSError, ValueError) as exc:
        raise GatewayConfigError(f"cannot read the config file: {exc}") from exc
    try:
        approval = load_approval()
    except ConfigApprovalError as exc:
        raise GatewayConfigError(
            f"the operator config approval ({approval_path()}) is unreadable: {exc}. "
            "Restore it, or remove it to approve the config file as it is now."
        ) from exc
    if approval is None:
        if adopt_missing:
            try:
                write_approval(raw_sections(payload), config_path)
            except OSError as exc:
                log.warning("config.approval_unwritable", error=str(exc))
        _mark(config, [])
        return []
    try:
        held = held_roots(payload, approval)
        if held:
            approved = GatewayConfig(**_migrated(_with_sections(payload, approval.sections)))
    except (ValidationError, ValueError, TypeError) as exc:
        raise GatewayConfigError(
            f"the approved operator-only settings no longer validate: {exc}. "
            "Run `agentos config reload` while the gateway is stopped to approve "
            "the config file as it is now."
        ) from exc
    for root in held:
        _set_attr(config, root, copy.deepcopy(_get_attr(approved, root)))
    if held:
        log.warning(
            "config.boot_held_operator_only",
            roots=",".join(held),
            path=str(config_path or ""),
            hint="the gateway runs on the values you last approved; run "
            "`agentos config reload` from your own terminal to apply the file",
        )
    _mark(config, held)
    return held


def _mark(config: Any, held: list[str]) -> None:
    try:
        setattr(config, _ENFORCED_ATTR, True)
        setattr(config, "_operator_config_held", tuple(held))
    except (AttributeError, TypeError, ValueError):
        pass


def pending_roots(config: Any) -> list[str]:
    """Operator-only roots the config file holds unapproved (for status views)."""
    try:
        approval = load_approval()
        if approval is None:
            return []
        return held_roots(read_file_payload(getattr(config, "config_path", None)), approval)
    except Exception:  # noqa: BLE001 - a status view must not fail on this
        log.debug("config.approval_status_failed", exc_info=True)
        return []


__all__ = [
    "APPROVAL_FILENAME",
    "ConfigApproval",
    "ConfigApprovalError",
    "approval_path",
    "approval_refusal",
    "approve_payload",
    "enforce_approval",
    "held_roots",
    "in_agent_session",
    "owning_gateway_pid",
    "load_approval",
    "pending_roots",
    "raw_sections",
    "read_file_payload",
    "record_write",
    "write_approval",
]
