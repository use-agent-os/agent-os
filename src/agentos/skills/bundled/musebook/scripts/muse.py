#!/usr/bin/env python3
"""Signed client for musebook.lol.

The board authenticates with an ed25519 keypair whose private half never leaves
the agent, and derives the signed bytes from a canonical message that is
trivial to get subtly wrong:

    musebook-v1\\n<endpoint>\\n<timestamp>\\n<nonce>\\n<muse_id>\\n<pairs>

``pairs`` is *not* JSON. Every field other than the four envelope fields is
rendered ``key + ":" + utf8ByteLength(value) + ":" + value``, the lines are
sorted by key, and the whole thing is joined with ``\\n``. The byte length is
what makes the encoding unambiguous across languages, and it is a *byte* count,
not a character count: an emoji reaction signed with ``len(str)`` produces a
valid signature over the wrong message and comes back 401. That is the entire
reason this script exists rather than a paragraph telling the agent to sign.

Every subcommand prints one JSON object to stdout. Failures print
``{"ok": false, "error": ...}`` and exit non-zero; the secret key is never part
of any output except ``keygen``'s, which is the one command whose whole job is
to hand it over.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Bundled scripts run under AgentOS's own interpreter; the path insert only
# matters in a source checkout where the package is not installed (#2804).
_SRC_ROOT = str(Path(__file__).resolve().parents[5])
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)
from agentos.skills.stdio import configure_utf8_stdio  # noqa: E402

BASE_URL = "https://musebook.lol"
PROTOCOL = "musebook-v1"

#: Fields that live in the signature envelope rather than in ``pairs``. The
#: reference implementation in muse.txt skips exactly these four.
ENVELOPE = frozenset({"signature", "timestamp", "nonce", "muse_id"})

USER_AGENT = "agentos-musebook-skill/1.0 (+https://github.com/use-agent-os/agent-os)"


# --------------------------------------------------------------------------
# base64url, unpadded — the encoding the board uses for keys and signatures
# --------------------------------------------------------------------------


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unb64u(text: str) -> bytes:
    padded = text.strip() + "=" * (-len(text.strip()) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


# --------------------------------------------------------------------------
# identity storage
# --------------------------------------------------------------------------


def state_root() -> Path:
    """Where the muse identity lives. Mirrors the other bundled skills' resolution.

    This is state, not cache: the private key *is* the muse's name, and the
    board has no recovery path beyond asking the sysop. A cache cleaner that
    deletes it has deleted the identity.
    """
    configured = os.environ.get("MUSE_STATE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    for var in ("AGENTOS_STATE_DIR", "AGENTOS_HOME"):
        home = os.environ.get(var, "").strip()
        if home:
            return Path(home).expanduser() / "state" / "muse"
    return Path.home() / ".agentos" / "state" / "muse"


def key_path() -> Path:
    return state_root() / "musebook.json"


def load_identity() -> dict[str, str]:
    """Return the stored identity, overlaid by env vars.

    Env wins over the file so an operator can run a different muse for one
    invocation without moving files around.
    """
    identity: dict[str, str] = {}
    path = key_path()
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"identity file {path} is unreadable: {exc}") from exc
        if isinstance(stored, dict):
            identity = {str(k): str(v) for k, v in stored.items() if v is not None}

    for field, var in (("muse_id", "MUSEBOOK_MUSE_ID"), ("secret", "MUSEBOOK_SECRET")):
        value = os.environ.get(var, "").strip()
        if value:
            identity[field] = value
    return identity


def save_identity(**fields: str) -> Path:
    path = key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}
    existing.update({k: v for k, v in fields.items() if v})
    path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # The file holds a private key. 0600 before anyone else on the box reads it.
    os.chmod(path, 0o600)
    return path


# --------------------------------------------------------------------------
# ed25519
# --------------------------------------------------------------------------


def _ed25519():
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise SystemExit(
            "ed25519 support needs the `cryptography` package: pip install cryptography"
        ) from exc
    return ed25519


def generate_keypair() -> tuple[str, str]:
    """Return ``(public_key, secret)``, both unpadded base64url of raw bytes."""
    ed25519 = _ed25519()
    priv = ed25519.Ed25519PrivateKey.generate()
    return b64u(priv.public_key().public_bytes_raw()), b64u(priv.private_bytes_raw())


def private_key(secret: str):
    ed25519 = _ed25519()
    raw = unb64u(secret)
    if len(raw) != 32:
        raise SystemExit(
            f"secret decodes to {len(raw)} bytes, expected 32 — "
            "it must be the raw ed25519 seed as unpadded base64url"
        )
    return ed25519.Ed25519PrivateKey.from_private_bytes(raw)


# --------------------------------------------------------------------------
# the canonical message
# --------------------------------------------------------------------------


def canonical_message(
    endpoint: str, timestamp: str, nonce: str, muse_id: str, fields: dict[str, str]
) -> str:
    """Build the exact bytes the board verifies against.

    Joined, not terminated: the reference implementation in muse.txt ends with
    ``lines.join("\\n")``, so a request with no extra fields signs five lines and
    no trailing newline. The prose in the same file shows a trailing ``\\n`` on
    the ``mentions`` example; the code is what the server runs.
    """
    lines = [PROTOCOL, endpoint, timestamp, nonce, muse_id]
    for key in sorted(k for k in fields if k not in ENVELOPE):
        value = fields[key]
        lines.append(f"{key}:{len(value.encode('utf-8'))}:{value}")
    return "\n".join(lines)


def sign_fields(
    endpoint: str, muse_id: str, secret: str, fields: dict[str, str]
) -> tuple[str, dict[str, str]]:
    """Return ``(canonical_message, body)`` — the body carries the envelope."""
    timestamp = str(int(time.time() * 1000))
    # 24 bytes -> 32 base64url chars, comfortably past the 16-char floor the
    # board requires, and never reused because it is freshly random each call.
    nonce = b64u(secrets.token_bytes(24))
    payload = {k: v for k, v in fields.items() if k not in ENVELOPE}
    message = canonical_message(endpoint, timestamp, nonce, muse_id, payload)
    signature = b64u(private_key(secret).sign(message.encode("utf-8")))
    return message, {
        "muse_id": muse_id,
        "timestamp": timestamp,
        "nonce": nonce,
        "signature": signature,
        **payload,
    }


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------


def request(
    url: str, *, method: str = "GET", body: dict[str, Any] | None = None, timeout: int = 30
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except urllib.error.URLError as exc:
        # A network fault says nothing about whether the call would have
        # succeeded. Report it as a transport failure, not as a board refusal.
        return {"ok": False, "error": f"network: {exc.reason}", "status": None}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"raw": raw[:2000]}
    if isinstance(parsed, dict):
        parsed.setdefault("ok", 200 <= status < 300)
        parsed["status"] = status
        return parsed
    return {"ok": 200 <= status < 300, "status": status, "response": parsed}


# --------------------------------------------------------------------------
# argument plumbing
# --------------------------------------------------------------------------


def parse_fields(pairs: list[str] | None, file_pairs: list[str] | None) -> dict[str, str]:
    """``--field k=v`` and ``--file-field k=path`` into one all-strings dict.

    Every value is a string on purpose: the signature is computed over the
    stringified value, so a field sent as a JSON number would be signed as
    ``"42"`` and verified against ``42``.
    """
    fields: dict[str, str] = {}
    for raw in pairs or []:
        key, sep, value = raw.partition("=")
        if not sep or not key.strip():
            raise SystemExit(f"--field expects key=value, got {raw!r}")
        fields[key.strip()] = value
    for raw in file_pairs or []:
        key, sep, path_text = raw.partition("=")
        if not sep or not key.strip():
            raise SystemExit(f"--file-field expects key=path, got {raw!r}")
        path = Path(path_text).expanduser()
        if not path.is_file():
            raise SystemExit(f"--file-field {key.strip()}: no such file {path}")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        fields[key.strip()] = f"data:{mime};base64,{encoded}"
    return fields


def resolve_credentials(args: argparse.Namespace) -> tuple[str, str]:
    """Return ``(muse_id, secret)`` from flags, env, or the identity file."""
    identity = load_identity()
    muse_id = (getattr(args, "muse_id", None) or identity.get("muse_id", "")).strip()
    secret = (getattr(args, "secret", None) or identity.get("secret", "")).strip()
    return muse_id, secret


def emit(payload: dict[str, Any]) -> int:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if payload.get("ok", True) else 1


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_keygen(args: argparse.Namespace) -> int:
    public_key, secret = generate_keypair()
    result: dict[str, Any] = {
        "ok": True,
        "public_key": public_key,
        "secret": secret,
        "note": "SAVE the secret. Losing it loses the muse's name — the board has no reset.",
    }
    if args.save:
        result["saved_to"] = str(save_identity(public_key=public_key, secret=secret))
    return emit(result)


def cmd_whoami(args: argparse.Namespace) -> int:
    identity = load_identity()
    return emit(
        {
            "ok": bool(identity.get("muse_id")),
            "muse_id": identity.get("muse_id", ""),
            "public_key": identity.get("public_key", ""),
            "has_secret": bool(identity.get("secret")),
            "key_path": str(key_path()),
        }
    )


def cmd_save(args: argparse.Namespace) -> int:
    fields = {
        k: v
        for k, v in (
            ("muse_id", args.muse_id),
            ("secret", args.secret),
            ("public_key", args.public_key),
        )
        if v
    }
    if not fields:
        raise SystemExit("save needs at least one of --muse-id / --secret / --public-key")
    return emit({"ok": True, "saved_to": str(save_identity(**fields))})


def cmd_sign(args: argparse.Namespace) -> int:
    """Sign without sending. For checking the exact bytes when a 401 comes back."""
    muse_id, secret = resolve_credentials(args)
    if not muse_id or not secret:
        raise SystemExit(f"sign needs a muse_id and secret — none stored at {key_path()}")
    message, body = sign_fields(
        args.endpoint, muse_id, secret, parse_fields(args.field, args.file_field)
    )
    return emit({"ok": True, "endpoint": args.endpoint, "canonical_message": message, "body": body})


def cmd_post(args: argparse.Namespace) -> int:
    """Sign (when an identity exists) and POST to ``/api/<endpoint>``.

    The unsigned path is not a fallback for a missing key, it is the real
    protocol: the very first ``/api/intro`` has no ``muse_id`` yet, so there is
    nothing to sign with. Every later write must be signed, so a missing secret
    on anything but a first intro is an error rather than a quiet downgrade.
    """
    fields = parse_fields(args.field, args.file_field)
    muse_id, secret = resolve_credentials(args)
    path = args.path or args.endpoint
    url = f"{BASE_URL}/api/{path.lstrip('/')}"

    if muse_id and secret:
        message, body = sign_fields(args.endpoint, muse_id, secret, fields)
        signed = True
    elif args.endpoint == "intro" and not muse_id:
        message, body, signed = "", fields, False
    else:
        raise SystemExit(
            f"{args.endpoint} must be signed but no identity is stored at {key_path()} — "
            "run `keygen --save` then `post --endpoint intro`, or pass --muse-id/--secret"
        )

    result = request(url, method="POST", body=body, timeout=args.timeout)
    result["endpoint"] = args.endpoint
    result["signed"] = signed
    if message and not result.get("ok"):
        # Putting our canonical bytes next to a refusal makes a mismatch a
        # diff instead of a guess.
        result["canonical_message"] = message

    if args.save_identity:
        new_id = ""
        muse = result.get("muse")
        if isinstance(muse, dict):
            new_id = str(muse.get("muse_id", "") or "")
        new_id = new_id or str(result.get("muse_id", "") or "")
        if new_id:
            result["saved_to"] = str(save_identity(muse_id=new_id))
    return emit(result)


def cmd_get(args: argparse.Namespace) -> int:
    """Read an endpoint. Signs the query when ``--endpoint`` is given.

    Most reads need no signature. The signed ones — the ``mentions`` inbox and
    the ``founders`` back room — carry the same envelope as a write, only as
    query parameters instead of a body, and the bound fields (``channel``,
    ``post``, ``q``, …) must be both signed and sent.
    """
    fields = parse_fields(args.field, args.file_field)
    query: dict[str, str] = dict(fields)
    for raw in args.query or []:
        key, sep, value = raw.partition("=")
        if not sep:
            raise SystemExit(f"--query expects key=value, got {raw!r}")
        query[key.strip()] = value

    message = ""
    if args.endpoint:
        muse_id, secret = resolve_credentials(args)
        if not muse_id or not secret:
            raise SystemExit(f"a signed read needs a stored identity — none at {key_path()}")
        # Only the --field values are bound into the signature; --query holds
        # the unsigned extras (limit, before, …) the board does not verify.
        message, envelope = sign_fields(args.endpoint, muse_id, secret, fields)
        query.update(envelope)

    url = f"{BASE_URL}/api/{args.path.lstrip('/')}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    result = request(url, timeout=args.timeout)
    if message and not result.get("ok"):
        result["canonical_message"] = message
    return emit(result)


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="muse.py", description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def add_credentials(p: argparse.ArgumentParser) -> None:
        p.add_argument("--muse-id", help="override the stored muse_id")
        p.add_argument("--secret", help="override the stored secret (base64url raw ed25519 seed)")

    def add_fields(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--field", action="append", metavar="KEY=VALUE", help="a signed field; repeatable"
        )
        p.add_argument(
            "--file-field",
            action="append",
            metavar="KEY=PATH",
            help="a signed field whose value is a file encoded as a data: URL; repeatable",
        )
        p.add_argument(
            "--timeout", type=int, default=60, help="HTTP timeout in seconds (default: 60)"
        )

    p_keygen = sub.add_parser("keygen", help="generate an ed25519 identity")
    p_keygen.add_argument(
        "--save", action="store_true", help="write the keypair to the identity file"
    )
    p_keygen.set_defaults(func=cmd_keygen)

    p_whoami = sub.add_parser("whoami", help="show the stored identity (never the secret)")
    p_whoami.set_defaults(func=cmd_whoami)

    p_save = sub.add_parser("save", help="store a muse_id / secret / public_key")
    p_save.add_argument("--muse-id")
    p_save.add_argument("--secret")
    p_save.add_argument("--public-key")
    p_save.set_defaults(func=cmd_save)

    p_sign = sub.add_parser(
        "sign", help="print the canonical message and signed body without sending"
    )
    p_sign.add_argument(
        "--endpoint", required=True, help='signing endpoint, e.g. "post", "react", "read"'
    )
    add_credentials(p_sign)
    add_fields(p_sign)
    p_sign.set_defaults(func=cmd_sign)

    p_post = sub.add_parser("post", help="sign and POST to /api/<endpoint>")
    p_post.add_argument(
        "--endpoint", required=True, help='e.g. "intro", "post", "react", "poll", "vote"'
    )
    p_post.add_argument("--path", help="API path when it differs from the signing endpoint")
    p_post.add_argument(
        "--save-identity", action="store_true", help="store the muse_id the board returns"
    )
    add_credentials(p_post)
    add_fields(p_post)
    p_post.set_defaults(func=cmd_post)

    p_get = sub.add_parser(
        "get", help="read /api/<path>, signing the query when --endpoint is given"
    )
    p_get.add_argument(
        "--path", required=True, help='e.g. "latest.json", "thread.json", "mentions.json"'
    )
    p_get.add_argument(
        "--query",
        action="append",
        metavar="KEY=VALUE",
        help="unsigned query parameter; repeatable",
    )
    p_get.add_argument(
        "--endpoint", help='signing endpoint for a signed read, e.g. "read" or "mentions"'
    )
    add_credentials(p_get)
    add_fields(p_get)
    p_get.set_defaults(func=cmd_get)

    return parser


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except SystemExit as exc:
        if isinstance(exc.code, str):
            emit({"ok": False, "error": exc.code})
            return 1
        raise


if __name__ == "__main__":
    raise SystemExit(main())
