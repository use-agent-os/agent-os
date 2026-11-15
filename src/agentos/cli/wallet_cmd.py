"""``agentos wallet`` — the engine's wallet vault, from the terminal.

A thin client over the gateway's ``wallet.*`` RPCs (the vault itself lives in
``agentos.trading``): keystores stay in the gateway process, and this command
only ever sends the vault password over the loopback RPC when a call needs it.
Secrets are read from a hidden prompt or ``AGENTOS_WALLET_PASSWORD`` and are
never echoed, logged, or written anywhere but where ``export --out`` says.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from agentos.cli.gateway_rpc import run_gateway_sync
from agentos.cli.output import emit_error, print_json
from agentos.cli.ui import ACCENT, ACCENT_HEADER, console, markup_escape

app = typer.Typer(help="Manage the trading wallet vault (create, import, export, unlock).")

PASSWORD_ENV = "AGENTOS_WALLET_PASSWORD"
KEYSTORE_PASSWORD_ENV = "AGENTOS_KEYSTORE_PASSWORD"

CHAIN_IDS: dict[str, int] = {"base": 8453, "robinhood": 4663}
CHAIN_KEYS: dict[int, str] = {chain_id: key for key, chain_id in CHAIN_IDS.items()}
NATIVE_ADDRESS = "0x0000000000000000000000000000000000000000"

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


# ── shared helpers (also used by ``agentos trade``) ─────────────────────────


def chain_id_from_arg(value: str) -> int:
    """Map ``base``/``robinhood`` (or a numeric id) to a chain id."""

    key = value.strip().lower()
    if key in CHAIN_IDS:
        return CHAIN_IDS[key]
    if key.isdigit() and int(key) in CHAIN_KEYS:
        return int(key)
    choices = ", ".join(sorted(CHAIN_IDS))
    raise typer.BadParameter(f"unknown chain {value!r}; expected one of: {choices}")


def chain_label(chain_id: Any) -> str:
    try:
        return CHAIN_KEYS.get(int(chain_id), str(chain_id))
    except (TypeError, ValueError):
        return str(chain_id)


def is_address(value: str) -> bool:
    return bool(_ADDRESS_RE.match(value.strip()))


def short_address(address: Any) -> str:
    text = str(address or "")
    if len(text) <= 12:
        return text
    return f"{text[:6]}…{text[-4:]}"


def read_password(
    prompt: str,
    *,
    env: str = PASSWORD_ENV,
    confirm: bool = False,
    json_output: bool = False,
) -> str:
    """Read a secret from ``env`` or a hidden prompt; never from argv."""

    value = os.environ.get(env, "")
    if value:
        return value
    if json_output and not sys.stdin.isatty():
        emit_error(
            f"password required; set {env} for non-interactive use",
            json_output=True,
            code="PASSWORD_REQUIRED",
        )
        raise typer.Exit(2)
    entered = typer.prompt(prompt, hide_input=True, confirmation_prompt=confirm)
    if not isinstance(entered, str) or not entered:
        raise typer.BadParameter("password cannot be empty")
    return entered


def money(value: Any) -> str:
    if value is None or value == "":
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if number < 0 else ""
    return f"{sign}${abs(number):,.2f}"


def percent(value: Any) -> str:
    if value is None or value == "":
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "+" if number > 0 else ""
    return f"{sign}{number:.2f}%"


def amount_text(value: Any) -> str:
    if value is None or value == "":
        return "—"
    return str(value)


def token_symbol(token: Any) -> str:
    if isinstance(token, dict):
        return str(token.get("symbol") or short_address(token.get("address")))
    return str(token or "—")


def _restrict_windows_acl(path: Path) -> None:
    """Owner-only DACL on a Windows file: no inherited entries, the user alone.

    ``os.open``'s mode only sets the read-only bit there; the file would
    otherwise inherit whatever the directory grants (``Users``, ``Everyone``
    on a shared drive). Raises ``OSError`` when the ACL cannot be set.
    """
    import getpass
    import subprocess

    user = getpass.getuser()
    domain = os.environ.get("USERDOMAIN", "")
    principal = f"{domain}\\{user}" if domain else user
    completed = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{principal}:F"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise OSError(f"icacls could not restrict {path}: {completed.stderr.strip()}")


def write_private_file(path: Path, text: str) -> bool:
    """Create *path* owner-only from its first byte and write *text* to it.

    ``O_CREAT | O_EXCL`` never reuses an existing file -- not a world-readable
    one left from an earlier export, not one another process already holds
    open, not a symlink planted at the name -- and raises ``FileExistsError``
    instead. The mode is ``0600`` at creation, so the secret never sits in a
    file the umask made readable. On Windows the ACL is cut to the current
    user while the file is still empty. Returns whether the result is
    verifiably owner-only (a POSIX filesystem that ignores modes says no).
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if os.name == "nt":
            _restrict_windows_acl(path)
        # newline="": the bytes the gateway returned are the bytes on disk.
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fd = -1
            handle.write(text)
    except BaseException:
        if fd != -1:
            os.close(fd)
        path.unlink(missing_ok=True)
        raise
    if os.name == "nt":
        return True
    info = os.stat(path)
    return (info.st_mode & 0o077) == 0 and info.st_uid == os.geteuid()


def _refuse_existing_export(out: Path, *, json_output: bool) -> None:
    emit_error(
        f"{out} already exists; export writes a new file and never overwrites one",
        json_output=json_output,
        code="FILE_EXISTS",
    )
    raise typer.Exit(1)


def _wallet_row(result: Any) -> dict[str, Any]:
    wallet = result.get("wallet") if isinstance(result, dict) else None
    return wallet if isinstance(wallet, dict) else {}


def _print_wallet(wallet: dict[str, Any], verb: str) -> None:
    label = markup_escape(str(wallet.get("label") or ""))
    console.print(f"{verb} wallet [{ACCENT}]{label}[/] {wallet.get('address') or ''}")


# ── commands ────────────────────────────────────────────────────────────────


@app.command("status")
def wallet_status(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show whether the vault exists, is unlocked, and how it unlocks."""

    async def _run(client):
        return await client.call("wallet.status", {})

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    result = result if isinstance(result, dict) else {}
    table = Table(title="Wallet vault", show_header=False)
    table.add_column("Field", style=ACCENT)
    table.add_column("Value")
    table.add_row("initialized", "yes" if result.get("initialized") else "no")
    table.add_row("unlocked", "yes" if result.get("unlocked") else "no")
    table.add_row("unlock mode", str(result.get("unlockMode") or "—"))
    stored = result.get("storedUnlockMode")
    if stored and stored != result.get("unlockMode"):
        note = " (stored password unused; `agentos wallet mode manual` deletes it)"
        table.add_row("vault records", f"{stored}{note if result.get('unlockKeyPresent') else ''}")
    table.add_row("wallets", str(result.get("walletCount") or 0))
    table.add_row("primary", str(result.get("primary") or "—"))
    table.add_row("vault", markup_escape(str(result.get("vaultPath") or "—")))
    console.print(table)


def _unlock_mode_arg(mode: str | None, option: str) -> str | None:
    if mode is None:
        return None
    mode = mode.strip().lower()
    if mode not in {"auto", "manual"}:
        raise typer.BadParameter(f"{option} must be 'auto' or 'manual'")
    return mode


def _explain_auto_unlock() -> None:
    console.print(
        "The password is kept in ~/.agentos/wallets/unlock.key so the agent can sign "
        "unattended; anyone who can read that file can spend from these wallets."
    )


@app.command("setup")
def wallet_setup(
    mode: str | None = typer.Option(
        None,
        "--mode",
        help="auto = password kept (encrypted) for unattended signing, manual = unlock "
        "per gateway session; default: the [trading] unlock_mode setting",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Create the vault with a new password (first run only)."""

    requested = _unlock_mode_arg(mode, "--mode")
    password = read_password("Vault password", confirm=True, json_output=json_output)
    params: dict[str, Any] = {"password": password}
    if requested is not None:
        params["unlockMode"] = requested

    async def _run(client):
        return await client.call("wallet.setup", params)

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    # The gateway decides the mode (a manual [trading] unlock_mode wins): print
    # what it reports, not what was asked for.
    actual = str((result or {}).get("unlockMode") or requested or "auto")
    console.print(f"Vault created ([{ACCENT}]{actual}[/] unlock).")
    if actual == "auto":
        _explain_auto_unlock()


@app.command("mode")
def wallet_mode(
    mode: str = typer.Argument(..., help="auto or manual"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Switch how the vault unlocks; `manual` deletes the stored password."""

    target = _unlock_mode_arg(mode, "MODE")
    password = read_password("Vault password", json_output=json_output)

    async def _run(client):
        return await client.call("wallet.setUnlockMode", {"mode": target, "password": password})

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    console.print(f"Vault unlock mode is now [{ACCENT}]{target}[/].")
    if target == "manual":
        console.print(
            "The stored password was deleted; unlock with `agentos wallet unlock` "
            "after each gateway start."
        )
    else:
        _explain_auto_unlock()


@app.command("unlock")
def wallet_unlock(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Unlock the vault for this gateway session."""

    password = read_password("Vault password", json_output=json_output)

    async def _run(client):
        return await client.call("wallet.unlock", {"password": password})

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    console.print("Vault unlocked.")


@app.command("lock")
def wallet_lock(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Lock the vault (drops keys from gateway memory)."""

    async def _run(client):
        return await client.call("wallet.lock", {})

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    console.print("Vault locked.")


@app.command("list")
def wallet_list(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List wallets; the primary one is the default for swaps."""

    async def _run(client):
        return await client.call("wallet.list", {})

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    rows = result.get("wallets", []) if isinstance(result, dict) else []
    table = Table(title="Wallets", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("")
    table.add_column("Label")
    table.add_column("Address")
    table.add_column("Chains")
    for row in rows:
        if not isinstance(row, dict):
            continue
        chains = ", ".join(chain_label(c) for c in row.get("chains", []) or [])
        table.add_row(
            "★" if row.get("primary") else "",
            markup_escape(str(row.get("label") or "")),
            str(row.get("address") or ""),
            chains,
        )
    console.print(table)
    if not rows:
        console.print("No wallets yet. Create one with: agentos wallet create --label main")


@app.command("create")
def wallet_create(
    label: str = typer.Option(..., "--label", help="Display name for the new wallet"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Create a fresh wallet inside the vault."""

    async def _run(client):
        return await client.call("wallet.create", {"label": label})

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    _print_wallet(_wallet_row(result), "Created")


@app.command("import")
def wallet_import(
    label: str = typer.Option(..., "--label", help="Display name for the imported wallet"),
    private_key_stdin: bool = typer.Option(
        False, "--private-key-stdin", help="Read a raw hex private key from stdin"
    ),
    keystore: Path | None = typer.Option(
        None, "--keystore", help="Import an encrypted keystore v3 JSON file"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Import an existing wallet from a private key (stdin) or a keystore file."""

    if private_key_stdin == (keystore is not None):
        raise typer.BadParameter("Use exactly one of --private-key-stdin or --keystore FILE")

    params: dict[str, Any] = {"label": label}
    if private_key_stdin:
        raw = sys.stdin.read().strip()
        if not raw:
            raise typer.BadParameter("no private key on stdin")
        params["privateKey"] = raw
    else:
        assert keystore is not None
        try:
            text = keystore.read_text(encoding="utf-8")
        except OSError as exc:
            raise typer.BadParameter(f"Cannot read {keystore}: {exc}") from exc
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"{keystore} is not JSON: {exc}") from exc
        params["keystoreJson"] = text
        params["keystorePassword"] = read_password(
            "Keystore password", env=KEYSTORE_PASSWORD_ENV, json_output=json_output
        )

    async def _run(client):
        return await client.call("wallet.import", params)

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    _print_wallet(_wallet_row(result), "Imported")


@app.command("export")
def wallet_export(
    address: str = typer.Argument(..., help="Wallet address to export"),
    keystore: bool = typer.Option(False, "--keystore", help="Export the encrypted keystore JSON"),
    private_key: bool = typer.Option(False, "--private-key", help="Export the raw private key"),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="Write to this new file, owner-only (mode 0600); never overwrites",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Export a wallet. Always asks for the vault password."""

    if keystore == private_key:
        raise typer.BadParameter("Use exactly one of --keystore or --private-key")
    fmt = "keystore" if keystore else "privateKey"
    password = read_password("Vault password", json_output=json_output)

    async def _run(client):
        return await client.call(
            "wallet.export", {"address": address, "password": password, "format": fmt}
        )

    if out is not None and os.path.lexists(out):
        # Checked before the password goes anywhere; the write below refuses
        # too (O_EXCL), this only spares the user a round trip.
        _refuse_existing_export(out, json_output=json_output)

    result = run_gateway_sync(_run, json_output=json_output)
    result = result if isinstance(result, dict) else {}
    secret = result.get("keystoreJson") if keystore else result.get("privateKey")
    if out is not None:
        try:
            private = write_private_file(out, str(secret or ""))
        except FileExistsError:
            _refuse_existing_export(out, json_output=json_output)
        except OSError as exc:
            emit_error(
                f"cannot write {out}: {exc.strerror or exc}",
                json_output=json_output,
                code="WRITE_FAILED",
            )
            raise typer.Exit(1) from None
        if json_output:
            print_json({"written": str(out), "format": fmt, "private": private})
        elif private:
            note = "owner-only" if os.name == "nt" else "mode 0600"
            console.print(f"Wrote {fmt} to [{ACCENT}]{markup_escape(str(out))}[/] ({note}).")
        else:
            console.print(
                f"Wrote {fmt} to [{ACCENT}]{markup_escape(str(out))}[/], but [red]could not "
                "make it owner-only[/] on this filesystem: move it somewhere private."
            )
        return
    if json_output:
        print_json(result)
        return
    if private_key:
        console.print(
            "[red]This is the raw private key. Anyone who sees it controls the wallet.[/]"
        )
    typer.echo(str(secret or ""))


@app.command("rename")
def wallet_rename(
    address: str = typer.Argument(..., help="Wallet address"),
    label: str = typer.Argument(..., help="New label"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Rename a wallet."""

    async def _run(client):
        return await client.call("wallet.rename", {"address": address, "label": label})

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    _print_wallet(_wallet_row(result), "Renamed")


@app.command("remove")
def wallet_remove(
    address: str = typer.Argument(..., help="Wallet address to remove from the vault"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Remove a wallet from the vault (export it first; this deletes the keystore)."""

    if not yes:
        if json_output:
            emit_error(
                "confirmation required; rerun with --yes to execute",
                json_output=True,
                code="CONFIRMATION_REQUIRED",
            )
            raise typer.Exit(2)
        typer.confirm(f"Delete the keystore for {address}? Funds stay on-chain.", abort=True)
    password = read_password("Vault password", json_output=json_output)

    async def _run(client):
        return await client.call("wallet.remove", {"address": address, "password": password})

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    console.print(f"Removed wallet {address}.")


@app.command("primary")
def wallet_primary(
    address: str = typer.Argument(..., help="Wallet address to make the default"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Make a wallet the primary (default) one."""

    async def _run(client):
        return await client.call("wallet.setPrimary", {"address": address})

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    _print_wallet(_wallet_row(result), "Primary is now")


@app.command("balances")
def wallet_balances(
    address: str | None = typer.Argument(None, help="Wallet address (default: every wallet)"),
    chain: str | None = typer.Option(None, "--chain", help="base or robinhood"),
    refresh: bool = typer.Option(
        False, "--refresh", help="Re-read the chain first instead of showing the last sync"
    ),
    hidden: bool = typer.Option(False, "--hidden", help="Include hidden (junk) tokens"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show token balances with USD values."""

    params: dict[str, Any] = {}
    if address:
        params["address"] = address
    if chain:
        params["chainId"] = chain_id_from_arg(chain)
    if refresh:
        params["refresh"] = True
    if hidden:
        params["includeHidden"] = True

    async def _run(client):
        return await client.call("wallet.balances", params)

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    rows = result.get("balances", []) if isinstance(result, dict) else []
    table = Table(title="Balances", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("Chain")
    table.add_column("Token")
    table.add_column("Amount", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Value", justify="right")
    table.add_column("24h", justify="right")
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = markup_escape(token_symbol(row.get("token")))
        table.add_row(
            chain_label(row.get("chainId")),
            f"[dim]{symbol} (hidden)[/dim]" if row.get("hidden") else symbol,
            amount_text(row.get("amount")),
            money(row.get("priceUsd")),
            money(row.get("valueUsd")),
            percent(row.get("change24hPct")),
        )
    console.print(table)
    hidden_count = int((result.get("hiddenCount") or 0) if isinstance(result, dict) else 0)
    if hidden_count and not hidden:
        noun = "token" if hidden_count == 1 else "tokens"
        console.print(
            f"[dim]{hidden_count} junk {noun} hidden; add --hidden to list them, "
            "or `agentos trade unhide` to keep one.[/dim]"
        )
    reads = result.get("chains", []) if isinstance(result, dict) else []
    stale = [r for r in reads if isinstance(r, dict) and r.get("status") != "ok"]
    for read in stale:
        note = f" ({read['reason']})" if read.get("reason") else ""
        console.print(
            f"[yellow]{chain_label(read.get('chainId'))}: last chain read "
            f"{read.get('status')}{note}; amounts shown are the last good values.[/yellow]"
        )
