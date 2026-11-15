"""Environment-variable RPC — list, set, unset, and (audited) reveal.

The control surface's window onto :mod:`agentos.env_store`. Three of the four
methods never carry a value in either direction of a response: a listing says
whether a variable is set, where its value comes from, and what it is for, but
not what it is. That keeps the ordinary browsing path free of secrets, so a
screenshot, a log line, or a stray proxy cannot leak one.

Reading a real value is deliberately a separate operation with its own cost.
``env.reveal`` exists because operators genuinely need it — confirming which of
two keys is installed, copying one into another tool — but it is rate limited
and audited so it cannot be used as a bulk export.

Everything writable goes through :mod:`agentos.env_policy`, so the gate that
stops this surface from rewriting PATH or the sandbox switches is the same one
the CLI and the agent tool answer to.

A connection the gateway recognizes as an agent's (agent token, descendant
process, exec window) is held to more than that denylist: it may write only a
name the catalog marks ``agent_settable`` -- a skill's or a built-in tool's
credential -- and may not reveal anything the catalog treats as a secret.
Every other name is refused with ``env.protected``. The values written here
land in the gateway's own ``os.environ``, the process that holds the vault
keys; a name such as ``ECC_BACKEND_CLASS`` picks the code that signs.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

import structlog

from agentos import credential_sources, env_catalog, env_policy, env_store
from agentos.gateway.access import CONTROL_ONLY
from agentos.gateway.agent_surface import recognized_agent
from agentos.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher, require_params_dict

log = structlog.get_logger(__name__)

_d = get_dispatcher()

# Reveal budget. Generous enough for the "which key is this?" case, far too
# small to walk the whole file. Process-global on purpose: the limit protects
# the secrets, not any particular connection, so opening a second tab must not
# reset it.
_REVEAL_MAX_PER_WINDOW = 5
_REVEAL_WINDOW_SECONDS = 30.0
_reveal_times: deque[float] = deque(maxlen=_REVEAL_MAX_PER_WINDOW * 4)


def _available_from(name: str, is_set: bool) -> dict[str, str] | None:
    """Return the source that could supply *name*, when one is usable.

    Only offered for a variable that is not set: the point is to stop telling
    someone to go find a credential they already have authenticated elsewhere.
    Probing never reads a value.
    """
    if is_set:
        return None
    source = credential_sources.available_for(name)
    if source is None:
        return None
    return {"id": source.id, "label": source.label}


def _entry_payload(name: str, spec: env_catalog.EnvVarSpec) -> dict[str, Any]:
    """Return the wire form of one variable — description and state, no value."""
    entry = env_store.resolve_entry(name, secret=spec.secret)
    return {
        "name": name,
        "isSet": entry.is_set,
        "source": entry.source,
        # Present for a non-secret so the operator can see the actual setting;
        # masked for anything credential-shaped.
        "masked": entry.masked,
        "secret": spec.secret,
        "description": spec.description,
        "url": spec.url,
        "category": spec.category,
        "owner": spec.owner,
        "required": spec.required,
        "writable": entry.writable,
        "restartRequired": spec.restart_required,
        # A required variable that is not set is what a UI highlights; an
        # optional one that is unset is merely unconfigured.
        "missing": spec.required and not entry.is_set,
        # Set when the value is already obtainable from somewhere the operator
        # authenticated earlier, so a surface can offer to import it instead of
        # asking them to go find it.
        "availableFrom": _available_from(name, entry.is_set),
    }


def _catalog_for(ctx: RpcContext) -> dict[str, env_catalog.EnvVarSpec]:
    """Build the catalog, including names present only in the operator's file."""
    present = set(env_store.read_env_file())
    return env_catalog.build_catalog(getattr(ctx, "skill_loader", None), present_names=present)


def _uniswap_key_env(ctx: RpcContext) -> str:
    trading = getattr(getattr(ctx, "config", None), "trading", None)
    return str(getattr(trading, "uniswap_api_key_env", "") or "").strip().upper()


def _refuse_agent_write(ctx: RpcContext, name: str, action: str) -> None:
    """An agent's connection may write only an ``agent_settable`` catalog name.

    The operator's own connections (and ``unbound`` ones -- the web console
    on loopback is unbound) keep the denylist alone.
    """
    binding = recognized_agent(ctx)
    if binding is None:
        return
    spec = _catalog_for(ctx).get(name)
    if (
        spec is not None
        and spec.agent_settable
        and env_policy.agent_may_own(name)
        and name.upper() != _uniswap_key_env(ctx)
    ):
        return
    log.warning("env.agent_write_refused", key=name, action=action, via=binding.via)
    raise RpcHandlerError(
        "env.protected",
        f"{name} is not a skill or tool credential an agent may {action}. "
        "Ask the user to set it in Settings > Environment or with `agentos env set`.",
        details={**binding.to_dict(), "name": name},
    )


def _refuse_agent_reveal(ctx: RpcContext, name: str) -> None:
    """An agent's connection may not read back a secret (or an unknown name)."""
    binding = recognized_agent(ctx)
    if binding is None:
        return
    spec = _catalog_for(ctx).get(name)
    if spec is not None and not spec.secret:
        return
    log.warning("env.agent_reveal_refused", key=name, via=binding.via)
    raise RpcHandlerError(
        "env.protected",
        f"{name} is a secret; an agent cannot read it back. Ask the user if it is needed.",
        details={**binding.to_dict(), "name": name},
    )


def _require_name(params: dict | None) -> str:
    if not isinstance(params, dict) or not params.get("name"):
        raise ValueError("params.name is required")
    name = str(params["name"]).strip()
    if not name:
        raise ValueError("params.name is required")
    return name


@_d.method("env.list", CONTROL_ONLY)
async def _handle_env_list(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Return every known environment variable and its state — never a value."""
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")

    catalog = _catalog_for(ctx)
    entries = [_entry_payload(name, spec) for name, spec in sorted(catalog.items())]
    return {
        "envFilePath": str(env_store.env_file_path()),
        "vars": entries,
        "setCount": sum(1 for e in entries if e["isSet"]),
        "totalCount": len(entries),
        # Variables whose file value is being overridden by the environment the
        # gateway was started with. Editing the file will not change these
        # until the export is removed, which is otherwise invisible and
        # produces "I saved it and nothing happened" reports.
        "shadowedCount": sum(1 for e in entries if e["source"] == "process"),
    }


@_d.method("env.set", CONTROL_ONLY)
async def _handle_env_set(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Write one variable. Returns its new state, without echoing the value."""
    name = _require_name(params)
    params = require_params_dict(params)
    if "value" not in params:
        raise ValueError("params.value is required")
    value = params["value"]
    if not isinstance(value, str):
        raise ValueError("params.value must be a string")
    _refuse_agent_write(ctx, name, "set")

    try:
        env_store.set_env_var(name, value)
    except env_policy.EnvPolicyError as exc:
        # Surfaced verbatim: the message explains which class of name was
        # refused and what to do instead, which is more useful than a code.
        raise ValueError(str(exc)) from exc

    spec = env_catalog.describe(name, _catalog_for(ctx))
    payload = _entry_payload(name, spec)
    log.info("env.rpc_set", key=name, restart_required=payload["restartRequired"])
    return payload


@_d.method("env.unset", CONTROL_ONLY)
async def _handle_env_unset(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Remove one variable from the AgentOS .env."""
    name = _require_name(params)
    _refuse_agent_write(ctx, name, "unset")
    try:
        removed = env_store.unset_env_var(name)
    except env_policy.EnvPolicyError as exc:
        raise ValueError(str(exc)) from exc

    spec = env_catalog.describe(name, _catalog_for(ctx))
    payload = _entry_payload(name, spec)
    payload["removed"] = removed
    log.info("env.rpc_unset", key=name, removed=removed)
    return payload


@_d.method("env.import", CONTROL_ONLY)
async def _handle_env_import(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Copy a credential in from a source that already holds it.

    Explicit by construction: nothing imports on its own, because "AgentOS took
    my GitHub token and gave it to an agent" is not a surprise anyone should
    get. The value goes source → store; it is not returned here.
    """
    name = _require_name(params)
    params = require_params_dict(params)
    source_id = str(params.get("sourceId") or "").strip()
    if not source_id:
        raise ValueError("params.sourceId is required")
    _refuse_agent_write(ctx, name, "import")

    try:
        value = credential_sources.read_from(name, source_id)
    except LookupError as exc:
        raise ValueError(str(exc)) from exc
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc

    try:
        env_store.set_env_var(name, value)
    except env_policy.EnvPolicyError as exc:
        raise ValueError(str(exc)) from exc

    spec = env_catalog.describe(name, _catalog_for(ctx))
    payload = _entry_payload(name, spec)
    payload["importedFrom"] = source_id
    # A copy does not follow the source's own rotation; say so once, here,
    # rather than letting it become a mystery 401 later.
    payload["note"] = (
        f"Copied from {source_id}. It will not update when that source rotates "
        "its credential — re-import to refresh."
    )
    log.info("env.imported", key=name, source=source_id)
    return payload


@_d.method("env.reveal", CONTROL_ONLY)
async def _handle_env_reveal(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Return one variable's real value. Rate limited and audited.

    The audit line records the name, never the value — the point of the log is
    that someone can later tell which secrets were read, not to make a second
    copy of them.
    """
    name = _require_name(params)
    # Before the budget: an agent's refused reveal must not use it up.
    _refuse_agent_reveal(ctx, name)

    now = time.monotonic()
    while _reveal_times and now - _reveal_times[0] > _REVEAL_WINDOW_SECONDS:
        _reveal_times.popleft()
    if len(_reveal_times) >= _REVEAL_MAX_PER_WINDOW:
        log.warning("env.reveal_rate_limited", key=name)
        raise ValueError(
            f"Too many reveal requests (limit {_REVEAL_MAX_PER_WINDOW} per "
            f"{int(_REVEAL_WINDOW_SECONDS)}s). Try again shortly."
        )
    _reveal_times.append(now)

    value = env_store.get_env_value(name)
    if value is None:
        raise KeyError(f"Environment variable is not set: {name}")

    log.info("env.revealed", key=name, conn_id=ctx.conn_id)
    return {"name": name, "value": value}


def _reset_reveal_budget() -> None:
    """Clear the reveal rate-limit window. Test seam only."""
    _reveal_times.clear()
