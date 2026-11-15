"""Wallet vault: keystore v3 files under ``~/.agentos/wallets``.

One vault password encrypts every wallet. Two unlock modes, neither tied to
an operating system secret store:

* ``auto``   — the password sits in ``unlock.key`` (mode 0600) next to the
  keystores, sealed under a key kept outside the state directory
  (:mod:`agentos.trading.unlock_key`), and the gateway opens it at first use.
  Neither file opens the vault alone, but anyone who can run code as this OS
  user can read both and spend: the OS sandbox or ``manual`` mode is the
  boundary against that.
* ``manual`` — nothing on disk; ``unlock(password)`` is called per gateway
  session and the decrypted keys live only in this process's memory.

The operator's ``[trading] unlock_mode`` is a policy over both: when it says
``manual`` the vault never reads or writes ``unlock.key``, whatever its own
index records, and refuses to be set up or switched to ``auto``. A leftover
``unlock.key`` is removed only by an explicit ``set_unlock_mode("manual")``.

Nothing in this module logs, returns or serialises a private key except
:meth:`Vault.export`, which re-verifies the password first.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import structlog

from agentos.paths import default_agentos_home
from agentos.redact import register_secret_values
from agentos.trading import unlock_key
from agentos.trading.chains import checksum_address, normalize_address

UnlockMode = Literal["auto", "manual"]

INDEX_FILE = "index.json"
UNLOCK_FILE = "unlock.key"
INDEX_VERSION = 1

log = structlog.get_logger(__name__)


class VaultError(RuntimeError):
    code = "wallet.error"


class VaultNotInitializedError(VaultError):
    code = "wallet.not_initialized"


class VaultAlreadyInitializedError(VaultError):
    code = "wallet.already_initialized"


class VaultLockedError(VaultError):
    code = "wallet.locked"


class BadPasswordError(VaultError):
    code = "wallet.bad_password"


class WalletNotFoundError(VaultError):
    code = "wallet.not_found"


class UnlockModePolicyError(VaultError):
    """The operator's ``[trading] unlock_mode`` forbids what was asked."""

    code = "wallet.unlock_mode_policy"


@dataclass
class WalletRecord:
    address: str  # EIP-55 checksum form
    label: str
    created_at: float
    created_block: dict[str, int] = field(default_factory=dict)
    imported: bool = False

    @property
    def key(self) -> str:
        return self.address.lower()

    def to_dict(self, *, primary: bool) -> dict[str, Any]:
        return {
            "address": self.address,
            "label": self.label,
            "primary": primary,
            "createdAt": int(self.created_at * 1000),
            "imported": self.imported,
            "createdBlock": dict(self.created_block),
        }


def default_vault_root() -> Path:
    return default_agentos_home() / "wallets"


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, stat.S_IRWXU)
    except OSError:  # pragma: no cover - permissions are best effort on odd filesystems
        pass


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        # newline="": what is handed in is what lands on disk. Text mode
        # would translate "\n" to "\r\n" on Windows, so a password
        # holding a newline came back as a different string and never
        # unlocked (#3504).
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover
        pass


class Vault:
    """Keystore directory + in-memory unlocked keys."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        kdf: Literal["pbkdf2", "scrypt"] | None = None,
        kdf_iterations: int | None = None,
        configured_mode: Callable[[], str | None] | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else default_vault_root()
        self._kdf = kdf
        self._iterations = kdf_iterations
        self._lock = threading.RLock()
        self._password: str | None = None
        self._keys: dict[str, bytes] = {}
        #: Reads the operator's ``[trading] unlock_mode`` at call time (the
        #: trading service wires it to the live config), so a changed policy
        #: applies without rebuilding the vault.
        self.configured_mode = configured_mode
        self._warned_ignored_unlock_key = False
        self._warned_unlock_key_unavailable = False

    # ── paths ──────────────────────────────────────────────────────────

    @property
    def index_path(self) -> Path:
        return self.root / INDEX_FILE

    @property
    def unlock_path(self) -> Path:
        return self.root / UNLOCK_FILE

    def keystore_path(self, address: str) -> Path:
        return self.root / f"{normalize_address(address)}.json"

    # ── index ──────────────────────────────────────────────────────────

    @property
    def initialized(self) -> bool:
        return self.index_path.is_file()

    def _read_index(self) -> dict[str, Any]:
        if not self.initialized:
            raise VaultNotInitializedError("wallet vault is not set up yet")
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise VaultError(f"cannot read wallet index: {exc}") from exc
        if not isinstance(data, dict):
            raise VaultError("wallet index is malformed")
        data.setdefault("wallets", [])
        data.setdefault("unlock_mode", "manual")
        data.setdefault("primary", None)
        return data

    def _write_index(self, data: dict[str, Any]) -> None:
        data["version"] = INDEX_VERSION
        _secure_dir(self.root)
        _write_private(self.index_path, json.dumps(data, indent=2, sort_keys=True))

    @staticmethod
    def _record(raw: dict[str, Any]) -> WalletRecord:
        return WalletRecord(
            address=checksum_address(str(raw["address"])),
            label=str(raw.get("label") or ""),
            created_at=float(raw.get("created_at") or 0.0),
            created_block={str(k): int(v) for k, v in (raw.get("created_block") or {}).items()},
            imported=bool(raw.get("imported", False)),
        )

    @staticmethod
    def _raw(record: WalletRecord) -> dict[str, Any]:
        return {
            "address": record.address,
            "label": record.label,
            "created_at": record.created_at,
            "created_block": dict(record.created_block),
            "imported": record.imported,
        }

    # ── status ─────────────────────────────────────────────────────────

    @property
    def unlocked(self) -> bool:
        return self._password is not None

    def policy_mode(self) -> UnlockMode | None:
        """The operator's configured ``unlock_mode``, or ``None`` when not wired."""
        if self.configured_mode is None:
            return None
        value = self.configured_mode()
        if value == "manual":
            return "manual"
        return "auto" if value == "auto" else None

    @property
    def stored_unlock_mode(self) -> UnlockMode:
        """The mode the vault's own index records."""
        if not self.initialized:
            return "auto"
        mode = self._read_index().get("unlock_mode")
        return "manual" if mode == "manual" else "auto"

    @property
    def unlock_mode(self) -> UnlockMode:
        """The mode in force: ``manual`` whenever the operator configured it."""
        if self.policy_mode() == "manual":
            return "manual"
        if not self.initialized:
            return self.policy_mode() or "auto"
        return self.stored_unlock_mode

    def status(self) -> dict[str, Any]:
        wallets = self.list() if self.initialized else []
        primary = self.primary_address() if self.initialized else None
        stored = self.stored_unlock_mode if self.initialized else None
        return {
            "initialized": self.initialized,
            "unlocked": self.unlocked,
            "unlockMode": self.unlock_mode,
            # What the index records and what the operator configured; they
            # differ only for a vault set up auto before unlock_mode = manual,
            # whose unlock.key stays (unused) until `agentos wallet mode manual`.
            "storedUnlockMode": stored,
            "configuredUnlockMode": self.policy_mode(),
            "unlockKeyPresent": self.unlock_path.exists(),
            "unlockKeyProtection": self._unlock_key_protection(),
            "walletCount": len(wallets),
            "primary": primary,
            "vaultPath": str(self.root),
        }

    def _refuse_auto_under_manual_policy(self) -> None:
        if self.policy_mode() == "manual":
            raise UnlockModePolicyError(
                "trading.unlock_mode is 'manual': the vault password is never stored. "
                "Change trading.unlock_mode first to use auto unlock."
            )

    # ── setup / unlock ─────────────────────────────────────────────────

    def _encrypt(self, key: bytes, password: str) -> dict[str, Any]:
        from eth_account import Account

        kwargs: dict[str, Any] = {}
        if self._kdf:
            kwargs["kdf"] = self._kdf
        if self._iterations:
            kwargs["iterations"] = self._iterations
        return dict(Account.encrypt(key, password, **kwargs))

    @staticmethod
    def _decrypt(keystore: dict[str, Any] | str, password: str) -> bytes:
        from eth_account import Account

        try:
            return bytes(Account.decrypt(keystore, password))
        except ValueError as exc:
            raise BadPasswordError("wrong vault password") from exc

    def setup(self, password: str, unlock_mode: UnlockMode | None = None) -> None:
        password = _require_password(password)
        if unlock_mode is None:
            unlock_mode = self.policy_mode() or "auto"
        if unlock_mode not in ("auto", "manual"):
            raise ValueError("unlock mode must be 'auto' or 'manual'")
        with self._lock:
            if self.initialized:
                raise VaultAlreadyInitializedError("wallet vault already exists")
            if unlock_mode == "auto":
                self._refuse_auto_under_manual_policy()
            _secure_dir(self.root)
            verifier = self._encrypt(secrets.token_bytes(32), password)
            self._write_index(
                {
                    "unlock_mode": unlock_mode,
                    "primary": None,
                    "wallets": [],
                    "verifier": verifier,
                    "created_at": time.time(),
                }
            )
            if unlock_mode == "auto":
                self._store_unlock_key(password)
            else:
                self._remove_unlock_key()
            self._password = password
            register_secret_values(password)
            self._keys = {}

    def verify_password(self, password: str) -> None:
        index = self._read_index()
        verifier = index.get("verifier")
        if not isinstance(verifier, dict):
            raise VaultError("wallet index has no password verifier")
        self._decrypt(verifier, password)

    def unlock(self, password: str) -> None:
        """Unlock with the operator's password.

        In ``auto`` mode a stored password that no longer opens (its key was
        lost with a container's home, a legacy plain-text file the gateway
        could not re-seal) is sealed again, so the next restart unlocks on
        its own. A deliberately deleted ``unlock.key`` is left deleted.
        """
        with self._lock:
            self._unlock(password)
            if self.unlock_mode == "auto" and self.unlock_path.exists():
                if not self._unlock_key_opens_to(password):
                    try:
                        self._store_unlock_key(password)
                    except (OSError, VaultError) as exc:
                        log.warning("vault.unlock_key_reseal_failed", error=type(exc).__name__)
                    else:
                        log.warning("vault.unlock_key_resealed")

    def _unlock(self, password: str) -> None:
        password = _require_password(password)
        with self._lock:
            self.verify_password(password)
            keys: dict[str, bytes] = {}
            for record in self.list():
                path = self.keystore_path(record.address)
                if not path.is_file():
                    continue
                keystore = json.loads(path.read_text(encoding="utf-8"))
                keys[record.key] = self._decrypt(keystore, password)
            self._keys = keys
            self._password = password
            register_secret_values(password)

    def lock(self) -> None:
        with self._lock:
            self._keys = {}
            self._password = None

    def try_auto_unlock(self) -> bool:
        """Unlock from ``unlock.key`` when in ``auto`` mode. Never raises."""
        with self._lock:
            if self.unlocked:
                return True
            if not self.initialized:
                return False
            if self.unlock_mode != "auto":
                self._warn_ignored_unlock_key()
                return False
            stored = self._read_unlock_key()
            if stored is None:
                return False
            try:
                envelope = unlock_key.parse_envelope(stored)
                if envelope is not None:
                    self._unlock(unlock_key.open_password(envelope))
                    return True
            except (unlock_key.UnlockKeyError, VaultError, ValueError) as exc:
                self._warn_unlock_key_unavailable(exc)
                return False
            # A legacy plain-text file (written before the password was
            # sealed). Verbatim first, then stripped. ``setup`` writes the password
            # exactly as it was set, so surrounding whitespace -- a trailing
            # newline, most often -- is part of it, and reading it back
            # stripped meant such a vault never auto-unlocked and never said
            # why (#3504). The stripped form stays as a fallback, since a
            # file edited by hand is where the strip came from.
            candidates = [stored, stored.strip()]
            for password in dict.fromkeys(c for c in candidates if c):
                try:
                    self._unlock(password)
                except (VaultError, ValueError):
                    # ValueError as well: ``_require_password`` raises it for
                    # anything under 8 characters, and this method is
                    # documented never to raise.
                    continue
                self._migrate_plaintext_unlock_key(password)
                return True
            return False

    # ── the stored password (auto mode) ────────────────────────────────

    def _read_unlock_key(self) -> str | None:
        try:
            # newline="": the password is bytes the user chose, not lines to
            # be normalised (#3504).
            with self.unlock_path.open(encoding="utf-8", newline="") as handle:
                return handle.read()
        except OSError:
            return None

    def _store_unlock_key(self, password: str) -> None:
        """Seal *password* into ``unlock.key`` under a fresh key; drop the old key."""
        previous = self._read_unlock_key()
        try:
            sealed = unlock_key.seal_password(password)
        except unlock_key.UnlockKeyError as exc:  # a malformed AGENTOS_VAULT_KEK
            raise VaultError(f"cannot store the vault password: {exc}") from exc
        try:
            _write_private(self.unlock_path, sealed)
        except BaseException:
            unlock_key.discard_kek(sealed)
            raise
        unlock_key.discard_kek(previous)

    def _remove_unlock_key(self) -> bool:
        """Delete ``unlock.key`` and the key that seals it. Returns whether it existed."""
        previous = self._read_unlock_key()
        existed = self.unlock_path.exists()
        self.unlock_path.unlink(missing_ok=True)
        unlock_key.discard_kek(previous)
        return existed

    def _migrate_plaintext_unlock_key(self, password: str) -> None:
        try:
            self._store_unlock_key(password)
        except (OSError, VaultError) as exc:
            # Still unlocked from the old file, which stays as it was: an
            # unwritable key directory must not cost the operator auto unlock.
            log.warning("vault.unlock_key_migration_failed", error=type(exc).__name__)
            return
        log.warning("vault.unlock_key_migrated", protection=self._unlock_key_protection())

    def _unlock_key_opens_to(self, password: str) -> bool:
        stored = self._read_unlock_key()
        if stored is None:
            return False
        try:
            envelope = unlock_key.parse_envelope(stored)
            return envelope is not None and unlock_key.open_password(envelope) == password
        except unlock_key.UnlockKeyError:
            return False

    def _unlock_key_protection(self) -> str | None:
        """``sealed:<store>``, ``plaintext`` (pre-migration), ``damaged`` or ``None``."""
        stored = self._read_unlock_key()
        if stored is None:
            return None
        try:
            envelope = unlock_key.parse_envelope(stored)
        except unlock_key.UnlockKeyError:
            return "damaged"
        return "plaintext" if envelope is None else f"sealed:{envelope.backend}"

    def _warn_unlock_key_unavailable(self, exc: Exception) -> None:
        if self._warned_unlock_key_unavailable:
            return
        self._warned_unlock_key_unavailable = True
        log.warning(
            "vault.unlock_key_unavailable",
            reason=str(exc) if isinstance(exc, unlock_key.UnlockKeyError) else type(exc).__name__,
            hint="unlock once with `agentos wallet unlock` to seal the password again",
        )

    def _warn_ignored_unlock_key(self) -> None:
        """Say once that a stored password is being ignored under a manual policy."""
        if self._warned_ignored_unlock_key:
            return
        if self.stored_unlock_mode == "auto" and self.unlock_path.exists():
            self._warned_ignored_unlock_key = True
            log.warning(
                "vault.unlock_key_ignored",
                reason="trading.unlock_mode is manual",
                hint="run `agentos wallet mode manual` to delete the stored password",
            )

    def set_unlock_mode(self, mode: UnlockMode, password: str) -> None:
        """Switch the vault's unlock mode; the one way ``unlock.key`` is removed."""
        if mode not in ("auto", "manual"):
            raise ValueError("unlock mode must be 'auto' or 'manual'")
        with self._lock:
            if mode == "auto":
                self._refuse_auto_under_manual_policy()
            self.verify_password(password)
            index = self._read_index()
            previous = index.get("unlock_mode")
            index["unlock_mode"] = mode
            self._write_index(index)
            removed = False
            if mode == "auto":
                self._store_unlock_key(password)
            else:
                removed = self._remove_unlock_key()
            log.warning(
                "vault.unlock_mode_changed",
                previous=previous,
                mode=mode,
                unlock_key_removed=removed,
            )
            if not self.unlocked:
                self._unlock(password)

    def change_password(self, password: str, new_password: str) -> None:
        new_password = _require_password(new_password)
        with self._lock:
            self.verify_password(password)
            index = self._read_index()
            records = [self._record(raw) for raw in index["wallets"]]
            keys: dict[str, bytes] = {}
            for record in records:
                path = self.keystore_path(record.address)
                keystore = json.loads(path.read_text(encoding="utf-8"))
                keys[record.key] = self._decrypt(keystore, password)
            for record in records:
                _write_private(
                    self.keystore_path(record.address),
                    json.dumps(self._encrypt(keys[record.key], new_password)),
                )
            index["verifier"] = self._encrypt(secrets.token_bytes(32), new_password)
            self._write_index(index)
            # Under a manual policy the stored password is not refreshed: the
            # old one no longer opens anything, and writing the new one would
            # be the very thing the operator configured away.
            if index.get("unlock_mode") == "auto" and self.policy_mode() != "manual":
                self._store_unlock_key(new_password)
            self._keys = keys
            self._password = new_password
            register_secret_values(new_password)

    def _require_unlocked(self) -> str:
        if not self.initialized:
            raise VaultNotInitializedError("wallet vault is not set up yet")
        if self._password is None:
            raise VaultLockedError("wallet vault is locked")
        return self._password

    # ── wallets ────────────────────────────────────────────────────────

    def list(self) -> list[WalletRecord]:
        index = self._read_index()
        return [self._record(raw) for raw in index.get("wallets", [])]

    def primary_address(self) -> str | None:
        index = self._read_index()
        primary = index.get("primary")
        if isinstance(primary, str) and primary:
            return checksum_address(primary)
        wallets = index.get("wallets") or []
        if wallets:
            return checksum_address(str(wallets[0]["address"]))
        return None

    def get(self, address: str) -> WalletRecord:
        key = normalize_address(address)
        for record in self.list():
            if record.key == key:
                return record
        raise WalletNotFoundError(f"no wallet {address}")

    def resolve(self, value: str | None) -> WalletRecord:
        """A wallet by address, label, or ``primary``/``None``."""
        if value is None or str(value).strip().lower() in {"", "primary", "default"}:
            primary = self.primary_address()
            if primary is None:
                raise WalletNotFoundError("no wallets yet")
            return self.get(primary)
        text = str(value).strip()
        if text.startswith("0x"):
            return self.get(text)
        for record in self.list():
            if record.label.lower() == text.lower():
                return record
        raise WalletNotFoundError(f"no wallet {value!r}")

    def _add(self, key: bytes, label: str, *, imported: bool) -> WalletRecord:
        from eth_account import Account

        password = self._require_unlocked()
        account = Account.from_key(key)
        record = WalletRecord(
            address=checksum_address(account.address),
            label=(label or "").strip() or f"Wallet {account.address[:6]}",
            created_at=time.time(),
            imported=imported,
        )
        index = self._read_index()
        if any(normalize_address(w["address"]) == record.key for w in index["wallets"]):
            raise VaultError(f"wallet {record.address} already exists")
        _write_private(self.keystore_path(record.address), json.dumps(self._encrypt(key, password)))
        index["wallets"].append(self._raw(record))
        if not index.get("primary"):
            index["primary"] = record.address
        self._write_index(index)
        self._keys[record.key] = bytes(key)
        return record

    def create(self, label: str) -> WalletRecord:
        with self._lock:
            key = secrets.token_bytes(32)
            return self._add(key, label, imported=False)

    def import_private_key(self, label: str, private_key: str) -> WalletRecord:
        text = private_key.strip().removeprefix("0x")
        if len(text) != 64:
            raise ValueError("private key must be 32 bytes of hex")
        try:
            key = bytes.fromhex(text)
        except ValueError as exc:
            raise ValueError("private key must be hex") from exc
        with self._lock:
            return self._add(key, label, imported=True)

    def import_keystore(
        self, label: str, keystore_json: str, keystore_password: str
    ) -> WalletRecord:
        try:
            keystore = json.loads(keystore_json)
        except ValueError as exc:
            raise ValueError("keystore must be JSON") from exc
        _check_keystore_kdf(keystore)
        with self._lock:
            key = self._decrypt(keystore, keystore_password)
            return self._add(key, label, imported=True)

    def export(self, address: str, password: str, fmt: Literal["keystore", "privateKey"]) -> str:
        with self._lock:
            self.verify_password(password)
            record = self.get(address)
            if fmt == "keystore":
                return self.keystore_path(record.address).read_text(encoding="utf-8")
            if fmt == "privateKey":
                key = self._keys.get(record.key)
                if key is None:
                    keystore = json.loads(self.keystore_path(record.address).read_text("utf-8"))
                    key = self._decrypt(keystore, password)
                return "0x" + key.hex()
            raise ValueError("format must be 'keystore' or 'privateKey'")

    def rename(self, address: str, label: str) -> WalletRecord:
        label = (label or "").strip()
        if not label:
            raise ValueError("label is required")
        with self._lock:
            index = self._read_index()
            key = normalize_address(address)
            for raw in index["wallets"]:
                if normalize_address(raw["address"]) == key:
                    raw["label"] = label
                    self._write_index(index)
                    return self._record(raw)
        raise WalletNotFoundError(f"no wallet {address}")

    def remove(self, address: str, password: str) -> None:
        with self._lock:
            self.verify_password(password)
            index = self._read_index()
            key = normalize_address(address)
            before = len(index["wallets"])
            index["wallets"] = [
                raw for raw in index["wallets"] if normalize_address(raw["address"]) != key
            ]
            if len(index["wallets"]) == before:
                raise WalletNotFoundError(f"no wallet {address}")
            if index.get("primary") and normalize_address(index["primary"]) == key:
                index["primary"] = index["wallets"][0]["address"] if index["wallets"] else None
            self._write_index(index)
            self.keystore_path(address).unlink(missing_ok=True)
            self._keys.pop(key, None)

    def set_primary(self, address: str) -> WalletRecord:
        with self._lock:
            record = self.get(address)
            index = self._read_index()
            index["primary"] = record.address
            self._write_index(index)
            return record

    def set_created_block(self, address: str, chain_id: int, block: int) -> None:
        with self._lock:
            index = self._read_index()
            key = normalize_address(address)
            for raw in index["wallets"]:
                if normalize_address(raw["address"]) == key:
                    blocks = raw.setdefault("created_block", {})
                    blocks[str(chain_id)] = int(block)
                    self._write_index(index)
                    return
            raise WalletNotFoundError(f"no wallet {address}")

    def private_key(self, address: str) -> bytes:
        with self._lock:
            self._require_unlocked()
            key = self._keys.get(normalize_address(address))
            if key is None:
                record = self.get(address)
                path = self.keystore_path(record.address)
                if not path.is_file():
                    raise WalletNotFoundError(f"keystore for {address} is missing")
                keystore = json.loads(path.read_text(encoding="utf-8"))
                key = self._decrypt(keystore, self._require_unlocked())
                self._keys[record.key] = key
            return key


# Upper bounds on a caller-supplied keystore's KDF work factors. A hostile
# keystore with scrypt ``n`` in the billions would pin the gateway for hours
# inside ``Account.decrypt`` before the password is even checked.
_MAX_SCRYPT_N = 2**20
_MAX_SCRYPT_R = 32
_MAX_SCRYPT_P = 16
_MAX_PBKDF2_C = 2**24


def _check_keystore_kdf(keystore: Any) -> None:
    crypto = None
    if isinstance(keystore, dict):
        crypto = keystore.get("crypto") or keystore.get("Crypto")
    if not isinstance(crypto, dict):
        raise ValueError("keystore has no crypto section")
    kdf = str(crypto.get("kdf") or "").lower()
    params = crypto.get("kdfparams") or {}
    if not isinstance(params, dict):
        raise ValueError("keystore kdfparams must be an object")

    def _int(name: str) -> int:
        try:
            return int(params.get(name) or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"keystore kdfparams.{name} must be an integer") from exc

    if kdf == "scrypt":
        if _int("n") > _MAX_SCRYPT_N or _int("r") > _MAX_SCRYPT_R or _int("p") > _MAX_SCRYPT_P:
            raise ValueError("keystore scrypt parameters are too expensive to decrypt")
    elif kdf == "pbkdf2":
        if _int("c") > _MAX_PBKDF2_C:
            raise ValueError("keystore pbkdf2 iteration count is too expensive to decrypt")
    else:
        raise ValueError(f"keystore kdf {kdf or 'missing'!r} is not supported")


def _require_password(password: str) -> str:
    text = str(password or "")
    if len(text) < 8:
        raise ValueError("vault password must be at least 8 characters")
    return text
