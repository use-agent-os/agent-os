"""Token metadata: build the JSON, and turn it into a `metadataUri`.

``PartyToken`` stores one immutable string, ``metadataUri``, and every pools.fun
front end reads the token's identity from whatever it points at. Three forms are
in use on-chain today, and this module can produce all three:

* ``ipfs://<cid>`` — what the reference launch used.
* ``https://…`` — any host you control.
* ``data:application/json;base64,…`` — the whole JSON inlined in calldata. One
  live launch does exactly this.

**`PINATA_JWT` is optional and must stay that way.** A launch with no token image
needs no secret, no account, and no network round trip beyond the chain itself:
the metadata JSON goes inline as a data URI. Pinata is required only for the one
thing that genuinely cannot be inlined — an image, which is far too large for
calldata.

The one hard rule here is that failures are loud. If a caller asks for an image
and Pinata is not configured, this raises. It never quietly launches an
image-less token, because the token's identity is immutable the moment the
transaction lands and there is no second chance to attach the picture.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .chains import ENV_PINATA_JWT, pinata_jwt

PINATA_API = "https://api.pinata.cloud"
PINATA_GATEWAY = "https://gateway.pinata.cloud/ipfs/"
USER_AGENT = "poolsdotfun-token-launcher/1.0"

# Pinata's own limit is far higher, but a token logo has no business being large,
# and catching it here beats a slow upload that fails at the far end.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# Inline metadata is paid for in calldata gas on every launch, forever. A logo URL
# plus a sentence of description fits easily; anything past this is a sign the
# caller meant to pin instead.
MAX_INLINE_URI_BYTES = 8 * 1024

_IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".avif": "image/avif",
}

# Magic-byte sniffing, because AgentOS webchat attachments routinely arrive with a
# generic or missing extension and the suffix alone would mislabel them.
_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"<svg", "image/svg+xml"),
    (b"<?xml", "image/svg+xml"),
]


class PinataError(RuntimeError):
    """Anything that went wrong talking to Pinata."""


# ── finding a user-attached image ───────────────────────────────────────────
# AgentOS never tells the model where an attachment lives. An image the user
# drags into webchat reaches the model as pixels — an image content block — with
# no filename and no path. But the bytes are not lost: every attachment is
# recorded in the transcript database, in one of two shapes.
#
#   inline  {"type": "image/png", "name": "image.png", "data": "<base64>"}
#   staged  {"sha256_ref": "<sha>", "name": …, "mime": …, "size": …}
#           bytes at <media_root>/transcripts/<session_id>/<sha>
#
# Anything under ~2 MB — which is most logos — is inline and never touches the
# filesystem at all.
#
# The transcript is therefore the authority, and scanning the media directory is
# not a substitute for it. An earlier version of this module did exactly that,
# and it was actively dangerous: with no inline attachment on disk it returned
# the newest *staged* blob, which belonged to an unrelated session three days
# earlier, and offered it as the logo to pin into an immutable token. Ordering
# by file mtime cannot tell "the image the user just sent" from "some image".
# Message rows can, because they carry the message's own timestamp and session.

_MEDIA_ROOT_ENV = "AGENTOS_ATTACHMENTS_MEDIA_ROOT"
_STATE_DIR_ENV = "AGENTOS_STATE_DIR"

# Past this, a candidate is almost certainly not what the user just attached.
STALE_ATTACHMENT_SECONDS = 15 * 60


def agentos_home() -> Path:
    state_dir = os.environ.get(_STATE_DIR_ENV, "").strip()
    return Path(state_dir).expanduser() if state_dir else Path.home() / ".agentos"


def sessions_db_path() -> Path:
    return agentos_home() / "state" / "sessions.db"


def agentos_media_root() -> Path:
    """Where AgentOS stages attachments, mirroring ``agentos.paths``.

    Kept as a small reimplementation rather than an import: this skill's scripts
    run as standalone python against the system interpreter and cannot assume
    the ``agentos`` package is importable.
    """
    override = os.environ.get(_MEDIA_ROOT_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    state_dir = os.environ.get(_STATE_DIR_ENV, "").strip()
    home = Path(state_dir).expanduser() if state_dir else Path.home() / ".agentos"
    return home / "media"


def sniff_image_mime(path: Path) -> str | None:
    """The image type from magic bytes, or None when it is not an image.

    Magic bytes, not the extension: staged attachments have no extension at all.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(32)
    except OSError:
        return None
    if not head:
        return None
    for magic, mime in _MAGIC:
        if head.startswith(magic):
            return mime
    # RIFF....WEBP
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    return None


def find_attachment_images(*, session: str | None = None, limit: int = 10,
                           media_root: Path | None = None) -> list[dict]:
    """Staged attachment images on disk, newest first.

    A fallback only. Prefer :func:`find_chat_images`: file mtime says when bytes
    were written, not which message they belong to, so this cannot distinguish
    the image the user just sent from one staged days ago in another session.
    """
    root = (media_root or agentos_media_root()) / "transcripts"
    if not root.is_dir():
        return []
    found: list[dict] = []
    session_dirs = [root / session] if session else sorted(
        (d for d in root.iterdir() if d.is_dir()), reverse=True)
    for directory in session_dirs:
        if not directory.is_dir():
            continue
        for candidate in directory.iterdir():
            if not candidate.is_file():
                continue
            mime = sniff_image_mime(candidate)
            if not mime:
                continue
            try:
                stat = candidate.stat()
            except OSError:
                continue
            found.append({
                "path": str(candidate), "session": directory.name,
                "bytes": stat.st_size, "mime": mime, "mtime": stat.st_mtime,
            })
    found.sort(key=lambda entry: entry["mtime"], reverse=True)
    return found[:limit]


def find_chat_images(*, session: str | None = None, limit: int = 10,
                     db_path: Path | None = None) -> list[dict]:
    """Image attachments from the transcript, newest message first.

    Each entry: ``{entry_id, index, session_key, sent_at, name, mime, bytes,
    source}`` where ``source`` is ``"inline"`` or ``"staged"``. Ordering is by
    the message's own timestamp, which is the only ordering that answers "what
    did the user just send".

    Returns an empty list if the database is missing or unreadable; the caller
    falls back to a disk scan and says so.
    """
    import sqlite3

    path = db_path or sessions_db_path()
    if not path.is_file():
        return []
    query = """
        SELECT e.id, a.key, e.session_key, e.created_at,
               json_extract(a.value, '$.name'),
               COALESCE(json_extract(a.value, '$.type'),
                        json_extract(a.value, '$.mime')),
               json_extract(a.value, '$.sha256_ref'),
               json_extract(a.value, '$.size'),
               length(json_extract(a.value, '$.data'))
        FROM transcript_entries e, json_each(e.content, '$.attachments') a
        WHERE e.role = 'user' AND json_valid(e.content)
        ORDER BY e.created_at DESC
        LIMIT 200
    """
    try:
        # Read-only URI: the gateway may hold this database open for writes.
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return []
    try:
        rows = con.execute(query).fetchall()
    except sqlite3.Error:
        # Old schema, or no JSON1 support in this interpreter's sqlite.
        return []
    finally:
        con.close()

    out: list[dict] = []
    for (entry_id, index, session_key, created_at, name, mime,
         sha_ref, size, b64_len) in rows:
        if session and session not in (session_key or ""):
            continue
        mime = (mime or "").strip()
        # A missing mime on a staged ref is rare but recoverable from the name.
        if not mime and name and "." in str(name):
            mime = _IMAGE_MIME.get("." + str(name).rsplit(".", 1)[-1].lower(), "")
        if not mime.startswith("image/"):
            continue
        out.append({
            "entry_id": int(entry_id),
            "index": int(index),
            "session_key": session_key or "",
            "sent_at": (int(created_at) / 1000.0) if created_at else 0.0,
            "name": name or "attachment",
            "mime": mime,
            "bytes": int(size) if size else (b64_len or 0) * 3 // 4,
            "source": "staged" if sha_ref else "inline",
            "sha256_ref": sha_ref or "",
        })
        if len(out) >= limit:
            break
    return out


def materialize_chat_image(entry: dict, *, dest_dir: Path | None = None,
                           db_path: Path | None = None,
                           media_root: Path | None = None) -> Path:
    """Write a transcript image attachment to a real file and return its path.

    This is the step that closes the gap: the bytes exist, they are simply not
    on the filesystem where ``--image`` can reach them. Inline attachments are
    base64-decoded out of the transcript row; staged ones are copied from the
    content-addressed blob the row points at.
    """
    import sqlite3

    target_dir = dest_dir or (Path(tempfile.gettempdir()) / "poolsfun-logos")
    target_dir.mkdir(parents=True, exist_ok=True)

    if entry["source"] == "staged":
        session_id = (entry.get("session_key") or "").rsplit(":", 1)[-1]
        blob = ((media_root or agentos_media_root()) / "transcripts" / session_id
                / entry["sha256_ref"])
        if not blob.is_file():
            raise PinataError(
                f"the transcript points at {entry['sha256_ref'][:12]}… but that blob is "
                f"not under {agentos_media_root()}/transcripts. Ask the user for the "
                "file path instead."
            )
        payload = blob.read_bytes()
    else:
        path = db_path or sessions_db_path()
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        try:
            row = con.execute(
                "SELECT json_extract(content, '$.attachments[' || ? || '].data') "
                "FROM transcript_entries WHERE id = ?",
                (entry["index"], entry["entry_id"]),
            ).fetchone()
        finally:
            con.close()
        if not row or not row[0]:
            raise PinataError("the transcript row no longer carries the image data")
        payload = base64.b64decode(row[0])

    suffix = Path(str(entry.get("name") or "")).suffix.lower()
    if suffix not in _IMAGE_MIME:
        suffix = "." + (entry["mime"].split("/")[-1] or "png")
    # Content-addressed so re-running does not pile up copies, and so the same
    # image always lands at the same path.
    digest = hashlib.sha256(payload).hexdigest()[:16]
    out_path = target_dir / f"{digest}{suffix}"
    out_path.write_bytes(payload)
    return out_path


def pinata_configured() -> bool:
    return pinata_jwt() is not None


def require_pinata_jwt(reason: str) -> str:
    """The JWT, or a refusal that says exactly how to fix it.

    Called only from paths that truly need Pinata. The message names the AgentOS
    setup action and the escape hatch, because "PINATA_JWT is not set" on its own
    leaves the user guessing whether the skill is broken or just unconfigured.
    """
    jwt = pinata_jwt()
    if not jwt:
        raise PinataError(
            f"{reason} needs Pinata, but {ENV_PINATA_JWT} is not set.\n"
            f"  Either set it in the agent environment "
            f"(AgentOS: Settings > Skills > Set {ENV_PINATA_JWT}),\n"
            f"  or drop --image and launch without a token image,\n"
            f"  or pass --metadata-uri <uri> if you have already hosted the metadata."
        )
    return jwt


def _post(url: str, body: bytes, headers: dict[str, str], timeout: int = 120) -> dict:
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        if exc.code in (401, 403):
            raise PinataError(
                f"Pinata rejected the credentials (HTTP {exc.code}). Check {ENV_PINATA_JWT} "
                f"is a valid JWT — not an API key or secret. {detail}"
            ) from exc
        raise PinataError(f"Pinata request failed: HTTP {exc.code} {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise PinataError(f"Pinata unreachable: {exc}") from exc


def verify_pinata_auth(jwt: str) -> None:
    """Probe the JWT before uploading anything.

    A bad token surfaces here in one cheap request rather than after a multi-megabyte
    upload, and — more importantly — before the launch plan is built around an image
    that was never going to pin.
    """
    request = urllib.request.Request(
        f"{PINATA_API}/data/testAuthentication",
        headers={"authorization": f"Bearer {jwt}", "user-agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        raise PinataError(
            f"{ENV_PINATA_JWT} was rejected by Pinata (HTTP {exc.code}). It must be a JWT "
            "from Pinata -> API Keys, not the API key or secret."
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise PinataError(f"Pinata unreachable: {exc}") from exc


def _guess_mime(path: Path, head: bytes) -> str:
    for magic, mime in _MAGIC:
        if head.startswith(magic):
            return mime
    suffix = path.suffix.lower()
    if suffix in _IMAGE_MIME:
        return _IMAGE_MIME[suffix]
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _multipart(fields: dict[str, str], filename: str, mime: str,
               payload: bytes) -> tuple[bytes, str]:
    """Hand-rolled multipart/form-data.

    The skill deliberately has no third-party dependencies (it targets the system
    python that ships with macOS), so there is no `requests` to lean on and
    urllib cannot build this itself.
    """
    boundary = "----poolsfun" + base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")
    # A boundary that appeared inside the payload would truncate the upload. With
    # 96 random bits that is not going to happen by chance, but the check is one
    # line and the failure it prevents is a silently corrupt logo.
    if boundary.encode() in payload:
        raise PinataError("could not frame the upload; retry")
    out = bytearray()
    for key, value in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'content-disposition: form-data; name="{key}"\r\n\r\n'.encode()
        out += value.encode() + b"\r\n"
    out += f"--{boundary}\r\n".encode()
    out += (
        f'content-disposition: form-data; name="file"; filename="{filename}"\r\n'
    ).encode()
    out += f"content-type: {mime}\r\n\r\n".encode()
    out += payload + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def pin_file(path: str | Path, jwt: str, *, name: str | None = None) -> str:
    """Pin a local file to IPFS. Returns the CID."""
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise PinataError(f"image not found: {file_path}")
    payload = file_path.read_bytes()
    if not payload:
        raise PinataError(f"image is empty: {file_path}")
    if len(payload) > MAX_IMAGE_BYTES:
        raise PinataError(
            f"image is {len(payload) / 1048576:.1f} MB, over the "
            f"{MAX_IMAGE_BYTES // 1048576} MB limit. Resize it first."
        )
    mime = _guess_mime(file_path, payload[:16])
    if not mime.startswith("image/"):
        raise PinataError(
            f"{file_path.name} does not look like an image (detected {mime}). "
            "Pass a png/jpg/gif/webp/svg."
        )
    # A staged AgentOS attachment is named after its sha256 with no extension.
    # Give Pinata a sensible filename derived from the sniffed type instead, so
    # the pinned object is not called "68c79977460f28…".
    upload_name = file_path.name
    if not file_path.suffix:
        upload_name = f"{(name or 'logo').replace(' ', '-')}.{mime.split('/')[-1]}"
    body, content_type = _multipart(
        {"pinataMetadata": json.dumps({"name": name or upload_name})},
        upload_name, mime, payload,
    )
    result = _post(
        f"{PINATA_API}/pinning/pinFileToIPFS", body,
        {"authorization": f"Bearer {jwt}", "content-type": content_type,
         "user-agent": USER_AGENT},
    )
    cid = result.get("IpfsHash")
    if not cid:
        raise PinataError(f"Pinata returned no IpfsHash: {result}")
    return str(cid)


def pin_json(obj: dict, jwt: str, *, name: str = "metadata.json") -> str:
    """Pin a JSON document to IPFS. Returns the CID."""
    body = json.dumps(
        {"pinataContent": obj, "pinataMetadata": {"name": name}},
        separators=(",", ":"),
    ).encode()
    result = _post(
        f"{PINATA_API}/pinning/pinJSONToIPFS", body,
        {"authorization": f"Bearer {jwt}", "content-type": "application/json",
         "user-agent": USER_AGENT},
    )
    cid = result.get("IpfsHash")
    if not cid:
        raise PinataError(f"Pinata returned no IpfsHash: {result}")
    return str(cid)


def build_metadata(name: str, symbol: str, *, description: str | None = None,
                   image: str | None = None, website: str | None = None,
                   twitter: str | None = None) -> dict:
    """The metadata document, shaped like the ones already on-chain.

    Field order and names match what pools.fun itself writes, so existing readers
    parse it without special-casing. Empty fields are omitted rather than sent as
    empty strings — a null logo renders worse than an absent one.
    """
    doc: dict[str, Any] = {"name": name, "symbol": symbol}
    if description:
        doc["description"] = description
    if image:
        doc["image"] = image
    if website:
        doc["website"] = website
    if twitter:
        doc["twitter"] = twitter
    return doc


def to_data_uri(doc: dict) -> str:
    """Inline the metadata as a base64 data URI — no hosting, no secret."""
    raw = json.dumps(doc, separators=(",", ":")).encode()
    uri = "data:application/json;base64," + base64.b64encode(raw).decode()
    if len(uri) > MAX_INLINE_URI_BYTES:
        raise PinataError(
            f"inline metadata is {len(uri)} bytes, over the {MAX_INLINE_URI_BYTES} byte "
            f"limit — it would make every launch needlessly expensive. Shorten "
            f"--description, or set {ENV_PINATA_JWT} to pin it instead."
        )
    return uri


def resolve_metadata_uri(*, name: str, symbol: str, metadata_uri: str | None = None,
                         image: str | None = None, description: str | None = None,
                         website: str | None = None, twitter: str | None = None,
                         pin: bool = False) -> tuple[str, dict | None, str]:
    """Produce the `metadataUri` for a launch.

    Returns ``(uri, document_or_None, how)`` where ``how`` is a short phrase for
    the plan block so the user can see which path ran.

    Precedence, highest first:

    1. ``metadata_uri`` — pass-through, never touches Pinata.
    2. ``image`` — pin the image, then pin the JSON. Requires PINATA_JWT.
    3. ``pin`` — pin the JSON only. Requires PINATA_JWT.
    4. otherwise — inline data URI. No secret, no network.
    """
    if metadata_uri:
        uri = metadata_uri.strip()
        if not (uri.startswith("ipfs://") or uri.startswith("https://")
                or uri.startswith("data:")):
            raise PinataError(
                f"--metadata-uri must start with ipfs://, https:// or data: (got {uri[:32]!r})"
            )
        return uri, None, "supplied"

    if image:
        jwt = require_pinata_jwt("attaching a token image")
        verify_pinata_auth(jwt)
        image_cid = pin_file(image, jwt, name=f"{symbol} logo")
        # Use the HTTPS gateway form for `image`, matching what pools.fun writes:
        # every wallet and explorer renders it, whereas ipfs:// needs a resolver.
        doc = build_metadata(name, symbol, description=description,
                             image=PINATA_GATEWAY + image_cid,
                             website=website, twitter=twitter)
        cid = pin_json(doc, jwt, name=f"{symbol} metadata")
        return f"ipfs://{cid}", doc, "image pinned via Pinata"

    doc = build_metadata(name, symbol, description=description,
                         website=website, twitter=twitter)
    if pin:
        jwt = require_pinata_jwt("--pin-metadata")
        verify_pinata_auth(jwt)
        cid = pin_json(doc, jwt, name=f"{symbol} metadata")
        return f"ipfs://{cid}", doc, "pinned via Pinata"

    return to_data_uri(doc), doc, "inline (no image)"
