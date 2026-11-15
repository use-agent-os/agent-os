"""``agentos wallet`` CLI surface tests.

The gateway round-trip is stubbed at ``run_gateway_sync``: these cover the
argument shapes, the RPC names and params the CLI sends, password handling,
and rendered output — not the vault, which ``tests/test_trading`` owns.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest
from typer.testing import CliRunner

from agentos.cli import wallet_cmd

runner = CliRunner()

ADDR = "0x1111111111111111111111111111111111111111"
_WALLET = {
    "address": ADDR,
    "label": "main",
    "primary": True,
    "createdAt": 1000,
    "chains": [8453, 4663],
}


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.payloads: dict[str, Any] = {
            "wallet.status": {
                "initialized": True,
                "unlocked": True,
                "unlockMode": "auto",
                "walletCount": 1,
                "primary": ADDR,
                "vaultPath": "/home/x/.agentos/wallets",
            },
            "wallet.list": {"wallets": [_WALLET], "primary": ADDR},
            "wallet.create": {"wallet": _WALLET},
            "wallet.import": {"wallet": {**_WALLET, "label": "cold"}},
            "wallet.export": {"privateKey": "0xdeadbeef", "keystoreJson": '{"version": 3}'},
            "wallet.rename": {"wallet": {**_WALLET, "label": "renamed"}},
            "wallet.remove": {"removed": True},
            "wallet.setPrimary": {"wallet": _WALLET},
            "wallet.setup": {"initialized": True},
            "wallet.unlock": {"unlocked": True},
            "wallet.lock": {"unlocked": False},
            "wallet.balances": {
                "balances": [
                    {
                        "chainId": 8453,
                        "token": {"symbol": "USDC", "address": "0xusdc"},
                        "amount": "12.5",
                        "priceUsd": 1.0,
                        "valueUsd": 12.5,
                        "change24hPct": 0.01,
                    }
                ],
                "updatedAt": 1,
            },
        }

    async def call(self, method: str, params: dict | None = None) -> Any:
        self.calls.append((method, dict(params or {})))
        return self.payloads[method]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    fake = _FakeClient()

    def _run(action, **kwargs):
        return asyncio.run(action(fake))

    monkeypatch.setattr(wallet_cmd, "run_gateway_sync", _run)
    monkeypatch.delenv(wallet_cmd.PASSWORD_ENV, raising=False)
    monkeypatch.delenv(wallet_cmd.KEYSTORE_PASSWORD_ENV, raising=False)
    monkeypatch.setenv("COLUMNS", "220")
    monkeypatch.setattr(wallet_cmd.console, "_width", 220)
    return fake


def test_status_renders_and_json(client: _FakeClient) -> None:
    result = runner.invoke(wallet_cmd.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "auto" in result.output
    assert ADDR in result.output

    as_json = runner.invoke(wallet_cmd.app, ["status", "--json"])
    assert json.loads(as_json.stdout)["unlockMode"] == "auto"
    assert client.calls == [("wallet.status", {}), ("wallet.status", {})]


def test_setup_prompts_twice_and_sends_mode(client: _FakeClient) -> None:
    result = runner.invoke(wallet_cmd.app, ["setup", "--mode", "manual"], input="pw\npw\n")
    assert result.exit_code == 0, result.output
    assert client.calls == [("wallet.setup", {"password": "pw", "unlockMode": "manual"})]
    assert "pw" not in result.output


def test_setup_rejects_unknown_mode(client: _FakeClient) -> None:
    result = runner.invoke(wallet_cmd.app, ["setup", "--mode", "keychain"], input="pw\npw\n")
    assert result.exit_code != 0
    assert client.calls == []


def test_setup_auto_warns_about_unlock_key(client: _FakeClient) -> None:
    result = runner.invoke(wallet_cmd.app, ["setup"], input="pw\npw\n")
    assert result.exit_code == 0, result.output
    assert "unlock.key" in result.output
    # No --mode: the gateway's [trading] unlock_mode decides (WAL-25).
    assert "unlockMode" not in client.calls[0][1]


def test_unlock_uses_env_password_without_prompting(
    client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(wallet_cmd.PASSWORD_ENV, "from-env")
    result = runner.invoke(wallet_cmd.app, ["unlock", "--json"])
    assert result.exit_code == 0, result.output
    assert client.calls == [("wallet.unlock", {"password": "from-env"})]
    assert "from-env" not in result.stdout


def test_unlock_json_without_env_or_tty_fails_cleanly(client: _FakeClient) -> None:
    result = runner.invoke(wallet_cmd.app, ["unlock", "--json"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert client.calls == []


def test_lock(client: _FakeClient) -> None:
    result = runner.invoke(wallet_cmd.app, ["lock"])
    assert result.exit_code == 0
    assert client.calls == [("wallet.lock", {})]


def test_list_marks_primary(client: _FakeClient) -> None:
    result = runner.invoke(wallet_cmd.app, ["list"])
    assert result.exit_code == 0, result.output
    assert "★" in result.output
    assert "main" in result.output
    assert "base" in result.output and "robinhood" in result.output


def test_list_empty_hint(client: _FakeClient) -> None:
    client.payloads["wallet.list"] = {"wallets": [], "primary": None}
    result = runner.invoke(wallet_cmd.app, ["list"])
    assert "agentos wallet create" in result.output


def test_create_sends_label(client: _FakeClient) -> None:
    result = runner.invoke(wallet_cmd.app, ["create", "--label", "main", "--json"])
    assert result.exit_code == 0, result.output
    assert client.calls == [("wallet.create", {"label": "main"})]
    assert json.loads(result.stdout)["wallet"]["address"] == ADDR


def test_import_private_key_from_stdin(client: _FakeClient) -> None:
    result = runner.invoke(
        wallet_cmd.app,
        ["import", "--label", "cold", "--private-key-stdin"],
        input="0xabc123\n",
    )
    assert result.exit_code == 0, result.output
    assert client.calls == [("wallet.import", {"label": "cold", "privateKey": "0xabc123"})]
    assert "0xabc123" not in result.output


def test_import_keystore_file_reads_json_and_password(
    client: _FakeClient, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keystore = tmp_path / "ks.json"
    keystore.write_text('{"version": 3, "crypto": {}}', encoding="utf-8")
    monkeypatch.setenv(wallet_cmd.KEYSTORE_PASSWORD_ENV, "kspw")
    result = runner.invoke(
        wallet_cmd.app, ["import", "--label", "cold", "--keystore", str(keystore)]
    )
    assert result.exit_code == 0, result.output
    method, params = client.calls[0]
    assert method == "wallet.import"
    assert params["label"] == "cold"
    assert params["keystorePassword"] == "kspw"
    assert json.loads(params["keystoreJson"])["version"] == 3


def test_import_requires_exactly_one_source(client: _FakeClient, tmp_path) -> None:
    result = runner.invoke(wallet_cmd.app, ["import", "--label", "x"])
    assert result.exit_code != 0
    keystore = tmp_path / "ks.json"
    keystore.write_text("{}", encoding="utf-8")
    both = runner.invoke(
        wallet_cmd.app,
        ["import", "--label", "x", "--private-key-stdin", "--keystore", str(keystore)],
        input="0x1\n",
    )
    assert both.exit_code != 0
    assert client.calls == []


def test_import_rejects_non_json_keystore(client: _FakeClient, tmp_path) -> None:
    keystore = tmp_path / "ks.json"
    keystore.write_text("not json", encoding="utf-8")
    result = runner.invoke(wallet_cmd.app, ["import", "--label", "x", "--keystore", str(keystore)])
    assert result.exit_code != 0
    assert client.calls == []


def test_export_private_key_asks_password_and_prints(client: _FakeClient) -> None:
    result = runner.invoke(wallet_cmd.app, ["export", ADDR, "--private-key"], input="pw\n")
    assert result.exit_code == 0, result.output
    assert client.calls == [
        ("wallet.export", {"address": ADDR, "password": "pw", "format": "privateKey"})
    ]
    assert "0xdeadbeef" in result.output
    assert "controls the wallet" in result.output


def test_export_keystore_to_file_is_private(client: _FakeClient, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(wallet_cmd.PASSWORD_ENV, "pw")
    out = tmp_path / "wallet.json"
    result = runner.invoke(
        wallet_cmd.app, ["export", ADDR, "--keystore", "--out", str(out), "--json"]
    )
    assert result.exit_code == 0, result.output
    assert client.calls[0][1]["format"] == "keystore"
    assert out.read_text(encoding="utf-8") == '{"version": 3}'
    if os.name != "nt":  # Windows has no POSIX modes to check
        assert oct(out.stat().st_mode & 0o777) == "0o600"
    payload = json.loads(result.stdout)
    assert payload == {"written": str(out), "format": "keystore", "private": True}
    assert "version" not in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_export_file_is_private_from_creation(client: _FakeClient, tmp_path, monkeypatch) -> None:
    """WAL-14: the key is never in a file the umask made readable.

    A no-op ``chmod`` stands in for a filesystem where it fails (or the window
    before it runs): the mode the file was *created* with is what is left.
    """
    monkeypatch.setenv(wallet_cmd.PASSWORD_ENV, "pw")
    monkeypatch.setattr(wallet_cmd.os, "chmod", lambda *args, **kwargs: None)
    previous = os.umask(0o022)
    try:
        out = tmp_path / "key.txt"
        result = runner.invoke(wallet_cmd.app, ["export", ADDR, "--private-key", "--out", str(out)])
    finally:
        os.umask(previous)
    assert result.exit_code == 0, result.output
    assert out.read_text(encoding="utf-8") == "0xdeadbeef"
    assert oct(out.stat().st_mode & 0o777) == "0o600"


def test_export_never_overwrites_an_existing_file(
    client: _FakeClient, tmp_path, monkeypatch
) -> None:
    """WAL-14: an existing (possibly world-readable, possibly open) file is refused."""
    monkeypatch.setenv(wallet_cmd.PASSWORD_ENV, "pw")
    out = tmp_path / "key.txt"
    out.write_text("old", encoding="utf-8")
    if os.name != "nt":
        out.chmod(0o644)
    result = runner.invoke(
        wallet_cmd.app, ["export", ADDR, "--private-key", "--out", str(out), "--json"]
    )
    assert result.exit_code != 0
    assert out.read_text(encoding="utf-8") == "old"
    assert client.calls == []  # refused before the password left the terminal
    assert "already exists" in result.output


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_export_refuses_a_symlink_at_the_target(client: _FakeClient, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(wallet_cmd.PASSWORD_ENV, "pw")
    elsewhere = tmp_path / "elsewhere"
    out = tmp_path / "key.txt"
    out.symlink_to(elsewhere)
    result = runner.invoke(wallet_cmd.app, ["export", ADDR, "--private-key", "--out", str(out)])
    assert result.exit_code != 0
    assert not elsewhere.exists()


def test_write_private_file_refuses_existing_and_cleans_up(tmp_path) -> None:
    target = tmp_path / "secret.txt"
    assert wallet_cmd.write_private_file(target, "s3cret") in (True, False)
    assert target.read_text(encoding="utf-8") == "s3cret"
    with pytest.raises(FileExistsError):
        wallet_cmd.write_private_file(target, "other")
    assert target.read_text(encoding="utf-8") == "s3cret"


@pytest.mark.skipif(os.name != "nt", reason="Windows ACLs")
def test_export_file_acl_is_owner_only_on_windows(
    client: _FakeClient, tmp_path, monkeypatch
) -> None:
    import subprocess

    monkeypatch.setenv(wallet_cmd.PASSWORD_ENV, "pw")
    out = tmp_path / "key.txt"
    result = runner.invoke(wallet_cmd.app, ["export", ADDR, "--private-key", "--out", str(out)])
    assert result.exit_code == 0, result.output
    acl = subprocess.run(["icacls", str(out)], capture_output=True, text=True, check=True).stdout
    for broad in ("Everyone", "BUILTIN\\Users", "Authenticated Users"):
        assert broad not in acl, acl
    assert "(I)" not in acl  # nothing inherited from the directory


def test_export_requires_exactly_one_format(client: _FakeClient) -> None:
    neither = runner.invoke(wallet_cmd.app, ["export", ADDR], input="pw\n")
    both = runner.invoke(
        wallet_cmd.app, ["export", ADDR, "--keystore", "--private-key"], input="pw\n"
    )
    assert neither.exit_code != 0 and both.exit_code != 0
    assert client.calls == []


def test_rename_and_primary(client: _FakeClient) -> None:
    renamed = runner.invoke(wallet_cmd.app, ["rename", ADDR, "renamed"])
    primary = runner.invoke(wallet_cmd.app, ["primary", ADDR])
    assert renamed.exit_code == 0 and primary.exit_code == 0
    assert client.calls == [
        ("wallet.rename", {"address": ADDR, "label": "renamed"}),
        ("wallet.setPrimary", {"address": ADDR}),
    ]
    assert "renamed" in renamed.output


def test_remove_needs_confirmation_then_password(client: _FakeClient) -> None:
    declined = runner.invoke(wallet_cmd.app, ["remove", ADDR], input="n\n")
    assert declined.exit_code != 0
    assert client.calls == []

    confirmed = runner.invoke(wallet_cmd.app, ["remove", ADDR, "--yes"], input="pw\n")
    assert confirmed.exit_code == 0, confirmed.output
    assert client.calls == [("wallet.remove", {"address": ADDR, "password": "pw"})]


def test_remove_json_requires_yes(client: _FakeClient, monkeypatch) -> None:
    monkeypatch.setenv(wallet_cmd.PASSWORD_ENV, "pw")
    result = runner.invoke(wallet_cmd.app, ["remove", ADDR, "--json"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert client.calls == []


def test_balances_filters_by_address_and_chain(client: _FakeClient) -> None:
    result = runner.invoke(wallet_cmd.app, ["balances", ADDR, "--chain", "robinhood"])
    assert result.exit_code == 0, result.output
    assert client.calls == [("wallet.balances", {"address": ADDR, "chainId": 4663})]
    assert "USDC" in result.output
    assert "$12.50" in result.output


def test_balances_hidden_note_and_flag(client: _FakeClient) -> None:
    client.payloads["wallet.balances"]["hiddenCount"] = 3
    quiet = runner.invoke(wallet_cmd.app, ["balances"])
    assert quiet.exit_code == 0, quiet.output
    assert "3 junk tokens hidden" in quiet.output
    loud = runner.invoke(wallet_cmd.app, ["balances", "--hidden"])
    assert loud.exit_code == 0, loud.output
    assert "junk tokens hidden" not in loud.output
    assert client.calls[-1] == ("wallet.balances", {"includeHidden": True})


def test_balances_rejects_unknown_chain(client: _FakeClient) -> None:
    result = runner.invoke(wallet_cmd.app, ["balances", "--chain", "solana"])
    assert result.exit_code != 0
    assert "unknown chain" in result.output
    assert client.calls == []


def test_chain_arg_accepts_numeric_ids() -> None:
    assert wallet_cmd.chain_id_from_arg("8453") == 8453
    assert wallet_cmd.chain_id_from_arg("Robinhood") == 4663
    assert wallet_cmd.chain_label(4663) == "robinhood"
    assert wallet_cmd.chain_label("junk") == "junk"


def test_formatters() -> None:
    assert wallet_cmd.money(None) == "—"
    assert wallet_cmd.money(-12.345) == "-$12.35"
    assert wallet_cmd.money("1234.5") == "$1,234.50"
    assert wallet_cmd.percent(3.14159) == "+3.14%"
    assert wallet_cmd.percent(-1) == "-1.00%"
    assert wallet_cmd.short_address(ADDR) == "0x1111…1111"
    assert wallet_cmd.token_symbol({"address": ADDR}) == "0x1111…1111"
    assert wallet_cmd.is_address(ADDR) and not wallet_cmd.is_address("ETH")
