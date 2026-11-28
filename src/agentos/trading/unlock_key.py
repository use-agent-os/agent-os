"""At-rest protection for the auto-unlock vault password (``unlock.key``).

In ``auto`` mode the gateway must open the vault after a restart with nobody
there to type the password, so the password has to be recoverable from
something on this machine. It used to sit in ``<vault>/unlock.key`` as plain
text beside the keystores it decrypts: one recursive read or copy of the
state directory (an agent's ``grep -r``, a backup, a support bundle) carried
the password and every keystore together (WAL-01).

``unlock.key`` now holds the password sealed with AES-256-GCM under a random
256-bit key-encryption key (KEK) that is **not** in the state directory:

* ``env``   -- ``AGENTOS_VAULT_KEK`` (64 hex characters), for containers and
  service managers that inject secrets. It is withheld from every child
  process (``tools.env_passthrough``), so the key is in no file and no
  subprocess, only the gateway's own memory.
* ``dpapi`` -- Windows: the KEK is sealed with the Data Protection API for the
  current user and stored under ``%LOCALAPPDATA%\\AgentOS\\vault-keys``; the
  blob is useless to another account or machine.
* ``file``  -- macOS and Linux: a 0600 file in a 0700 directory outside the
  state root (``~/Library/Application Support/AgentOS/vault-keys``,
  ``${XDG_STATE_HOME:-~/.local/state}/agentos/vault-keys``), itself on the
  sensitive-path floor.

* ``handoff`` -- the desktop app keeps the KEK in the operating system's
  keychain and hands it to the gateway it spawns, over a pipe
  (``AGENTOS_VAULT_KEK_FD``) the gateway reads once and closes at boot
  (:func:`load_handoff_kek`). The KEK is then in no file at all: reading
  every file this user owns does not open ``unlock.key``. A gateway started
  without the app (``agentos gateway run``) does not have the key, so the
  vault waits for ``agentos wallet unlock``.

``AGENTOS_VAULT_KEK_DIR`` moves the KEK directory (a separate mount). The file
alone is useless without the KEK and the KEK alone is useless without the
file. For the ``file`` store this is defence in depth, not an OS boundary: a
process running arbitrary code as the same user can read both, exactly as the
gateway does at boot. The ``handoff`` store closes that read; what remains
there is a same-user process that rewrites AgentOS's own installed files. The
boundaries that hold against that are ``agentos sandbox`` and ``manual``
unlock (nothing on disk).

A legacy plain-text ``unlock.key`` still unlocks and is re-sealed on the
spot (``vault.unlock_key_migrated``); see :mod:`agentos.trading.vault`.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import stat
import sys
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

from agentos.paths import default_vault_kek_dir, vault_kek_dir
from agentos.trading.durable import fsync_dir, fsync_file

KEK_ENV = "AGENTOS_VAULT_KEK"
#: A pipe the desktop app writes its keychain-held KEK to at spawn.
KEK_FD_ENV = "AGENTOS_VAULT_KEK_FD"
#: A 0600 file carrying the same key across an in-place re-exec of the gateway.
KEK_FILE_ENV = "AGENTOS_VAULT_KEK_FILE"
HANDOFF_BACKEND = "handoff"
#: Read at most this much from the pipe or file: 64 hex characters and a newline.
_HANDOFF_READ_LIMIT = 256
ENVELOPE_FORMAT = "agentos-unlock-key"
ENVELOPE_VERSION = 2
_ALG = "AES-256-GCM"
_KEK_BYTES = 32
_NONCE_BYTES = 12


class UnlockKeyError(RuntimeError):
    """The sealed password cannot be opened (KEK missing, file damaged)."""


@dataclass(frozen=True)
class Envelope:
    backend: str
    kek_id: str
    nonce: bytes
    ciphertext: bytes

    def to_json(self) -> str:
        return json.dumps(
            {
                "format": ENVELOPE_FORMAT,
                "version": ENVELOPE_VERSION,
                "alg": _ALG,
                "kek": self.backend,
                "kekId": self.kek_id,
                "nonce": base64.b64encode(self.nonce).decode("ascii"),
                "ciphertext": base64.b64encode(self.ciphertext).decode("ascii"),
            },
            sort_keys=True,
        )


def parse_envelope(text: str) -> Envelope | None:
    """The sealed form of *text*, or ``None`` when it is a legacy plain-text file."""
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        data = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("format") != ENVELOPE_FORMAT:
        return None
    try:
        if int(data.get("version", 0)) != ENVELOPE_VERSION or data.get("alg") != _ALG:
            raise UnlockKeyError("unlock.key uses an unsupported format version")
        kek_id = str(data["kekId"])
        if not kek_id.isalnum():
            raise UnlockKeyError("unlock.key names an invalid key id")
        return Envelope(
            backend=str(data["kek"]),
            kek_id=kek_id,
            nonce=base64.b64decode(str(data["nonce"]), validate=True),
            ciphertext=base64.b64decode(str(data["ciphertext"]), validate=True),
        )
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise UnlockKeyError("unlock.key is damaged") from exc


def _aad(kek_id: str) -> bytes:
    return f"{ENVELOPE_FORMAT}/v{ENVELOPE_VERSION}|{kek_id}".encode("ascii")


def _seal(kek: bytes, kek_id: str, plaintext: bytes) -> tuple[bytes, bytes]:
    from Crypto.Cipher import AES

    nonce = secrets.token_bytes(_NONCE_BYTES)
    cipher = AES.new(kek, AES.MODE_GCM, nonce=nonce)
    cipher.update(_aad(kek_id))
    body, tag = cipher.encrypt_and_digest(plaintext)
    return nonce, body + tag


def _open(kek: bytes, envelope: Envelope) -> bytes:
    from Crypto.Cipher import AES

    if len(envelope.ciphertext) < 16:
        raise UnlockKeyError("unlock.key is damaged")
    body, tag = envelope.ciphertext[:-16], envelope.ciphertext[-16:]
    cipher = AES.new(kek, AES.MODE_GCM, nonce=envelope.nonce)
    cipher.update(_aad(envelope.kek_id))
    try:
        return bytes(cipher.decrypt_and_verify(body, tag))
    except ValueError as exc:
        raise UnlockKeyError("unlock.key does not match its key") from exc


# ── KEK sources ─────────────────────────────────────────────────────────────


def _env_kek() -> bytes | None:
    """``AGENTOS_VAULT_KEK`` from the environment.

    Kept in this process's ``os.environ`` (the boot-time credential scrub skips
    it; ``gateway.credential_env._HANDLED_ELSEWHERE``) but withheld from every
    child, since it is in ``GATEWAY_CREDENTIAL_ENV`` which
    ``tools.env_passthrough`` strips. So the gateway can seal and open the
    vault while no subprocess ever sees the key.
    """
    raw = os.environ.get(KEK_ENV, "").strip()
    if not raw:
        return None
    try:
        value = bytes.fromhex(raw.removeprefix("0x"))
    except ValueError as exc:
        raise UnlockKeyError(f"{KEK_ENV} must be {_KEK_BYTES * 2} hex characters") from exc
    if len(value) != _KEK_BYTES:
        raise UnlockKeyError(f"{KEK_ENV} must be {_KEK_BYTES * 2} hex characters")
    return value


def _parse_kek_hex(raw: str) -> bytes | None:
    try:
        value = bytes.fromhex(raw.strip().removeprefix("0x"))
    except ValueError:
        return None
    return value if len(value) == _KEK_BYTES else None


# The key the desktop app handed over at boot. In this process's memory only:
# never in ``os.environ`` (a child would inherit it) and never in a file,
# except for the moment of an in-place re-exec (:func:`export_handoff_kek_file`).
_handoff_kek: bytes | None = None


def _read_handoff_fd(raw_fd: str) -> str | None:
    """What the app wrote to the pipe, or ``None``. Closes the pipe.

    Only a pipe is read or closed -- a FIFO, or the socket pair Node hands a
    child for a ``'pipe'`` stdio entry: a stale variable naming a descriptor
    that is something else by now (a log file, a lock) must not cost this
    process that descriptor.
    """
    try:
        fd = int(raw_fd)
    except ValueError:
        return None
    if fd < 3:
        return None
    try:
        mode = os.fstat(fd).st_mode
        if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
            return None
    except OSError:
        return None
    chunks: list[bytes] = []
    size = 0
    try:
        while size < _HANDOFF_READ_LIMIT:
            chunk = os.read(fd, _HANDOFF_READ_LIMIT - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if b"\n" in chunk:
                break
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return b"".join(chunks).decode("ascii", errors="replace")


def _read_handoff_file(raw_path: str) -> str | None:
    """The key in a one-shot file, or ``None``. Deletes the file either way."""
    path = Path(raw_path)
    try:
        with path.open("rb") as handle:
            return handle.read(_HANDOFF_READ_LIMIT).decode("ascii", errors="replace")
    except OSError:
        return None
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def load_handoff_kek(environ: MutableMapping[str, str] | None = None) -> bool:
    """Take the key the desktop app hands the gateway at boot; leave no trace.

    Both variables are removed from *environ* (default ``os.environ``) whether
    or not they yield a key, the pipe is closed and the file deleted, so no
    child process finds either. Returns whether a key is now held. A
    malformed key is ignored: the vault then behaves as if none was handed
    over. Never raises, never logs the key.
    """
    global _handoff_kek
    env = os.environ if environ is None else environ
    raw_fd = env.pop(KEK_FD_ENV, None)
    raw_path = env.pop(KEK_FILE_ENV, None)
    text = _read_handoff_fd(raw_fd) if raw_fd else None
    from_file = _read_handoff_file(raw_path) if raw_path else None
    if text is None:
        text = from_file
    kek = _parse_kek_hex(text) if text else None
    if kek is not None:
        _handoff_kek = kek
    return _handoff_kek is not None


def handoff_kek_held() -> bool:
    """Whether the desktop app's key is in this process's memory."""
    return _handoff_kek is not None


def clear_handoff_kek() -> None:
    """Forget the handed-over key. For tests."""
    global _handoff_kek
    _handoff_kek = None


def export_handoff_kek_file(directory: Path) -> Path | None:
    """Write the handed-over key to a fresh 0600 file for an in-place re-exec.

    The pipe it arrived on is closed, so a gateway that replaces its own image
    has to pass the key on or its vault stays locked until the app restarts
    it. The new image reads and deletes the file at boot
    (:func:`load_handoff_kek`). *directory* is the vault directory: 0700 and
    on the sensitive-path floor. Returns ``None`` when no key is held.
    """
    if _handoff_kek is None:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(directory, stat.S_IRWXU)
    path = directory / f"vault-kek.reexec-{secrets.token_hex(8)}"
    _write_new_private(path, _handoff_kek.hex().encode("ascii") + b"\n")
    return path


def _kek_path(kek_id: str, backend: str) -> Path:
    suffix = ".dpapi" if backend == "dpapi" else ".key"
    return vault_kek_dir() / f"{kek_id}{suffix}"


def _kek_locations(kek_id: str, backend: str) -> list[Path]:
    """Where a stored KEK may be: the configured directory, then the default one.

    A vault sealed before ``AGENTOS_VAULT_KEK_DIR`` was set has its key in the
    default directory. It is still read from there (and moved by the vault's
    next seal) so that pointing the variable at a separate mount does not
    cost the operator auto unlock.
    """
    current = _kek_path(kek_id, backend)
    legacy = default_vault_kek_dir() / current.name
    return [current] if legacy == current else [current, legacy]


def _secure_kek_dir() -> Path:
    directory = vault_kek_dir()
    directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(directory, stat.S_IRWXU)
    return directory


def _write_new_private(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            fsync_file(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent)


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.c_void_p)]

    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def _dpapi(data: bytes, entropy: bytes, *, protect: bool) -> bytes:
        """``CryptProtectData`` / ``CryptUnprotectData`` for the current user."""
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        call.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR if protect else ctypes.c_void_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        call.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        data_buffer = ctypes.create_string_buffer(data, len(data))
        entropy_buffer = ctypes.create_string_buffer(entropy, len(entropy))
        data_in = _DataBlob(len(data), ctypes.cast(data_buffer, ctypes.c_void_p))
        extra = _DataBlob(len(entropy), ctypes.cast(entropy_buffer, ctypes.c_void_p))
        out = _DataBlob()
        description = "AgentOS vault key" if protect else None
        ok = call(
            ctypes.byref(data_in),
            description,
            ctypes.byref(extra),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out),
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), "Windows Data Protection API call failed")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(out.pbData)

    def dpapi_available() -> bool:
        return True

else:

    def _dpapi(data: bytes, entropy: bytes, *, protect: bool) -> bytes:
        raise OSError("the Windows Data Protection API is not available on this platform")

    def dpapi_available() -> bool:
        return False


def _load_kek(envelope: Envelope) -> bytes:
    if envelope.backend == "env":
        kek = _env_kek()
        if kek is None:
            raise UnlockKeyError(f"{KEK_ENV} is not set")
        return kek
    if envelope.backend == HANDOFF_BACKEND:
        if _handoff_kek is None:
            raise UnlockKeyError(
                "the desktop app keeps this vault's unlock key and this gateway "
                "was not started by it"
            )
        return _handoff_kek
    if envelope.backend not in ("file", "dpapi"):
        raise UnlockKeyError(f"unlock.key names an unknown key store {envelope.backend!r}")
    raw = None
    for path in _kek_locations(envelope.kek_id, envelope.backend):
        try:
            raw = path.read_bytes()
            break
        except OSError:
            continue
    if raw is None:
        raise UnlockKeyError("the vault's unlock key is missing")
    if envelope.backend == "dpapi":
        try:
            raw = _dpapi(raw, envelope.kek_id.encode("ascii"), protect=False)
        except Exception as exc:  # ctypes raises more than OSError
            raise UnlockKeyError("the vault's unlock key cannot be opened") from exc
    if len(raw) != _KEK_BYTES:
        raise UnlockKeyError("the vault's unlock key is damaged")
    return raw


def _new_kek() -> tuple[str, bytes, str]:
    """``(backend, kek, kek_id)`` for a fresh seal, stored where it belongs."""
    kek_id = secrets.token_hex(16)
    env_kek = _env_kek()
    if env_kek is not None:
        return "env", env_kek, kek_id
    if _handoff_kek is not None:
        return HANDOFF_BACKEND, _handoff_kek, kek_id
    kek = secrets.token_bytes(_KEK_BYTES)
    _secure_kek_dir()
    if dpapi_available():
        try:
            sealed = _dpapi(kek, kek_id.encode("ascii"), protect=True)
        except Exception:  # ctypes raises more than OSError; the file store still works
            sealed = b""
        if sealed:
            _write_new_private(_kek_path(kek_id, "dpapi"), sealed)
            return "dpapi", kek, kek_id
    _write_new_private(_kek_path(kek_id, "file"), kek)
    return "file", kek, kek_id


def seal_password(password: str) -> str:
    """The ``unlock.key`` text for *password*; stores a new KEK as a side effect."""
    backend, kek, kek_id = _new_kek()
    nonce, ciphertext = _seal(kek, kek_id, password.encode("utf-8"))
    return Envelope(backend, kek_id, nonce, ciphertext).to_json()


def open_password(envelope: Envelope) -> str:
    """The password sealed in *envelope*. Raises :class:`UnlockKeyError`."""
    try:
        return _open(_load_kek(envelope), envelope).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnlockKeyError("unlock.key is damaged") from exc


def discard_kek(text: str | None) -> None:
    """Delete the stored KEK an ``unlock.key`` text names (never raises)."""
    if not text:
        return
    try:
        envelope = parse_envelope(text)
    except UnlockKeyError:
        return
    if envelope is None or envelope.backend not in ("file", "dpapi"):
        return
    for path in _kek_locations(envelope.kek_id, envelope.backend):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def kek_in_place(envelope: Envelope) -> bool:
    """Whether *envelope*'s stored KEK is in the configured directory.

    ``False`` only for a file or DPAPI key that exists in the default
    directory while ``AGENTOS_VAULT_KEK_DIR`` names another.
    """
    if envelope.backend not in ("file", "dpapi"):
        return True
    locations = _kek_locations(envelope.kek_id, envelope.backend)
    return locations[0].is_file() or not any(path.is_file() for path in locations[1:])


def should_reseal(envelope: Envelope) -> bool:
    """Whether an ``unlock.key`` that just opened belongs under a better key.

    ``True`` for a key on disk while the desktop app's key is held (the fresh
    seal goes under the app's key and the file is deleted), and for a stored
    key found outside the configured directory (:func:`kek_in_place`). A key
    the operator injects (``AGENTOS_VAULT_KEK``) stays where it was put.
    """
    if envelope.backend in ("file", "dpapi") and _handoff_kek is not None and _env_kek() is None:
        return True
    return not kek_in_place(envelope)


def awaits_handoff(text: str | None) -> bool:
    """Whether *text* (an ``unlock.key``) is sealed under an app key not held here.

    Such a file is the app's to open: a gateway without the key leaves it as
    it is instead of sealing the password again under a key on disk.
    """
    if not text or _handoff_kek is not None:
        return False
    try:
        envelope = parse_envelope(text)
    except UnlockKeyError:
        return False
    return envelope is not None and envelope.backend == HANDOFF_BACKEND


__all__ = [
    "ENVELOPE_FORMAT",
    "HANDOFF_BACKEND",
    "KEK_ENV",
    "KEK_FD_ENV",
    "KEK_FILE_ENV",
    "Envelope",
    "UnlockKeyError",
    "awaits_handoff",
    "clear_handoff_kek",
    "discard_kek",
    "export_handoff_kek_file",
    "handoff_kek_held",
    "kek_in_place",
    "dpapi_available",
    "load_handoff_kek",
    "open_password",
    "parse_envelope",
    "seal_password",
    "should_reseal",
]
