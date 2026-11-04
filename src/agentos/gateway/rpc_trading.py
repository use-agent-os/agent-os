"""``wallet.*`` and ``trading.*`` RPC — the engine side of the Trading page.

Thin handlers: validate params, call :class:`agentos.trading.service.TradingService`,
map its coded errors to :class:`RpcHandlerError`. The service is a lazy
module singleton built from ``ctx.config`` on first use; its background
sync/expiry loop starts the first time a handler runs on a live event loop.
Control-plane only.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import re
import unicodedata
from collections.abc import Awaitable, Callable, Iterator
from decimal import Decimal
from typing import Any

from agentos.gateway.agent_surface import agent_binding
from agentos.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from agentos.trading import get_trading_service
from agentos.trading.chains import ChainSpec, resolve_chain
from agentos.trading.providers import PROVIDER_IDS
from agentos.trading.service import (
    DEFAULT_CHART_RANGE,
    TradingError,
    TradingService,
    _err,
)

_d = get_dispatcher()

_VALID_EXPORT_FORMATS = {"keystore", "privateKey"}
_VALID_INITIATORS = {"manual", "agent"}
NOTE_MAX_CHARS = 240
# Unicode bidi controls (LRM/RLM/ALM, the embedding/override/isolate pairs):
# in a note they can make "send to A" read as "send to B" in the approval card.
_BIDI_CONTROLS = "\u200e\u200f\u061c\u202a-\u202e\u2066-\u2069"
_NOTE_STRIP = re.compile(f"[\\x00-\\x1f\\x7f-\\x9f{_BIDI_CONTROLS}]")
_NOTE_SPACES = re.compile(r"\s+")


def _require_operator(ctx: RpcContext, action: str) -> None:
    """Refuse an agent's connection: this is the user's action, not the agent's.

    The binding is computed at admission by ``gateway.agent_surface``; a
    client cannot talk its way out of it with a parameter.
    """
    binding = agent_binding(ctx)
    if binding is not None:
        raise RpcHandlerError(
            "trading.operator_required",
            f"{action} is the user's action; an agent cannot do it. "
            "Ask the user to do it in the app.",
            details=binding.to_dict(),
        )


def _operator_only(fn: Callable[[dict | None, RpcContext], Awaitable[dict[str, Any]]]) -> Any:
    @functools.wraps(fn)
    async def wrapper(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
        _require_operator(ctx, fn.__name__.strip("_").replace("_", "."))
        return await fn(params, ctx)

    return wrapper


def _initiator(ctx: RpcContext, p: dict[str, Any]) -> tuple[str, str | None]:
    """Who is asking, decided server-side.

    An agent-bound connection is an agent whatever it declares, and its
    orders are filed under the chat the binding names. Only an unbound
    connection may call itself ``manual``.
    """
    declared = _str(p, "initiator") or "manual"
    if declared not in _VALID_INITIATORS:
        raise ValueError("params.initiator must be 'manual' or 'agent'")
    session_key = _str(p, "sessionKey")
    binding = agent_binding(ctx)
    if binding is not None:
        return "agent", binding.session_key or session_key
    return declared, session_key


async def _broadcast(event: str, payload: dict[str, Any]) -> None:
    """Fan an engine event out to every authenticated WebSocket connection."""
    from agentos.gateway.websocket import get_registry

    try:
        await get_registry().broadcast(event, payload)
    except Exception:  # pragma: no cover - best effort
        pass


def _service(ctx: RpcContext) -> TradingService:
    service = get_trading_service(ctx.config, broadcast=_broadcast)
    service.ensure_started()
    return service


def _params(params: dict | None) -> dict[str, Any]:
    return params if isinstance(params, dict) else {}


def _str(params: dict[str, Any], key: str, *, required: bool = False) -> str | None:
    value = params.get(key)
    if value is None or value == "":
        if required:
            raise ValueError(f"params.{key} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"params.{key} must be a string")
    return value.strip()


def _note(params: dict[str, Any], key: str = "note") -> str | None:
    """A free-text note as it will be shown in history and the approval card.

    C0/C1 control characters and Unicode bidi controls are dropped, runs of
    whitespace collapse to one space, and the text is cut at
    :data:`NOTE_MAX_CHARS`. The note is the one field an agent writes that a
    person reads before approving, so it must render as it was typed.
    """
    raw = _str(params, key)
    if raw is None:
        return None
    text = unicodedata.normalize("NFC", raw)
    # Whitespace first (a newline or tab becomes a space, not nothing), then
    # the controls that are not whitespace, then collapse what that left.
    text = _NOTE_SPACES.sub(" ", text)
    text = _NOTE_STRIP.sub("", text)
    text = _NOTE_SPACES.sub(" ", text).strip()
    if not text:
        return None
    return text[:NOTE_MAX_CHARS].rstrip()


def _without_paths(ctx: RpcContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Drop filesystem paths (``vaultPath`` and any other ``*Path``) for an agent.

    Where the keystores live is the operator's business; an agent only needs
    to know whether the vault is set up and unlocked.
    """
    if agent_binding(ctx) is None:
        return payload
    return {k: v for k, v in payload.items() if not (isinstance(k, str) and k.endswith("Path"))}


def _client_order_id(service: TradingService, method: str, p: dict[str, Any]) -> dict[str, Any]:
    """``clientOrderId`` → ``client_order_id=`` for the service call.

    An idempotency key must never be dropped on the floor: a retry that lost
    its key is a second trade. So when the engine's ``method`` does not take
    one, a caller that sent one is refused instead of served without it.
    """
    value = _str(p, "clientOrderId")
    if value is None:
        return {}
    fn = getattr(type(service), method, None)
    try:
        accepted = fn is not None and "client_order_id" in inspect.signature(fn).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins without a signature
        accepted = False
    if not accepted:
        raise RpcHandlerError(
            "trading.invalid", f"clientOrderId is not supported by this engine's {method}"
        )
    return {"client_order_id": value}


def _number(params: dict[str, Any], key: str) -> float | None:
    value = params.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"params.{key} must be a number")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"params.{key} must be a number") from exc


def _raw_amount(params: dict[str, Any], key: str) -> int | None:
    """A token amount in base units: an integer or an int-like string, never a float."""
    value = params.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"params.{key} must be an integer string")
    if isinstance(value, str):
        value = value.strip()
        if not value.isdigit():
            raise ValueError(f"params.{key} must be an integer string")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"params.{key} must be an integer string") from exc


def _int(params: dict[str, Any], key: str, default: int) -> int:
    value = params.get(key)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(f"params.{key} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"params.{key} must be an integer") from exc


def _chain(params: dict[str, Any], *, required: bool = True) -> ChainSpec | None:
    value = params.get("chainId", params.get("chain"))
    if value is None or value == "":
        if required:
            raise ValueError("params.chainId is required")
        return None
    try:
        return resolve_chain(value)
    except ValueError as exc:
        raise RpcHandlerError("trading.unsupported_chain", str(exc)) from exc


def _raise(exc: Exception) -> RpcHandlerError:
    if isinstance(exc, RpcHandlerError):
        return exc
    error: TradingError = _err(exc)
    return RpcHandlerError(error.code, str(error), details=error.details)


# ── wallet.* ───────────────────────────────────────────────────────────────


@_d.method("wallet.status")
async def _wallet_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    service = _service(ctx)
    service.ensure_unlocked()
    return _without_paths(ctx, service.vault.status())


@_d.method("wallet.setup")
@_operator_only
async def _wallet_setup(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    password = _str(p, "password", required=True) or ""
    mode = _str(p, "unlockMode") or "auto"
    if mode not in ("auto", "manual"):
        raise ValueError("params.unlockMode must be 'auto' or 'manual'")
    service = _service(ctx)
    try:
        service.vault.setup(password, mode)  # type: ignore[arg-type]
    except Exception as exc:
        raise _raise(exc) from exc
    await service._emit("trading.changed", {"reason": "wallet"})
    return {"initialized": True, **service.vault.status()}


@_d.method("wallet.unlock")
@_operator_only
async def _wallet_unlock(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    password = _str(p, "password", required=True) or ""
    service = _service(ctx)
    try:
        service.vault.unlock(password)
    except Exception as exc:
        raise _raise(exc) from exc
    await service._emit("trading.changed", {"reason": "wallet"})
    return {"unlocked": True}


@_d.method("wallet.lock")
@_operator_only
async def _wallet_lock(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    service = _service(ctx)
    service.vault.lock()
    await service._emit("trading.changed", {"reason": "wallet"})
    return {"unlocked": False}


@_d.method("wallet.setUnlockMode")
@_operator_only
async def _wallet_set_unlock_mode(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    mode = _str(p, "mode", required=True) or ""
    password = _str(p, "password", required=True) or ""
    if mode not in ("auto", "manual"):
        raise ValueError("params.mode must be 'auto' or 'manual'")
    service = _service(ctx)
    try:
        service.vault.set_unlock_mode(mode, password)  # type: ignore[arg-type]
    except Exception as exc:
        raise _raise(exc) from exc
    await service._emit("trading.changed", {"reason": "wallet"})
    return {"unlockMode": mode}


@_d.method("wallet.changePassword")
@_operator_only
async def _wallet_change_password(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    password = _str(p, "password", required=True) or ""
    new_password = _str(p, "newPassword", required=True) or ""
    service = _service(ctx)
    try:
        service.vault.change_password(password, new_password)
    except Exception as exc:
        raise _raise(exc) from exc
    return {"changed": True}


@_d.method("wallet.list")
async def _wallet_list(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    service = _service(ctx)
    service.ensure_unlocked()
    try:
        wallets = service.wallet_dicts()
        primary = service.vault.primary_address() if service.vault.initialized else None
    except Exception as exc:
        raise _raise(exc) from exc
    return {"wallets": wallets, "primary": primary}


@_d.method("wallet.create")
@_operator_only
async def _wallet_create(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    label = _str(p, "label") or ""
    service = _service(ctx)
    try:
        return {"wallet": await service.create_wallet(label)}
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("wallet.import")
@_operator_only
async def _wallet_import(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    label = _str(p, "label") or ""
    private_key = _str(p, "privateKey")
    keystore_json = _str(p, "keystoreJson")
    keystore_password = _str(p, "keystorePassword")
    if not private_key and not keystore_json:
        raise ValueError("params.privateKey or params.keystoreJson is required")
    service = _service(ctx)
    try:
        wallet = await service.import_wallet(
            label,
            private_key=private_key,
            keystore_json=keystore_json,
            keystore_password=keystore_password,
        )
    except Exception as exc:
        raise _raise(exc) from exc
    return {"wallet": wallet}


@_d.method("wallet.export")
@_operator_only
async def _wallet_export(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    address = _str(p, "address", required=True) or ""
    password = _str(p, "password", required=True) or ""
    fmt = _str(p, "format") or "keystore"
    if fmt not in _VALID_EXPORT_FORMATS:
        raise ValueError("params.format must be 'keystore' or 'privateKey'")
    service = _service(ctx)
    try:
        secret = service.vault.export(address, password, fmt)  # type: ignore[arg-type]
    except Exception as exc:
        raise _raise(exc) from exc
    return {"keystoreJson": secret} if fmt == "keystore" else {"privateKey": secret}


@_d.method("wallet.rename")
@_operator_only
async def _wallet_rename(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    address = _str(p, "address", required=True) or ""
    label = _str(p, "label", required=True) or ""
    service = _service(ctx)
    try:
        record = service.vault.rename(address, label)
        service._mirror_wallet(record)
    except Exception as exc:
        raise _raise(exc) from exc
    await service._emit("trading.changed", {"reason": "wallet", "wallet": record.address})
    primary = service.vault.primary_address()
    return {"wallet": record.to_dict(primary=(record.address == primary))}


@_d.method("wallet.remove")
@_operator_only
async def _wallet_remove(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    address = _str(p, "address", required=True) or ""
    password = _str(p, "password", required=True) or ""
    service = _service(ctx)
    try:
        await service.remove_wallet(address, password)
    except Exception as exc:
        raise _raise(exc) from exc
    return {"removed": True}


@_d.method("wallet.setPrimary")
@_operator_only
async def _wallet_set_primary(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    address = _str(p, "address", required=True) or ""
    service = _service(ctx)
    try:
        record = service.vault.set_primary(address)
        for item in service.vault.list():
            service._mirror_wallet(item)
    except Exception as exc:
        raise _raise(exc) from exc
    await service._emit("trading.changed", {"reason": "wallet", "wallet": record.address})
    return {"wallet": record.to_dict(primary=True)}


@_d.method("wallet.balances")
async def _wallet_balances(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Balances from the ledger. ``refresh: true`` forces a chain read first (throttled).

    ``chains`` says how fresh each wallet/chain is: ``ok``, ``partial`` (some
    token reads failed, their rows are last-good) or ``failed`` (the node was
    unreachable). ``updatedAt`` is the newest balance row returned.
    """
    p = _params(params)
    address = _str(p, "address")
    chain = _chain(p, required=False)
    chain_id = chain.chain_id if chain else None
    service = _service(ctx)
    service.ensure_unlocked()
    include_hidden = bool(p.get("includeHidden"))
    try:
        rows = await service.balances(
            address, chain_id, refresh=bool(p.get("refresh")), include_hidden=include_hidden
        )
        reads = service.chain_reads(address, chain_id)
        hidden = service.hidden_balance_count(address, chain_id)
    except Exception as exc:
        raise _raise(exc) from exc
    newest = max((int(r.get("updatedAt") or 0) for r in rows), default=0)
    return {"balances": rows, "hiddenCount": hidden, "chains": reads, "updatedAt": newest or None}


# ── trading.* ──────────────────────────────────────────────────────────────


@_d.method("trading.status")
async def _trading_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    service = _service(ctx)
    service.ensure_unlocked()
    return _without_paths(ctx, await service.status(check_rpc=bool(p.get("checkRpc"))))


@_d.method("trading.probe")
async def _trading_probe(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Is the provider reachable? With ``apiKey`` it also tests a key that is not
    in config, which turns the gateway into a key oracle — so that form is the
    operator's only."""
    p = _params(params)
    api_key = _str(p, "apiKey")
    if api_key is not None:
        _require_operator(ctx, "trading.probe(apiKey)")
    service = _service(ctx)
    try:
        return await service.probe(api_key, provider_id=_str(p, "provider"))
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.setProvider")
async def _trading_set_provider(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Persist ``trading.provider`` through the same path as ``config.set``.

    Convenience for the CLI; the desktop may equally call
    ``config.patch {trading: {provider}}``. Both take effect on the next
    trading call without a gateway restart.
    """
    p = _params(params)
    provider = (_str(p, "provider", required=True) or "").lower()
    if provider not in PROVIDER_IDS:
        raise ValueError(
            "params.provider must be one of: " + ", ".join(repr(p) for p in PROVIDER_IDS)
        )
    result = await get_dispatcher().dispatch(
        "trading.setProvider",
        "config.set",
        {"path": "trading.provider", "value": provider},
        ctx,
    )
    if not result.ok:
        message = result.error.message if result.error else "config write failed"
        code = result.error.code if result.error else "trading.error"
        raise RpcHandlerError(str(code), str(message))
    service = _service(ctx)
    await service._emit("trading.changed", {"reason": "config", "provider": provider})
    payload = result.payload if isinstance(result.payload, dict) else {}
    return {"provider": provider, "restartRequired": bool(payload.get("restartRequired"))}


@_d.method("trading.tokens.search")
async def _trading_tokens_search(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    chain = _chain(p)
    assert chain is not None
    query = _str(p, "query", required=True) or ""
    service = _service(ctx)
    try:
        return {"tokens": await service.search_tokens(chain, query)}
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.tokens.resolve")
async def _trading_tokens_resolve(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    chain = _chain(p)
    assert chain is not None
    value = _str(p, "address") or _str(p, "token", required=True) or ""
    service = _service(ctx)
    try:
        meta = await service.resolve_token(chain, value)
    except Exception as exc:
        raise _raise(exc) from exc
    return {"token": meta.to_dict()}


@_d.method("trading.tokens.hide")
@_operator_only
async def _trading_tokens_hide(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Hide (``hidden: true``, the default) or show a token. The user's call, and final."""
    p = _params(params)
    chain = _chain(p)
    assert chain is not None
    address = _str(p, "address", required=True) or ""
    hidden = p.get("hidden", True)
    if not isinstance(hidden, bool):
        raise RpcHandlerError("trading.invalid", "hidden must be a boolean")
    service = _service(ctx)
    try:
        token = await service.set_token_hidden(chain, address, hidden)
    except Exception as exc:
        raise _raise(exc) from exc
    return {"token": token}


@_d.method("trading.quote")
async def _trading_quote(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    chain = _chain(p)
    assert chain is not None
    initiator, _session = _initiator(ctx, p)
    service = _service(ctx)
    service.ensure_unlocked()
    try:
        return await service.quote(
            chain=chain,
            wallet=_str(p, "wallet"),
            token_in=_str(p, "tokenIn", required=True) or "",
            token_out=_str(p, "tokenOut", required=True) or "",
            amount_in=_str(p, "amountIn"),
            amount_usd=_number(p, "amountUsd"),
            slippage_pct=_number(p, "slippagePct"),
            initiator=initiator,  # type: ignore[arg-type]
        )
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.swap")
async def _trading_swap(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    chain = _chain(p)
    assert chain is not None
    initiator, session_key = _initiator(ctx, p)
    wallets = p.get("wallets", p.get("wallet"))
    if wallets is not None and not isinstance(wallets, str | list):
        raise ValueError("params.wallets must be an address list, 'all' or omitted")
    amount_in = p.get("amountIn")
    if amount_in is not None and not isinstance(amount_in, str | int | float):
        raise ValueError("params.amountIn must be a decimal string")
    service = _service(ctx)
    try:
        orders = await service.swap(
            chain=chain,
            wallets=wallets,
            token_in=_str(p, "tokenIn", required=True) or "",
            token_out=_str(p, "tokenOut", required=True) or "",
            amount_in=str(amount_in) if amount_in is not None else None,
            amount_pct=_number(p, "amountPct"),
            amount_usd=_number(p, "amountUsd"),
            slippage_pct=_number(p, "slippagePct"),
            initiator=initiator,  # type: ignore[arg-type]
            session_key=session_key,
            note=_note(p),
            wait=bool(p.get("wait")),
            expected_out_raw=_raw_amount(p, "expectedOutRaw"),
            min_out_raw=_raw_amount(p, "minOutRaw"),
            quote_id=_str(p, "quoteId"),
            **_client_order_id(service, "swap", p),
        )
    except Exception as exc:
        raise _raise(exc) from exc
    return {"orders": orders}


def _recipients(p: dict[str, Any]) -> list[dict[str, Any]]:
    """Recipients from either shape: ``recipients: [{to, amount|amountUsd}]``
    or the one-address ``to`` + ``amount``/``amountUsd`` form."""
    listed = p.get("recipients")
    if listed is not None:
        if not isinstance(listed, list) or not listed:
            raise ValueError("params.recipients must be a non-empty list")
        out: list[dict[str, Any]] = []
        for item in listed:
            if not isinstance(item, dict):
                raise ValueError("params.recipients entries must be objects")
            entry: dict[str, Any] = {"to": _str(item, "to", required=True)}
            amount = item.get("amount")
            if amount is not None and amount != "":
                if not isinstance(amount, str | int | float) or isinstance(amount, bool):
                    raise ValueError("recipient amount must be a decimal string")
                entry["amount"] = str(amount)
            usd = _number(item, "amountUsd")
            if usd is not None:
                entry["amountUsd"] = usd
            out.append(entry)
        return out
    to = _str(p, "to", required=True) or ""
    entry = {"to": to}
    amount = p.get("amount")
    if amount is not None and amount != "":
        if not isinstance(amount, str | int | float) or isinstance(amount, bool):
            raise ValueError("params.amount must be a decimal string")
        entry["amount"] = str(amount)
    usd = _number(p, "amountUsd")
    if usd is not None:
        entry["amountUsd"] = usd
    return [entry]


@_d.method("trading.send")
async def _trading_send(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Send one token to one or many addresses. An agent's send always parks for approval."""
    p = _params(params)
    chain = _chain(p)
    assert chain is not None
    initiator, session_key = _initiator(ctx, p)
    recipients = _recipients(p)
    service = _service(ctx)
    try:
        orders = await service.send(
            chain=chain,
            wallet=_str(p, "wallet"),
            token=_str(p, "token", required=True) or "",
            recipients=recipients,
            initiator=initiator,  # type: ignore[arg-type]
            session_key=session_key,
            note=_note(p),
            wait=bool(p.get("wait")),
            **_client_order_id(service, "send", p),
        )
    except Exception as exc:
        raise _raise(exc) from exc
    return {"orders": orders, "batchId": orders[0].get("batchId") if orders else None}


# ── trading.bridge.* (docs/bridge.md) ───────────────────────────────────────
#
# A bridge moves ETH or the USD stable between Base and Robinhood Chain through
# Relay. An agent may quote and create (its order always parks for approval,
# like a send); the operator approves through ``trading.orders.approve``.


def _bridge_chains(p: dict[str, Any]) -> tuple[ChainSpec, ChainSpec]:
    origin = _chain(p)
    assert origin is not None
    value = p.get("toChainId", p.get("toChain"))
    if value is None or value == "":
        raise ValueError("params.toChainId is required")
    try:
        destination = resolve_chain(value)
    except ValueError as exc:
        raise RpcHandlerError("trading.unsupported_chain", str(exc)) from exc
    return origin, destination


def _bridge_size(p: dict[str, Any]) -> dict[str, Any]:
    """Exactly one of ``amount`` (human units), ``amountUsd`` or ``amountPct``."""
    amount = _decimal_text(p, "amount")
    usd = _number(p, "amountUsd")
    pct = _number(p, "amountPct")
    given = [x for x in (amount, usd, pct) if x is not None]
    if len(given) != 1:
        raise ValueError("params need exactly one of amount, amountUsd or amountPct")
    return {"amount_in": amount, "amount_usd": usd, "amount_pct": pct}


@_d.method("trading.bridge.routes")
async def _trading_bridge_routes(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Every route the bridge serves (both directions)."""
    from agentos.trading.bridge import list_routes

    return {"routes": list_routes()}


@_d.method("trading.bridge.quote")
async def _trading_bridge_quote(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Price a bridge: what arrives on the other chain, fees, and the guard's verdict."""
    p = _params(params)
    origin, destination = _bridge_chains(p)
    size = _bridge_size(p)
    initiator, _session_key = _initiator(ctx, p)
    service = _service(ctx)
    try:
        quote = await service.bridge_quote(
            origin=origin,
            destination=destination,
            token=_str(p, "token", required=True) or "",
            wallet=_str(p, "wallet"),
            initiator=initiator,  # type: ignore[arg-type]
            **size,
        )
    except Exception as exc:
        raise _raise(exc) from exc
    return {"quote": quote}


@_d.method("trading.bridge.create")
async def _trading_bridge_create(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Create a bridge order. An agent's always parks for approval; the user's runs."""
    p = _params(params)
    origin, destination = _bridge_chains(p)
    size = _bridge_size(p)
    initiator, session_key = _initiator(ctx, p)
    service = _service(ctx)
    try:
        order = await service.bridge(
            origin=origin,
            destination=destination,
            token=_str(p, "token", required=True) or "",
            wallet=_str(p, "wallet"),
            initiator=initiator,  # type: ignore[arg-type]
            session_key=session_key,
            note=_note(p),
            wait=bool(p.get("wait")),
            **size,
            **_client_order_id(service, "bridge", p),
        )
    except Exception as exc:
        raise _raise(exc) from exc
    return {"order": order}


@_d.method("trading.bridge.status")
async def _trading_bridge_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """A bridge order by ``orderId`` or Relay ``requestId``, its fill refreshed when in flight."""
    p = _params(params)
    order_id = _str(p, "orderId")
    request_id = _str(p, "requestId")
    if not order_id and not request_id:
        raise ValueError("params.orderId or params.requestId is required")
    service = _service(ctx)
    try:
        return await service.bridge_status(order_id=order_id, request_id=request_id)
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.orders.batch")
async def _trading_orders_batch(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    batch_id = _str(p, "batchId", required=True) or ""
    service = _service(ctx)
    try:
        return {"orders": service.batch(batch_id), "batchId": batch_id}
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.allowances.list")
async def _trading_allowances_list(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """What a wallet has approved others to spend, per chain (all chains when none given)."""
    p = _params(params)
    chain = _chain(p, required=False)
    service = _service(ctx)
    service.ensure_unlocked()
    full = bool(p.get("full"))
    wait = bool(p.get("wait"))
    wallet = _str(p, "wallet")
    try:
        if chain is not None:
            return await service.allowances(chain, wallet, full=full, wait=wait)
        results = [
            await service.allowances(c, wallet, full=full, wait=wait) for c in service.chains()
        ]
    except Exception as exc:
        raise _raise(exc) from exc
    rows = [a for r in results for a in r["allowances"]]
    return {
        "wallet": results[0]["wallet"] if results else wallet,
        "chainId": None,
        "allowances": rows,
        "count": len(rows),
        "unlimitedCount": sum(int(r["unlimitedCount"]) for r in results),
        "scanning": any(bool(r["scanning"]) for r in results),
        "chains": [
            {
                "chainId": r["chainId"],
                "count": r["count"],
                "scanning": r["scanning"],
                "scannedTo": r["scannedTo"],
                "scanFrom": r["scanFrom"],
                "head": r["head"],
            }
            for r in results
        ],
    }


@_d.method("trading.allowances.revoke")
async def _trading_allowances_revoke(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Set an allowance to zero. From an agent this parks an order; from the user it runs."""
    p = _params(params)
    chain = _chain(p)
    assert chain is not None
    initiator, session_key = _initiator(ctx, p)
    service = _service(ctx)
    try:
        order = await service.revoke(
            chain=chain,
            wallet=_str(p, "wallet"),
            token=_str(p, "token", required=True) or "",
            spender=_str(p, "spender", required=True) or "",
            initiator=initiator,  # type: ignore[arg-type]
            session_key=session_key,
            note=_note(p),
            wait=bool(p.get("wait")),
        )
    except Exception as exc:
        raise _raise(exc) from exc
    return {"order": order}


@_d.method("trading.decode")
async def _trading_decode(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Explain a transaction hash, or raw calldata (with an optional ``to``)."""
    p = _params(params)
    chain = _chain(p)
    assert chain is not None
    tx_hash = _str(p, "txHash")
    data = _str(p, "data")
    if not tx_hash and data is None:
        raise ValueError("params.txHash or params.data is required")
    service = _service(ctx)
    try:
        return await service.decode(chain, tx_hash=tx_hash, data=data, to=_str(p, "to"))
    except Exception as exc:
        raise _raise(exc) from exc


# ── trading.lp.* (Uniswap V4 read-outs; payloads in docs/lp-cards.md) ─────
#
# Read-only, so an agent may call every one of them. Each runs the blocking V4
# library in a worker thread (see ``agentos.trading.lp``).


def _lp_target(p: dict[str, Any]) -> str:
    value = _str(p, "target") or _str(p, "token") or _str(p, "poolId")
    if not value:
        raise ValueError("params.target (a token symbol, address or poolId) is required")
    return value


def _with_request(result: dict[str, Any], kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """Echo ``request: {kind, params}`` into a card payload so the card can re-run itself.

    ``params`` are what ``trading.lp.<kind>`` takes, normalised; ``None`` values
    are left out. A single-chain read that found its pool on one chain names
    that chain, so a refresh reads the same pool rather than probing again.
    """
    if not isinstance(result, dict):  # a test double may answer with anything
        return result
    clean = {k: v for k, v in params.items() if v is not None}
    return {**result, "request": {"kind": kind, "params": clean}}


def _read_chain_id(result: Any, chain: ChainSpec | None) -> int | None:
    found = result.get("chain") if isinstance(result, dict) else None
    if isinstance(found, dict) and isinstance(found.get("id"), int):
        return int(found["id"])
    return chain.chain_id if chain is not None else None


@_d.method("trading.lp.pool")
async def _trading_lp_pool(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """A token's (deepest) V4 pool: reserves, launcher, lock status, biggest ranges."""
    from agentos.trading import lp

    p = _params(params)
    chain = _chain(p, required=False)
    target = _lp_target(p)
    quote = _str(p, "quote")
    service = _service(ctx)
    try:
        fee = lp.parse_fee(p.get("feePct"))
        result = await lp.lp_pool(service, chain=chain, target=target, quote=quote, fee=fee)
    except Exception as exc:
        raise _raise(exc) from exc
    echo = {
        "target": target,
        "chainId": _read_chain_id(result, chain),
        "quote": quote,
        "feePct": lp.fee_label(fee) if fee is not None else None,
    }
    return _with_request(result, "pool", echo)


@_d.method("trading.lp.ranges")
async def _trading_lp_ranges(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """A pool's liquidity distribution as contiguous tick segments."""
    from agentos.trading import lp

    p = _params(params)
    chain = _chain(p, required=False)
    target = _lp_target(p)
    quote = _str(p, "quote")
    service = _service(ctx)
    try:
        fee = lp.parse_fee(p.get("feePct"))
        result = await lp.lp_ranges(service, chain=chain, target=target, quote=quote, fee=fee)
    except Exception as exc:
        raise _raise(exc) from exc
    echo = {
        "target": target,
        "chainId": _read_chain_id(result, chain),
        "quote": quote,
        "feePct": lp.fee_label(fee) if fee is not None else None,
    }
    return _with_request(result, "ranges", echo)


@_d.method("trading.lp.position")
async def _trading_lp_position(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """One V4 position NFT: range, status, principal, uncollected fees."""
    from agentos.trading import lp

    p = _params(params)
    chain = _chain(p)
    assert chain is not None
    token_id = _raw_amount(p, "tokenId")
    if token_id is None or token_id <= 0:
        raise ValueError("params.tokenId must be a positive integer")
    service = _service(ctx)
    try:
        result = await lp.lp_position(service, chain=chain, token_id=token_id)
    except Exception as exc:
        raise _raise(exc) from exc
    echo = {"tokenId": str(token_id), "chainId": chain.chain_id}
    return _with_request(result, "position", echo)


@_d.method("trading.lp.positions")
async def _trading_lp_positions(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Every V4 position of the vault's wallets (or ``wallets``) on one or both chains.

    ``chainId`` (one) or ``chainIds`` (several); neither reads both chains.
    ``budgetSeconds`` bounds the read (default 25, 5-300); past it the card is partial.
    """
    from agentos.trading import lp

    p = _params(params)
    chain = _chain(p, required=False)
    raw_chains = p.get("chainIds")
    chains: list[ChainSpec] | None = [chain] if chain is not None else None
    if raw_chains is not None:
        if chain is not None:
            raise ValueError("pass params.chainId or params.chainIds, not both")
        if not isinstance(raw_chains, list) or not raw_chains:
            raise ValueError("params.chainIds must be a non-empty list of chain ids")
        chains = []
        for value in raw_chains:
            spec = _chain({"chainId": value})
            assert spec is not None
            if spec not in chains:
                chains.append(spec)
    raw_budget = p.get("budgetSeconds")
    if raw_budget is not None and (
        isinstance(raw_budget, bool) or not isinstance(raw_budget, int | float)
    ):
        raise ValueError("params.budgetSeconds must be a number of seconds")
    raw_wallets = p.get("wallets")
    if raw_wallets is None:
        wallets: list[str] = []
    elif isinstance(raw_wallets, list) and all(isinstance(w, str) for w in raw_wallets):
        wallets = [w for w in raw_wallets if w.strip()]
    else:
        raise ValueError("params.wallets must be a list of addresses")
    include_closed = p.get("all", False)
    if not isinstance(include_closed, bool):
        raise ValueError("params.all must be a boolean")
    service = _service(ctx)
    try:
        result = await lp.lp_positions(
            service,
            chains=chains,
            wallets=wallets,
            include_closed=include_closed,
            budget_s=float(raw_budget) if raw_budget is not None else None,
        )
    except Exception as exc:
        raise _raise(exc) from exc
    echo: dict[str, Any] = {
        "chainIds": [c.chain_id for c in chains] if chains else None,
        "wallets": wallets or None,
        "all": True if include_closed else None,
        "budgetSeconds": float(raw_budget) if raw_budget is not None else None,
    }
    return _with_request(result, "positions", echo)


# ── trading.markets (every pool a token trades in; docs/markets.md) ─────────


@_d.method("trading.markets")
async def _trading_markets(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Every pool a token trades in, on every DEX, split by side (docs/markets.md).

    Read-only, so an agent may call it. ``chainId`` defaults to Robinhood Chain.
    """
    from agentos.trading import markets

    p = _params(params)
    chain = _chain(p, required=False) or resolve_chain(markets.ROBINHOOD_CHAIN_ID)
    try:
        target = _str(p, "target") or _str(p, "token")
        if not target:
            raise ValueError("params.target (a token symbol, address or ETH) is required")
        side = (_str(p, "side") or "all").lower()
        if side not in markets.SIDES:
            raise ValueError("params.side must be all, quote or base")
        raw_tvl = p.get("minTvlUsd")
        if raw_tvl is None or raw_tvl == "":
            min_tvl = markets.DEFAULT_MIN_TVL_USD
        else:
            if isinstance(raw_tvl, bool) or not isinstance(raw_tvl, int | float | str):
                raise ValueError("params.minTvlUsd must be a number of dollars, 0 or more")
            try:
                min_tvl = float(raw_tvl)
            except ValueError as exc:
                raise ValueError("params.minTvlUsd must be a number of dollars, 0 or more") from exc
            if not min_tvl >= 0 or min_tvl == float("inf"):
                raise ValueError("params.minTvlUsd must be a number of dollars, 0 or more")
        limit = _int(p, "limit", markets.DEFAULT_LIMIT)
        if not 1 <= limit <= markets.MAX_LIMIT:
            raise ValueError(f"params.limit must be between 1 and {markets.MAX_LIMIT}")
        flags: dict[str, bool] = {}
        for key in ("lookalikes", "deep"):
            value = p.get(key, False)
            if value is None:
                value = False
            if not isinstance(value, bool):
                raise ValueError(f"params.{key} must be a boolean")
            flags[key] = value
    except ValueError as exc:
        raise _raise(exc) from exc
    service = _service(ctx)
    try:
        result = await markets.markets(
            service,
            chain=chain,
            target=target,
            side=side,
            min_tvl_usd=min_tvl,
            limit=limit,
            lookalikes=flags["lookalikes"],
            deep=flags["deep"],
        )
    except Exception as exc:
        raise _raise(exc) from exc
    token = result.get("token") if isinstance(result, dict) else None
    resolved = token.get("address") if isinstance(token, dict) else None
    echo = {
        "target": resolved or target,
        "chainId": chain.chain_id,
        "side": side,
        "minTvlUsd": min_tvl,
        "limit": limit,
        "lookalikes": flags["lookalikes"],
        "deep": flags["deep"],
    }
    return _with_request(result, "markets", echo)


# ── trading.lp.collect|remove|add (docs/lp-write.md) ─────────────────────────
#
# An agent may call these: each only creates an order, and every LP write parks
# as ``awaiting_approval`` whoever asked. Approving, rejecting and waiting stay
# on ``trading.orders.approve|reject|wait`` (approve/reject are operator-only).


def _lp_token_id(p: dict[str, Any], key: str, *, required: bool) -> int | None:
    value = p.get(key)
    if value is None or value == "":
        if required:
            raise ValueError(f"params.{key} is required")
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"params.{key} must be a positive integer")
    text = str(value).strip().lstrip("#")
    if not text.isdigit() or int(text) <= 0:
        raise ValueError(f"params.{key} must be a positive integer")
    return int(text)


def _decimal_text(p: dict[str, Any], key: str) -> str | None:
    value = p.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise ValueError(f"params.{key} must be a decimal string")
    return str(value).strip()


@_d.method("trading.lp.collect")
async def _trading_lp_collect(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Collect a V4 position's fees: creates an order that always awaits approval.

    A position with nothing uncollected is refused (``trading.lp.nothing_to_collect``)
    unless ``allowEmpty`` is true.
    """
    p = _params(params)
    chain = _chain(p)
    assert chain is not None
    token_id = _lp_token_id(p, "tokenId", required=True)
    assert token_id is not None
    allow_empty = p.get("allowEmpty", False)
    if not isinstance(allow_empty, bool):
        raise ValueError("params.allowEmpty must be a boolean")
    initiator, session_key = _initiator(ctx, p)
    service = _service(ctx)
    try:
        order = await service.lp_collect(
            chain=chain,
            token_id=token_id,
            allow_empty=allow_empty,
            initiator=initiator,  # type: ignore[arg-type]
            session_key=session_key,
            note=_note(p),
            **_client_order_id(service, "lp_collect", p),
        )
    except Exception as exc:
        raise _raise(exc) from exc
    return {"order": order}


@_d.method("trading.lp.remove")
async def _trading_lp_remove(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Remove ``pct`` % (default 100: burn) of a V4 position; always awaits approval."""
    p = _params(params)
    chain = _chain(p)
    assert chain is not None
    token_id = _lp_token_id(p, "tokenId", required=True)
    assert token_id is not None
    pct = _number(p, "pct")
    initiator, session_key = _initiator(ctx, p)
    service = _service(ctx)
    try:
        order = await service.lp_remove(
            chain=chain,
            token_id=token_id,
            pct=100.0 if pct is None else pct,
            slippage_pct=_number(p, "slippagePct"),
            initiator=initiator,  # type: ignore[arg-type]
            session_key=session_key,
            note=_note(p),
            **_client_order_id(service, "lp_remove", p),
        )
    except Exception as exc:
        raise _raise(exc) from exc
    return {"order": order}


@_d.method("trading.lp.add")
async def _trading_lp_add(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Mint a V4 position (or add to ``toPosition``) from a deposit; always awaits approval.

    ``token`` (or ``target``/``poolId``) names the pool -- a ``TOKEN/QUOTE``
    pair, or a token with ``quote``; ``feePct`` (``0.05``, ``0.3%``, ``500``)
    picks the fee tier; sized by ``usd`` or by ``amountBase`` and/or
    ``amountQuote``; ``range`` is ``mcap:LO-HI``, ``pct:N``, ``above[:N]``
    (all base token, from just above the price up N %), ``below[:N]`` (all
    quote token, from just below it down N %; N defaults to 20), ``full`` or
    ``ticks:LO:HI`` (default ``pct:20``).
    """
    from agentos.trading import lp

    p = _params(params)
    chain = _chain(p)
    assert chain is not None
    target = _str(p, "token") or _str(p, "target") or _str(p, "poolId")
    to_position = _lp_token_id(p, "toPosition", required=False)
    if not target and to_position is None:
        raise ValueError("params.token (a token or poolId) or params.toPosition is required")
    initiator, session_key = _initiator(ctx, p)
    service = _service(ctx)
    try:
        order = await service.lp_add(
            chain=chain,
            target=target,
            quote=_str(p, "quote"),
            fee=lp.parse_fee(p.get("feePct")),
            usd=_number(p, "usd"),
            amount_base=_decimal_text(p, "amountBase"),
            amount_quote=_decimal_text(p, "amountQuote"),
            range_spec=_str(p, "range"),
            to_position=to_position,
            wallet=_str(p, "wallet"),
            slippage_pct=_number(p, "slippagePct"),
            initiator=initiator,  # type: ignore[arg-type]
            session_key=session_key,
            note=_note(p),
            **_client_order_id(service, "lp_add", p),
        )
    except Exception as exc:
        raise _raise(exc) from exc
    return {"order": order}


# ── trading.dca.* (docs/dca.md) ─────────────────────────────────────────────
#
# An agent may create (it only proposes: an agent-bound connection always gets
# ``awaiting_approval``), get and list. Every other write is the user's, so it
# is ``@_operator_only``. A malformed param is ``trading.dca.invalid`` naming
# the field -- never the dispatcher's generic ``INVALID_REQUEST`` -- so a card,
# the CLI and an agent see one code for "change the input".

_DCA_MIN_EVERY_SECONDS = 60
#: ``trading.dca.update`` params → the engine's ``dca_update`` keywords.
_DCA_UPDATE_FIELDS = {
    "usdPerRun": "usd_per_run",
    "capUsd": "cap_usd",
    "runsMax": "runs_max",
    "everySeconds": "every_seconds",
    "maxPriceUsd": "max_price_usd",
    "name": "name",
}
_DCA_INT_FIELDS = frozenset({"runsMax", "everySeconds"})


def _dca_invalid(message: str) -> RpcHandlerError:
    return RpcHandlerError("trading.dca.invalid", message)


def _dca_id(p: dict[str, Any]) -> str:
    value = p.get("mandateId")
    if not isinstance(value, str) or not value.strip():
        raise _dca_invalid("params.mandateId is required")
    return value.strip()


def _dca_number(p: dict[str, Any], key: str) -> float | None:
    try:
        value = _number(p, key)
    except ValueError as exc:
        raise _dca_invalid(str(exc)) from exc
    if value is not None and (value != value or value in (float("inf"), float("-inf"))):
        raise _dca_invalid(f"params.{key} must be a finite number")
    return value


def _dca_int(p: dict[str, Any], key: str) -> int | None:
    """A whole number: an int, an integral float (``86400.0``) or a digit string."""
    value = p.get(key)
    if value is None or value == "":
        return None
    number = _dca_number(p, key)
    if number is None or number != int(number):
        raise _dca_invalid(f"params.{key} must be an integer")
    return int(number)


def _dca_bool(p: dict[str, Any], key: str, default: bool) -> bool:
    value = p.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise _dca_invalid(f"params.{key} must be a boolean")
    return value


def _dca_text(p: dict[str, Any], key: str) -> str | None:
    """A free-text field a person reads (name, reason): sanitised like a note."""
    try:
        return _note(p, key)
    except ValueError as exc:
        raise _dca_invalid(str(exc)) from exc


def _dca_str(p: dict[str, Any], key: str, *, required: bool = False) -> str | None:
    try:
        return _str(p, key, required=required)
    except ValueError as exc:
        raise _dca_invalid(str(exc)) from exc


def _dca_every(p: dict[str, Any], *, required: bool) -> int | None:
    every = _dca_int(p, "everySeconds")
    if every is None:
        if required:
            raise _dca_invalid("params.everySeconds is required")
        return None
    if every < _DCA_MIN_EVERY_SECONDS:
        raise _dca_invalid(f"params.everySeconds must be at least {_DCA_MIN_EVERY_SECONDS}")
    return every


@_d.method("trading.dca.create")
async def _trading_dca_create(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Create a DCA mandate. The operator's starts ``active``; an agent's awaits approval.

    ``initiator`` and ``sessionKey`` are decided by :func:`_initiator`: an
    agent-bound connection is the agent and files under its bound chat
    whatever it declares.
    """
    p = _params(params)
    try:
        chain = _chain(p)
        initiator, session_key = _initiator(ctx, p)
    except ValueError as exc:
        raise _dca_invalid(str(exc)) from exc
    assert chain is not None
    token = _dca_str(p, "token", required=True) or ""
    usd = _dca_number(p, "usdPerRun")
    if usd is None:
        raise _dca_invalid("params.usdPerRun is required")
    cap = _dca_number(p, "capUsd")
    runs = _dca_int(p, "runsMax")
    if cap is None and runs is None:
        raise _dca_invalid("params.capUsd or params.runsMax is required")
    every = _dca_every(p, required=True)
    assert every is not None
    service = _service(ctx)
    try:
        return await service.dca_create(
            chain=chain,
            token=token,
            quote=_dca_str(p, "quote"),
            usd_per_run=usd,
            cap_usd=cap,
            runs_max=runs,
            every_seconds=every,
            max_price_usd=_dca_number(p, "maxPriceUsd"),
            wallet=_dca_str(p, "wallet"),
            slippage_pct=_dca_number(p, "slippagePct"),
            name=_dca_text(p, "name"),
            start_now=_dca_bool(p, "startNow", True),
            initiator=initiator,
            session_key=session_key,
        )
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.dca.get")
async def _trading_dca_get(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """One mandate as a ``mandate`` card payload."""
    mandate_id = _dca_id(_params(params))
    service = _service(ctx)
    try:
        return await service.dca_get(mandate_id)
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.dca.list")
async def _trading_dca_list(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Live mandates (every one with ``all``), optionally of one ``wallet``."""
    p = _params(params)
    include_all = _dca_bool(p, "all", False)
    wallet = _dca_str(p, "wallet")
    service = _service(ctx)
    try:
        return await service.dca_list(all=include_all, wallet=wallet)
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.dca.approve")
@_operator_only
async def _trading_dca_approve(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Approve a pending mandate: it turns ``active`` (first buy on the next tick)."""
    mandate_id = _dca_id(_params(params))
    service = _service(ctx)
    try:
        return await service.dca_approve(mandate_id)
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.dca.reject")
@_operator_only
async def _trading_dca_reject(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    mandate_id = _dca_id(p)
    service = _service(ctx)
    try:
        return await service.dca_reject(mandate_id, _dca_text(p, "reason"))
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.dca.pause")
@_operator_only
async def _trading_dca_pause(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    mandate_id = _dca_id(_params(params))
    service = _service(ctx)
    try:
        return await service.dca_pause(mandate_id)
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.dca.resume")
@_operator_only
async def _trading_dca_resume(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    mandate_id = _dca_id(_params(params))
    service = _service(ctx)
    try:
        return await service.dca_resume(mandate_id)
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.dca.stop")
@_operator_only
async def _trading_dca_stop(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Stop for good; the mandate's buys still awaiting approval are rejected."""
    p = _params(params)
    mandate_id = _dca_id(p)
    service = _service(ctx)
    try:
        return await service.dca_stop(mandate_id, _dca_text(p, "reason"))
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.dca.run")
@_operator_only
async def _trading_dca_run(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Buy now: one run on an active or paused mandate; the schedule does not move."""
    p = _params(params)
    mandate_id = _dca_id(p)
    wait = _dca_bool(p, "wait", False)
    service = _service(ctx)
    try:
        return await service.dca_run_now(mandate_id, wait=wait)
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.dca.update")
@_operator_only
async def _trading_dca_update(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Change a mandate's terms; only the keys given are forwarded.

    ``runsMax: 0`` removes the run limit and ``maxPriceUsd: 0`` the price
    guard (the engine reads 0 that way); ``null`` means "not given".
    """
    p = _params(params)
    mandate_id = _dca_id(p)
    fields: dict[str, Any] = {}
    for key, field in _DCA_UPDATE_FIELDS.items():
        if p.get(key) is None:
            continue
        if key == "name":
            if not isinstance(p["name"], str):
                raise _dca_invalid("params.name must be a string")
            fields[field] = _dca_text(p, "name") or ""
        elif key == "everySeconds":
            fields[field] = _dca_every(p, required=False)
        elif key in _DCA_INT_FIELDS:
            fields[field] = _dca_int(p, key)
        else:
            fields[field] = _dca_number(p, key)
    if not fields:
        raise _dca_invalid(
            "nothing to update: pass one of " + ", ".join(f"params.{k}" for k in _DCA_UPDATE_FIELDS)
        )
    service = _service(ctx)
    try:
        return await service.dca_update(mandate_id, **fields)
    except Exception as exc:
        raise _raise(exc) from exc


# ── trading.trigger.* (docs/triggers.md) ────────────────────────────────────
#
# Same split as ``trading.dca.*``: an agent may create (it only proposes: an
# agent-bound connection always gets ``awaiting_approval``), get and list; every
# other write is the user's, so it is ``@_operator_only``. A malformed param is
# ``trading.trigger.invalid`` naming the field. The structural rules (which
# condition needs which number, which action takes which size) are checked
# here; the engine checks the values against the market and the wallet.

_TRIGGER_KINDS = ("sell", "buy", "alert")
_TRIGGER_DIRECTIONS = ("below", "above", "trail")
_TRIGGER_SIZES = ("amountUsd", "amountPct", "amount")
_TRIGGER_MIN_VALID_SECONDS = 60


def _trigger_invalid(message: str) -> RpcHandlerError:
    return RpcHandlerError("trading.trigger.invalid", message)


def _trigger_id(p: dict[str, Any]) -> str:
    value = p.get("triggerId")
    if not isinstance(value, str) or not value.strip():
        raise _trigger_invalid("params.triggerId is required")
    return value.strip()


def _trigger_number(p: dict[str, Any], key: str) -> float | None:
    try:
        value = _number(p, key)
    except ValueError as exc:
        raise _trigger_invalid(str(exc)) from exc
    if value is not None and (value != value or value in (float("inf"), float("-inf"))):
        raise _trigger_invalid(f"params.{key} must be a finite number")
    return value


def _trigger_int(p: dict[str, Any], key: str) -> int | None:
    """A whole number: an int, an integral float (``3600.0``) or a digit string."""
    value = p.get(key)
    if value is None or value == "":
        return None
    number = _trigger_number(p, key)
    if number is None or number != int(number):
        raise _trigger_invalid(f"params.{key} must be an integer")
    return int(number)


def _trigger_bool(p: dict[str, Any], key: str, default: bool) -> bool:
    value = p.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise _trigger_invalid(f"params.{key} must be a boolean")
    return value


def _trigger_text(p: dict[str, Any], key: str) -> str | None:
    """A free-text field a person reads (name, reason): sanitised like a note."""
    try:
        return _note(p, key)
    except ValueError as exc:
        raise _trigger_invalid(str(exc)) from exc


def _trigger_str(p: dict[str, Any], key: str, *, required: bool = False) -> str | None:
    try:
        return _str(p, key, required=required)
    except ValueError as exc:
        raise _trigger_invalid(str(exc)) from exc


def _trigger_choice(p: dict[str, Any], key: str, choices: tuple[str, ...]) -> str:
    value = (_trigger_str(p, key, required=True) or "").lower()
    if value not in choices:
        raise _trigger_invalid(f"params.{key} must be one of {', '.join(choices)}")
    return value


def _trigger_price(p: dict[str, Any]) -> str | float | None:
    """``price`` as given: ``"3800"``, ``"-10%"``, ``"+15%"`` or a number.

    The engine resolves a percent against the price at creation, so the text
    goes through untouched (stripped); a number must be finite.
    """
    value = p.get("price")
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _trigger_invalid("params.price must be a string or a number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise _trigger_invalid("params.price must be a finite number")
    return number


def _trigger_amount(p: dict[str, Any]) -> str | None:
    """``amount`` (token units, human): a string or a number, passed on as a string."""
    value = p.get("amount")
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _trigger_invalid("params.amount must be a string or a number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise _trigger_invalid("params.amount must be a finite number")
    # ``1e-05`` would reach the engine as an exponent; give it plain digits.
    return format(Decimal(str(value)), "f")


def _trigger_valid_for(p: dict[str, Any]) -> int | None:
    seconds = _trigger_int(p, "validForSeconds")
    if seconds is not None and seconds < _TRIGGER_MIN_VALID_SECONDS:
        raise _trigger_invalid(
            f"params.validForSeconds must be at least {_TRIGGER_MIN_VALID_SECONDS}"
        )
    return seconds


@_d.method("trading.trigger.create")
async def _trading_trigger_create(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Create a price trigger. The operator's is ``armed`` at once; an agent's awaits approval.

    ``initiator`` and ``sessionKey`` are decided by :func:`_initiator`: an
    agent-bound connection is the agent and files under its bound chat
    whatever it declares.
    """
    p = _params(params)
    try:
        chain = _chain(p)
        initiator, session_key = _initiator(ctx, p)
    except ValueError as exc:
        raise _trigger_invalid(str(exc)) from exc
    assert chain is not None
    kind = _trigger_choice(p, "kind", _TRIGGER_KINDS)
    token = _trigger_str(p, "token", required=True) or ""
    direction = _trigger_choice(p, "direction", _TRIGGER_DIRECTIONS)
    price = _trigger_price(p)
    trail_pct = _trigger_number(p, "trailPct")
    if direction == "trail":
        if kind == "buy":
            raise _trigger_invalid("params.kind buy cannot use direction trail")
        if trail_pct is None:
            raise _trigger_invalid("params.trailPct is required for direction trail")
        if price is not None:
            raise _trigger_invalid("params.price is not used with direction trail")
    else:
        if price is None:
            raise _trigger_invalid(f"params.price is required for direction {direction}")
        if trail_pct is not None:
            raise _trigger_invalid(f"params.trailPct is not used with direction {direction}")
    amount_usd = _trigger_number(p, "amountUsd")
    amount_pct = _trigger_number(p, "amountPct")
    amount = _trigger_amount(p)
    given = [
        key
        for key, value in zip(_TRIGGER_SIZES, (amount_usd, amount_pct, amount), strict=True)
        if value is not None
    ]
    if kind == "alert" and given:
        raise _trigger_invalid(f"params.{given[0]} is not used by kind alert: it sends no order")
    if kind == "buy" and (amount_usd is None or len(given) > 1):
        raise _trigger_invalid("params.amountUsd is required for kind buy, and is its only size")
    if kind == "sell" and len(given) != 1:
        raise _trigger_invalid(
            "kind sell takes exactly one size: params.amountPct, params.amount or params.amountUsd"
        )
    service = _service(ctx)
    try:
        return await service.trigger_create(
            chain=chain,
            kind=kind,
            token=token,
            quote=_trigger_str(p, "quote"),
            direction=direction,
            price=price,
            trail_pct=trail_pct,
            amount_usd=amount_usd,
            amount_pct=amount_pct,
            amount=amount,
            wallet=_trigger_str(p, "wallet"),
            slippage_pct=_trigger_number(p, "slippagePct"),
            name=_trigger_text(p, "name"),
            valid_for_seconds=_trigger_valid_for(p),
            initiator=initiator,
            session_key=session_key,
        )
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.trigger.get")
async def _trading_trigger_get(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """One trigger as a ``trigger`` card payload."""
    trigger_id = _trigger_id(_params(params))
    service = _service(ctx)
    try:
        return await service.trigger_get(trigger_id)
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.trigger.list")
async def _trading_trigger_list(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Live triggers (every one with ``all``), optionally of one ``wallet``."""
    p = _params(params)
    include_all = _trigger_bool(p, "all", False)
    wallet = _trigger_str(p, "wallet")
    service = _service(ctx)
    try:
        return await service.trigger_list(all=include_all, wallet=wallet)
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.trigger.approve")
@_operator_only
async def _trading_trigger_approve(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Approve a pending trigger: it is ``armed`` and checked from the next tick."""
    trigger_id = _trigger_id(_params(params))
    service = _service(ctx)
    try:
        return await service.trigger_approve(trigger_id)
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.trigger.reject")
@_operator_only
async def _trading_trigger_reject(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    trigger_id = _trigger_id(p)
    service = _service(ctx)
    try:
        return await service.trigger_reject(trigger_id, _trigger_text(p, "reason"))
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.trigger.pause")
@_operator_only
async def _trading_trigger_pause(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    trigger_id = _trigger_id(_params(params))
    service = _service(ctx)
    try:
        return await service.trigger_pause(trigger_id)
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.trigger.resume")
@_operator_only
async def _trading_trigger_resume(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    trigger_id = _trigger_id(_params(params))
    service = _service(ctx)
    try:
        return await service.trigger_resume(trigger_id)
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.trigger.stop")
@_operator_only
async def _trading_trigger_stop(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Stop for good; an order of the trigger still awaiting approval is rejected."""
    p = _params(params)
    trigger_id = _trigger_id(p)
    service = _service(ctx)
    try:
        return await service.trigger_stop(trigger_id, _trigger_text(p, "reason"))
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.trigger.fire")
@_operator_only
async def _trading_trigger_fire(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Fire now: act at once on an armed or paused trigger, whatever the price."""
    p = _params(params)
    trigger_id = _trigger_id(p)
    wait = _trigger_bool(p, "wait", False)
    service = _service(ctx)
    try:
        return await service.trigger_fire_now(trigger_id, wait=wait)
    except Exception as exc:
        raise _raise(exc) from exc


# ── trading.bracket.* (docs/brackets.md) ────────────────────────────────────
#
# A bracket is a take-profit and a stop-loss on one position, one cancelling
# the other. Same split as ``trading.trigger.*``: an agent may create (it only
# proposes, both legs ``awaiting_approval``), get and list; every other write
# is the user's (``@_operator_only``). A malformed param is
# ``trading.bracket.invalid`` naming the field. The structural rules
# (``takeProfit`` required, exactly one of ``stopLoss`` / ``trailPct``, one
# size at most, ``tpPct`` only with ``amountPct``, no size on an alert) are
# checked here; the lines against the market and the wallet in the engine.

_BRACKET_KINDS = ("sell", "alert")
_BRACKET_LEGS = ("tp", "sl")
_BRACKET_SIZES = ("amountPct", "amount", "amountUsd")


def _bracket_invalid(message: str) -> RpcHandlerError:
    return RpcHandlerError("trading.bracket.invalid", message)


@contextlib.contextmanager
def _bracket_errors() -> Iterator[None]:
    """Re-code the shared ``_trigger_*`` helpers' refusals as ``trading.bracket.invalid``.

    The field parsing is the trigger's (same types, same wording); only the
    error code is the bracket's.
    """
    try:
        yield
    except RpcHandlerError as exc:
        if exc.code != "trading.trigger.invalid":
            raise
        raise _bracket_invalid(str(exc)) from exc
    except ValueError as exc:
        raise _bracket_invalid(str(exc)) from exc


def _bracket_id(p: dict[str, Any]) -> str:
    value = p.get("bracketId")
    if not isinstance(value, str) or not value.strip():
        raise _bracket_invalid("params.bracketId is required")
    return value.strip()


def _bracket_number(p: dict[str, Any], key: str) -> float | None:
    with _bracket_errors():
        return _trigger_number(p, key)


def _bracket_bool(p: dict[str, Any], key: str, default: bool) -> bool:
    with _bracket_errors():
        return _trigger_bool(p, key, default)


def _bracket_text(p: dict[str, Any], key: str) -> str | None:
    with _bracket_errors():
        return _trigger_text(p, key)


def _bracket_str(p: dict[str, Any], key: str, *, required: bool = False) -> str | None:
    with _bracket_errors():
        return _trigger_str(p, key, required=required)


def _bracket_choice(
    p: dict[str, Any], key: str, choices: tuple[str, ...], default: str | None = None
) -> str | None:
    """One of ``choices`` (case-insensitive); ``default`` when the param is absent."""
    value = _bracket_str(p, key)
    if value is None:
        return default
    value = value.lower()
    if value not in choices:
        raise _bracket_invalid(f"params.{key} must be one of {', '.join(choices)}")
    return value


def _bracket_line(p: dict[str, Any], key: str) -> str | float | None:
    """``takeProfit`` / ``stopLoss`` as given: ``"4560"``, ``"+20%"``, ``"-10%"`` or a number.

    The engine resolves a percent against the price at creation, so the text
    goes through untouched (stripped); a number must be finite.
    """
    value = p.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _bracket_invalid(f"params.{key} must be a string or a number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise _bracket_invalid(f"params.{key} must be a finite number")
    return number


def _bracket_amount(p: dict[str, Any]) -> str | None:
    with _bracket_errors():
        return _trigger_amount(p)


def _bracket_valid_for(p: dict[str, Any]) -> int | None:
    with _bracket_errors():
        return _trigger_valid_for(p)


@_d.method("trading.bracket.create")
async def _trading_bracket_create(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Create a bracket: two legs, one cancelling the other.

    The operator's legs are ``armed`` at once; an agent's both await one
    approval. ``initiator`` and ``sessionKey`` are decided by
    :func:`_initiator`, as for a trigger.
    """
    p = _params(params)
    with _bracket_errors():
        chain = _chain(p)
        initiator, session_key = _initiator(ctx, p)
    assert chain is not None
    kind = _bracket_choice(p, "kind", _BRACKET_KINDS, "sell") or "sell"
    token = _bracket_str(p, "token", required=True) or ""
    take_profit = _bracket_line(p, "takeProfit")
    if take_profit is None:
        raise _bracket_invalid("params.takeProfit is required")
    stop_loss = _bracket_line(p, "stopLoss")
    trail_pct = _bracket_number(p, "trailPct")
    if stop_loss is None and trail_pct is None:
        raise _bracket_invalid("one of params.stopLoss or params.trailPct is required")
    if stop_loss is not None and trail_pct is not None:
        raise _bracket_invalid("params.stopLoss and params.trailPct exclude each other: pass one")
    amount_pct = _bracket_number(p, "amountPct")
    amount = _bracket_amount(p)
    amount_usd = _bracket_number(p, "amountUsd")
    tp_pct = _bracket_number(p, "tpPct")
    given = [
        key
        for key, value in zip(_BRACKET_SIZES, (amount_pct, amount, amount_usd), strict=True)
        if value is not None
    ]
    if kind == "alert":
        if given or tp_pct is not None:
            field = given[0] if given else "tpPct"
            raise _bracket_invalid(f"params.{field} is not used by kind alert: it sends no order")
    if len(given) > 1:
        raise _bracket_invalid(
            "a bracket takes one size at most: params.amountPct, params.amount or "
            f"params.amountUsd (got {', '.join(given)})"
        )
    if tp_pct is not None and given and given[0] != "amountPct":
        raise _bracket_invalid(
            f"params.tpPct works with params.amountPct only (got params.{given[0]})"
        )
    service = _service(ctx)
    try:
        return await service.bracket_create(
            chain=chain,
            kind=kind,
            token=token,
            quote=_bracket_str(p, "quote"),
            take_profit=take_profit,
            stop_loss=stop_loss,
            trail_pct=trail_pct,
            amount_usd=amount_usd,
            amount_pct=amount_pct,
            amount=amount,
            tp_pct=tp_pct,
            wallet=_bracket_str(p, "wallet"),
            slippage_pct=_bracket_number(p, "slippagePct"),
            name=_bracket_text(p, "name"),
            valid_for_seconds=_bracket_valid_for(p),
            initiator=initiator,
            session_key=session_key,
        )
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.bracket.get")
async def _trading_bracket_get(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """One bracket (both legs) as a ``bracket`` card payload."""
    bracket_id = _bracket_id(_params(params))
    service = _service(ctx)
    try:
        return await service.bracket_get(bracket_id)
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.bracket.list")
async def _trading_bracket_list(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Live brackets (every one with ``all``), optionally of one ``wallet``."""
    p = _params(params)
    include_all = _bracket_bool(p, "all", False)
    wallet = _bracket_str(p, "wallet")
    service = _service(ctx)
    try:
        return await service.bracket_list(all=include_all, wallet=wallet)
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.bracket.approve")
@_operator_only
async def _trading_bracket_approve(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Approve a proposed bracket: both legs are ``armed`` and checked from the next tick."""
    bracket_id = _bracket_id(_params(params))
    service = _service(ctx)
    try:
        return await service.bracket_approve(bracket_id)
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.bracket.reject")
@_operator_only
async def _trading_bracket_reject(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    bracket_id = _bracket_id(p)
    service = _service(ctx)
    try:
        return await service.bracket_reject(bracket_id, _bracket_text(p, "reason"))
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.bracket.pause")
@_operator_only
async def _trading_bracket_pause(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    bracket_id = _bracket_id(_params(params))
    service = _service(ctx)
    try:
        return await service.bracket_pause(bracket_id)
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.bracket.resume")
@_operator_only
async def _trading_bracket_resume(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    bracket_id = _bracket_id(_params(params))
    service = _service(ctx)
    try:
        return await service.bracket_resume(bracket_id)
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.bracket.stop")
@_operator_only
async def _trading_bracket_stop(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Stop both legs for good; a parked order of either is rejected."""
    p = _params(params)
    bracket_id = _bracket_id(p)
    service = _service(ctx)
    try:
        return await service.bracket_stop(bracket_id, _bracket_text(p, "reason"))
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.bracket.fire")
@_operator_only
async def _trading_bracket_fire(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Fire one leg now (``leg``, else the nearest), whatever the price; the other goes on hold."""
    p = _params(params)
    bracket_id = _bracket_id(p)
    leg = _bracket_choice(p, "leg", _BRACKET_LEGS)
    wait = _bracket_bool(p, "wait", False)
    service = _service(ctx)
    try:
        return await service.bracket_fire_now(bracket_id, leg=leg, wait=wait)
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.network")
async def _trading_network(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Head block, block age, gas and RPC latency per chain (cached for a few seconds)."""
    p = _params(params)
    service = _service(ctx)
    try:
        return await service.network(fresh=bool(p.get("fresh")))
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.unwrap")
@_operator_only
async def _trading_unwrap(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Unwrap WETH held by a wallet. It moves funds, so it is the user's action."""
    p = _params(params)
    chain = _chain(p)
    assert chain is not None
    service = _service(ctx)
    try:
        return await service.unwrap(chain, _str(p, "wallet"), _str(p, "amount"))
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.orders.list")
async def _trading_orders_list(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    service = _service(ctx)
    try:
        return service.list_orders(
            status=_str(p, "status"),
            wallet=_str(p, "wallet"),
            limit=_int(p, "limit", 50),
            kind=_str(p, "kind"),
        )
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.orders.get")
async def _trading_orders_get(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    order_id = _str(p, "orderId", required=True) or ""
    service = _service(ctx)
    try:
        return {"order": service.get_order(order_id)}
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.orders.wait")
async def _trading_orders_wait(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    order_id = _str(p, "orderId", required=True) or ""
    timeout = _number(p, "timeoutSeconds")
    service = _service(ctx)
    try:
        order = await service.wait_order(order_id, timeout if timeout is not None else 60.0)
    except Exception as exc:
        raise _raise(exc) from exc
    return {"order": order}


@_d.method("trading.orders.approve")
@_operator_only
async def _trading_orders_approve(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    order_id = _str(p, "orderId", required=True) or ""
    service = _service(ctx)
    try:
        return {"order": await service.approve(order_id, wait=bool(p.get("wait")))}
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.orders.reject")
@_operator_only
async def _trading_orders_reject(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    order_id = _str(p, "orderId", required=True) or ""
    service = _service(ctx)
    try:
        return {"order": await service.reject(order_id, _str(p, "reason"))}
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.history")
async def _trading_history(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    chain = _chain(p, required=False)
    before = _number(p, "before")
    service = _service(ctx)
    try:
        return service.history(
            wallet=_str(p, "wallet"),
            chain_id=chain.chain_id if chain else None,
            kind=_str(p, "kind"),
            limit=_int(p, "limit", 100),
            before=before / 1000.0 if before is not None and before > 10**11 else before,
            include_hidden=bool(p.get("includeHidden")),
        )
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.portfolio")
async def _trading_portfolio(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    service = _service(ctx)
    service.ensure_unlocked()
    try:
        return await service.portfolio(
            _str(p, "wallet"), include_hidden=bool(p.get("includeHidden"))
        )
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.chart")
async def _trading_chart(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    chain = _chain(p)
    assert chain is not None
    token = _str(p, "token", required=True) or ""
    range_key = _str(p, "range") or DEFAULT_CHART_RANGE
    service = _service(ctx)
    try:
        return await service.chart(chain, token, range_key)
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.sync")
async def _trading_sync(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Re-read the chain into the ledger. ``full`` drops and rebuilds the ledger
    (every chain, from the wallet's first block), so an agent may not ask for it."""
    p = _params(params)
    full = bool(p.get("full"))
    if full:
        _require_operator(ctx, "trading.sync(full)")
    service = _service(ctx)
    service.ensure_unlocked()
    try:
        service.request_sync(wallet=_str(p, "wallet"), full=full)
    except Exception as exc:
        raise _raise(exc) from exc
    return {"started": True}


@_d.method("trading.limits")
async def _trading_limits(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    service = _service(ctx)
    try:
        return service.limits(_str(p, "wallet"))
    except Exception as exc:
        raise _raise(exc) from exc


@_d.method("trading.lot.setCost")
@_operator_only
async def _trading_lot_set_cost(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    entry_id = _int(p, "entryId", 0)
    cost = _number(p, "costUsdPerToken")
    if entry_id <= 0:
        raise ValueError("params.entryId is required")
    if cost is None or cost < 0:
        raise ValueError("params.costUsdPerToken must be a non-negative number")
    service = _service(ctx)
    try:
        entry = service.set_lot_cost(entry_id, cost)
    except Exception as exc:
        raise _raise(exc) from exc
    await service._emit("trading.changed", {"reason": "sync"})
    return {"entry": entry}
