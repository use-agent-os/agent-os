"""Wallet vault: keystore v3 files under ``~/.agentos/wallets``.

One vault password encrypts every wallet. Two unlock modes, neither tied to
an operating system secret store:

* ``auto``   — the password sits in ``unlock.key`` (mode 0600) next to the
  keystores, sealed under a key kept outside the state directory
  (:mod:`agentos.trading.unlock_key`), and the gateway opens it at first use.
  Neither file opens the vault alone, but anyone who can run code as this OS
  user can read both and spend: the OS sandbox or ``manual`` mode is the
  boundary against that. Under the desktop app that key is in no file: the
  app keeps it in the keychain and hands it to the gateway it spawns.
* ``manual`` — nothing on disk; ``unlock(password)`` is called per gateway
  session and the decrypted keys live only in this process's memory.

The operator's ``[trading] unlock_mode`` is a policy over both: when it says
``manual`` the vault never reads or writes ``unlock.key``, whatever its own
index records, and refuses to be set up or switched to ``auto``. A leftover
``unlock.key`` is removed only by an explicit ``set_unlock_mode("manual")``.

An explicit :meth:`Vault.lock` is the operator saying "nothing signs". It is
latched in ``index.json`` and outranks ``unlock.key``: an ``auto`` vault stays
locked, across gateway restarts too, until :meth:`Vault.unlock` is called with
the password.

Nothing in this module logs, returns or serialises a private key except
:meth:`Vault.export`, which re-verifies the password first, and
:meth:`Vault.create_batch`, which hands the caller the keys and recovery
phrases of the wallets it has just made (the vault keeps no phrase).
"""

from __future__ import annotations

import builtins
import hashlib
import hmac
import json
import os
import secrets
import stat
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import structlog

from agentos.paths import default_agentos_home
from agentos.redact import register_secret_values
from agentos.trading import unlock_key
from agentos.trading.chains import checksum_address, normalize_address
from agentos.trading.durable import fsync_dir, fsync_file

UnlockMode = Literal["auto", "manual"]

#: The most wallets one :meth:`Vault.create_batch` makes.
MAX_BATCH_CREATE = 30

INDEX_FILE = "index.json"
UNLOCK_FILE = "unlock.key"
INDEX_VERSION = 1
#: ``index.json`` key of the operator's lock latch (:meth:`Vault.lock`).
LOCK_LATCH = "operator_locked"
#: ``index.json`` key of the ledger seal's salt (:meth:`Vault.ledger_seal_key`).
LEDGER_SEAL = "ledger_seal"
_LEDGER_SEAL_LABEL = b"agentos/trading/ledger-seal/v1\x00"
#: ``index.json`` key naming the wallets a password change is still moving to
#: the new password, and the suffix of their staged keystores
#: (:meth:`Vault.change_password`).
REKEY_PENDING = "rekey_pending"
REKEY_SUFFIX = ".rekey"

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


class WalletExistsError(VaultError):
    """The vault already holds this key."""


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


def default_label(address: str) -> str:
    """The name a wallet gets when none is given: ``0xAbC_1234``.

    The address's first five characters and its last four, so two wallets
    made without a name still read apart in every list.
    """
    return f"{address[:5]}_{address[-4:]}"


@dataclass(frozen=True)
class MintedWallet:
    """A wallet :meth:`Vault.create_batch` just made, with its secrets."""

    record: WalletRecord
    #: ``0x``-prefixed hex.
    private_key: str
    #: The 12-word BIP-39 phrase the key derives from (``m/44'/60'/0'/0/0``).
    mnemonic: str

    def __repr__(self) -> str:  # never print a key
        return f"MintedWallet(address={self.record.address!r})"


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
            # Flushed before the rename: a keystore can be the only copy of a
            # key, and a rename alone may survive a power cut as an empty file.
            handle.flush()
            fsync_file(handle.fileno())
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover
        pass
    fsync_dir(path.parent)


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
        # The ledger seal key (``ledger_seal_key``): in memory while unlocked only.
        self._seal_key: bytes | None = None
        # Set when an unlock had to create the seal salt: the ledger's live rows
        # predate sealing and the service adopts them once (``take_new_seal``).
        self._seal_new = False

    # ── paths ──────────────────────────────────────────────────────────

    @property
    def index_path(self) -> Path:
        return self.root / INDEX_FILE

    @property
    def unlock_path(self) -> Path:
        return self.root / UNLOCK_FILE

    def keystore_path(self, address: str) -> Path:
        return self.root / f"{normalize_address(address)}.json"

    def _rekey_path(self, address: str) -> Path:
        path = self.keystore_path(address)
        return path.with_name(path.name + REKEY_SUFFIX)

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

    @property
    def operator_locked(self) -> bool:
        """The operator locked the vault and has not unlocked it since (any mode, any restart)."""
        if not self.initialized:
            return False
        try:
            return bool(self._read_index().get(LOCK_LATCH))
        except VaultError:
            return False

    def status(self) -> dict[str, Any]:
        wallets = self.list() if self.initialized else []
        primary = self.primary_address() if self.initialized else None
        stored = self.stored_unlock_mode if self.initialized else None
        return {
            "initialized": self.initialized,
            "unlocked": self.unlocked,
            "unlockMode": self.unlock_mode,
            "lockedByOperator": self.operator_locked,
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
            secret = secrets.token_bytes(32)
            seal = _new_ledger_seal()
            self._write_index(
                {
                    "unlock_mode": unlock_mode,
                    "primary": None,
                    "wallets": [],
                    "verifier": self._encrypt(secret, password),
                    LEDGER_SEAL: seal,
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
            self._seal_key = _derive_seal_key(secret, seal)
            self._seal_new = False

    def _open_verifier(self, password: str) -> bytes:
        """The verifier's secret: decrypting it is what proves the password."""
        # Every password-proven path starts here, so an interrupted password
        # change is settled before any keystore is read.
        self._settle_rekey()
        index = self._read_index()
        verifier = index.get("verifier")
        if not isinstance(verifier, dict):
            raise VaultError("wallet index has no password verifier")
        return self._decrypt(verifier, password)

    def verify_password(self, password: str) -> None:
        self._open_verifier(password)

    def unlock(self, password: str) -> None:
        """Unlock with the operator's password: the one way out of an operator's lock.

        In ``auto`` mode a stored password that no longer opens (its key was
        lost with a container's home, a legacy plain-text file the gateway
        could not re-seal) is sealed again, so the next restart unlocks on
        its own. A deliberately deleted ``unlock.key`` is left deleted.
        """
        with self._lock:
            self._unlock(password)
            self._clear_lock_latch()
            if self.unlock_mode == "auto" and self.unlock_path.exists():
                if self._unlock_key_awaits_app():
                    # Sealed under the desktop app's key, which this gateway
                    # was not given: unlocked for this session, and the file
                    # stays the app's. Sealing again here would put the
                    # password back under a key on disk.
                    log.info("vault.unlock_key_left_for_app")
                elif not self._unlock_key_opens_to(password):
                    try:
                        self._store_unlock_key(password)
                    except (OSError, VaultError) as exc:
                        log.warning("vault.unlock_key_reseal_failed", error=type(exc).__name__)
                    else:
                        log.warning("vault.unlock_key_resealed")

    def _unlock(self, password: str) -> None:
        """Decrypt every key into memory. Leaves an operator's lock latch as it is."""
        password = _require_password(password)
        with self._lock:
            secret = self._open_verifier(password)
            keys: dict[str, bytes] = {}
            for record in self.list():
                path = self.keystore_path(record.address)
                if not path.is_file():
                    continue
                keystore = json.loads(path.read_text(encoding="utf-8"))
                keys[record.key] = self._decrypt(keystore, password)
            index = self._read_index()
            seal, changed = self._ledger_seal(index)
            if changed:
                self._write_index(index)
            self._keys = keys
            self._password = password
            register_secret_values(password)
            self._seal_key = _derive_seal_key(secret, seal)

    def _clear_lock_latch(self) -> None:
        """Lift the operator's lock latch; only an explicit, password-proven call does this."""
        index = self._read_index()
        if index.get(LOCK_LATCH):
            index[LOCK_LATCH] = False
            self._write_index(index)

    def _ledger_seal(self, index: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """The index's seal salt; ``True`` when it had to be created (``index`` is changed)."""
        stored = index.get(LEDGER_SEAL)
        if isinstance(stored, dict) and _valid_ledger_seal(stored):
            return stored, False
        # A vault from before ledger seals: start sealing now, and let the
        # service vouch (once) for the rows that are already live.
        seal = index[LEDGER_SEAL] = _new_ledger_seal()
        self._seal_new = True
        return seal, True

    def lock(self) -> None:
        """Drop the keys and latch the lock: nothing auto-unlocks until :meth:`unlock`.

        ``auto`` mode would otherwise read ``unlock.key`` again on the next
        status call or agent swap and undo the lock at once. The latch lives in
        ``index.json`` so a gateway restart does not undo it either.
        """
        with self._lock:
            self._keys = {}
            self._password = None
            self._seal_key = None
            if self.initialized:
                index = self._read_index()
                if not index.get(LOCK_LATCH):
                    index[LOCK_LATCH] = True
                    self._write_index(index)

    def try_auto_unlock(self) -> bool:
        """Unlock from ``unlock.key`` when in ``auto`` mode. Never raises.

        An operator's :meth:`lock` outranks the file: a latched vault stays locked.
        """
        with self._lock:
            if self.unlocked:
                return True
            if not self.initialized or self.operator_locked:
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
                    password = unlock_key.open_password(envelope)
                    self._unlock(password)
                    self._reseal_unlock_key_if_due(envelope, password)
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

    def _unlock_key_awaits_app(self) -> bool:
        """``unlock.key`` is sealed under the desktop app's key and this gateway lacks it."""
        return unlock_key.awaits_handoff(self._read_unlock_key())

    def _reseal_unlock_key_if_due(self, envelope: unlock_key.Envelope, password: str) -> None:
        """Seal again when the key that just opened ``unlock.key`` should not stay.

        ``AGENTOS_VAULT_KEK_DIR`` set (or changed) on a vault that already
        auto-unlocks: the old key still opens it, once, and the fresh seal
        puts the key where the operator now wants it and deletes the old one.
        Likewise a key on disk once the desktop app hands the gateway its
        own: the fresh seal goes under the app's key and the file is deleted.
        """
        if not unlock_key.should_reseal(envelope):
            return
        try:
            self._store_unlock_key(password)
        except (OSError, VaultError) as exc:
            log.warning("vault.unlock_key_move_failed", error=type(exc).__name__)
            return
        log.warning("vault.unlock_key_moved", protection=self._unlock_key_protection())

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
            # An explicit, password-proven operator action: it lifts the lock
            # latch like ``unlock`` does.
            if not self.unlocked:
                self._unlock(password)
            self._clear_lock_latch()

    def change_password(self, password: str, new_password: str) -> None:
        """Re-encrypt every keystore and the verifier under *new_password*.

        All or nothing across the files. The new keystores are staged beside
        the live ones, then one ``index.json`` write carries the new verifier
        and the list of wallets being moved: that write is the commit. Before
        it the old password opens everything and the staged files are dropped;
        after it the new one does, and whoever next proves the password moves
        the staged files into place (:meth:`_settle_rekey`). Rewriting the
        live keystores one by one left a failure in between (a full disk, a
        kill) with wallets under both passwords, and no password unlocked.
        """
        new_password = _require_password(new_password)
        with self._lock:
            secret = self._open_verifier(password)
            index = self._read_index()
            records = [self._record(raw) for raw in index["wallets"]]
            keys: dict[str, bytes] = {}
            for record in records:
                path = self.keystore_path(record.address)
                keystore = json.loads(path.read_text(encoding="utf-8"))
                keys[record.key] = self._decrypt(keystore, password)
            try:
                for record in records:
                    _write_private(
                        self._rekey_path(record.address),
                        json.dumps(self._encrypt(keys[record.key], new_password)),
                    )
                # The same secret under the new password: the ledger seal key
                # is derived from it, and every live order and mandate stays
                # sealed.
                index["verifier"] = self._encrypt(secret, new_password)
                seal, _created = self._ledger_seal(index)
                index[LOCK_LATCH] = False
                index[REKEY_PENDING] = [record.address for record in records]
                self._write_index(index)
            except BaseException:
                # Not committed: the old password still opens every keystore.
                try:
                    self._settle_rekey()
                except Exception as exc:  # the next unlock drops what is left
                    log.warning("vault.rekey_cleanup_failed", error=type(exc).__name__)
                raise
            # Committed. This process holds the new password from here on,
            # whatever happens to the moves below.
            self._keys = keys
            self._password = new_password
            register_secret_values(new_password)
            self._seal_key = _derive_seal_key(secret, seal)
            try:
                self._settle_rekey()
            except (OSError, VaultError) as exc:
                # The keys are in memory and the next password-proven call
                # retries the move.
                log.warning("vault.rekey_finish_deferred", error=type(exc).__name__)
            # Under a manual policy the stored password is not refreshed: the
            # old one no longer opens anything, and writing the new one would
            # be the very thing the operator configured away.
            if index.get("unlock_mode") == "auto" and self.policy_mode() != "manual":
                if self._unlock_key_awaits_app():
                    # The app's key is not here to seal the new password with,
                    # and a key on disk is what the app's replaced. The stored
                    # one is stale until the next unlock in the app seals again.
                    log.warning(
                        "vault.unlock_key_stale",
                        hint="unlock once in the desktop app to store the new password",
                    )
                else:
                    self._store_unlock_key(new_password)

    def _settle_rekey(self) -> None:
        """Finish a committed password change, or drop what an uncommitted one staged.

        ``index.json`` decides which: while it lists ``rekey_pending`` wallets
        its verifier is already the new one, so each staged keystore replaces
        the live one (a missing staged file was moved before the interruption).
        Without the list, staged files belong to a change that never
        committed and are deleted.
        """
        with self._lock:
            if not self.initialized:
                return
            index = self._read_index()
            pending = index.get(REKEY_PENDING)
            if isinstance(pending, list):
                for address in pending:
                    staged = self._rekey_path(str(address))
                    if staged.is_file():
                        os.replace(staged, self.keystore_path(str(address)))
                fsync_dir(self.root)
                del index[REKEY_PENDING]
                self._write_index(index)
                log.info("vault.rekey_finished", wallets=len(pending))
            elif REKEY_PENDING in index:
                del index[REKEY_PENDING]
                self._write_index(index)
            for stray in self.root.glob(f"*.json{REKEY_SUFFIX}"):
                stray.unlink(missing_ok=True)

    # ── ledger seal (agentos.trading.integrity) ────────────────────────

    def ledger_seal_key(self) -> bytes | None:
        """The key the ledger's rows are sealed with; ``None`` while locked.

        Derived from the verifier's secret (encrypted under the vault password,
        so as well protected as the wallet keys) and a per-vault salt in
        ``index.json``. It is never written anywhere.
        """
        return self._seal_key

    def take_new_seal(self) -> bool:
        """``True`` once after an unlock started sealing an existing vault (adopt its rows)."""
        with self._lock:
            new, self._seal_new = self._seal_new, False
            return new

    def incarnation(self, address: str) -> str | None:
        """Which time the vault learned this key: a removal and re-import is a new one.

        A random id written when the wallet was added; a wallet added before
        that id existed answers its creation time instead. ``None`` for an
        address the vault does not hold.
        """
        try:
            key = normalize_address(address)
            index = self._read_index()
        except (VaultError, ValueError):
            return None
        for raw in index.get("wallets", []):
            if normalize_address(str(raw.get("address") or "")) == key:
                stored = raw.get("incarnation")
                if isinstance(stored, str) and stored:
                    return stored
                return f"created:{float(raw.get('created_at') or 0.0)!r}"
        return None

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
        address = checksum_address(account.address)
        record = WalletRecord(
            address=address,
            label=(label or "").strip() or default_label(address),
            created_at=time.time(),
            imported=imported,
        )
        index = self._read_index()
        if any(normalize_address(w["address"]) == record.key for w in index["wallets"]):
            raise WalletExistsError(f"wallet {record.address} already exists")
        _write_private(self.keystore_path(record.address), json.dumps(self._encrypt(key, password)))
        # A fresh id each time the vault learns this key (``incarnation``).
        index["wallets"].append({**self._raw(record), "incarnation": secrets.token_hex(16)})
        if not index.get("primary"):
            index["primary"] = record.address
        self._write_index(index)
        self._keys[record.key] = bytes(key)
        return record

    def create(self, label: str) -> WalletRecord:
        with self._lock:
            key = secrets.token_bytes(32)
            return self._add(key, label, imported=False)

    def create_batch(self, count: int, label: str = "") -> builtins.list[MintedWallet]:
        """Make *count* wallets, each from its own fresh 12-word recovery phrase.

        Each key is the phrase's first account at ``m/44'/60'/0'/0/0``, so any
        wallet app that restores the phrase lands on the same address. The
        vault stores the key, never the phrase: the returned
        :class:`MintedWallet` rows are the only copy of it.

        A *label* names them ``"<label> 1"`` … ``"<label> N"``; without one each
        gets :func:`default_label`. The keystores are encrypted outside the vault
        lock (scrypt is slow and signing must not wait on it), then written and
        recorded in one ``index.json`` write: either every wallet is added or
        none is.
        """
        from eth_account import Account
        from eth_account.hdaccount import (
            ETHEREUM_DEFAULT_PATH,
            Language,
            generate_mnemonic,
            key_from_seed,
            seed_from_mnemonic,
        )

        if isinstance(count, bool) or not isinstance(count, int):
            raise ValueError("count must be a whole number")
        if not 1 <= count <= MAX_BATCH_CREATE:
            raise ValueError(f"count must be between 1 and {MAX_BATCH_CREATE}")
        base = (label or "").strip()
        with self._lock:
            password = self._require_unlocked()
        made: list[tuple[MintedWallet, bytes, dict[str, Any]]] = []
        for n in range(1, count + 1):
            mnemonic = generate_mnemonic(12, Language.ENGLISH)
            key = key_from_seed(seed_from_mnemonic(mnemonic, ""), ETHEREUM_DEFAULT_PATH)
            address = checksum_address(Account.from_key(key).address)
            record = WalletRecord(
                address=address,
                label=f"{base} {n}" if base else default_label(address),
                created_at=time.time(),
                imported=False,
            )
            minted = MintedWallet(record=record, private_key="0x" + key.hex(), mnemonic=mnemonic)
            made.append((minted, bytes(key), self._encrypt(key, password)))
        with self._lock:
            if self._require_unlocked() != password:
                raise VaultError(
                    "the vault password changed while the wallets were being made; none were added"
                )
            index = self._read_index()
            known = {normalize_address(w["address"]) for w in index["wallets"]}
            for minted, _key, _keystore in made:
                if minted.record.key in known:  # pragma: no cover - 2**-128
                    raise WalletExistsError(f"wallet {minted.record.address} already exists")
                known.add(minted.record.key)
            written: list[Path] = []
            try:
                for minted, _key, keystore in made:
                    path = self.keystore_path(minted.record.address)
                    _write_private(path, json.dumps(keystore))
                    written.append(path)
                    index["wallets"].append(
                        {**self._raw(minted.record), "incarnation": secrets.token_hex(16)}
                    )
                if not index.get("primary"):
                    index["primary"] = made[0][0].record.address
                self._write_index(index)
            except BaseException:
                for path in written:
                    path.unlink(missing_ok=True)
                raise
            for minted, key, _keystore in made:
                self._keys[minted.record.key] = key
        return [minted for minted, _key, _keystore in made]

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

    def remove_many(self, addresses: Iterable[str], password: str) -> builtins.list[str]:
        """Remove several wallets under one password check, all or none.

        Every address must be in the vault: one that is not removes nothing,
        so a list built from a stale screen cannot half-apply. The password
        is checked once (it is the slow part, by design) and the index is
        written once. Returns the removed addresses, normalized, in order.
        """
        with self._lock:
            self.verify_password(password)
            index = self._read_index()
            keys = list(dict.fromkeys(normalize_address(a) for a in addresses))
            present = {normalize_address(raw["address"]) for raw in index["wallets"]}
            for key in keys:
                if key not in present:
                    raise WalletNotFoundError(f"no wallet {key}")
            gone = set(keys)
            index["wallets"] = [
                raw for raw in index["wallets"] if normalize_address(raw["address"]) not in gone
            ]
            if index.get("primary") and normalize_address(index["primary"]) in gone:
                index["primary"] = index["wallets"][0]["address"] if index["wallets"] else None
            self._write_index(index)
            for key in keys:
                self.keystore_path(key).unlink(missing_ok=True)
                self._keys.pop(key, None)
            return keys

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


def _new_ledger_seal() -> dict[str, Any]:
    return {"version": 1, "salt": secrets.token_hex(16), "created_at": time.time()}


def _valid_ledger_seal(seal: Any) -> bool:
    if not isinstance(seal, dict) or seal.get("version") != 1:
        return False
    try:
        return len(bytes.fromhex(str(seal.get("salt") or ""))) == 16
    except ValueError:
        return False


def _derive_seal_key(secret: bytes, seal: dict[str, Any]) -> bytes:
    """``HMAC-SHA256(verifier secret, label || salt)``: one key per vault, for one purpose."""
    salt = bytes.fromhex(str(seal["salt"]))
    return hmac.new(secret, _LEDGER_SEAL_LABEL + salt, hashlib.sha256).digest()


def _require_password(password: str) -> str:
    text = str(password or "")
    if len(text) < 8:
        raise ValueError("vault password must be at least 8 characters")
    return text
