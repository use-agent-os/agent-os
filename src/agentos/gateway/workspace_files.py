"""Read-only access to an agent's workspace for the operator's file browser.

The desktop app lists a workspace and previews files in it through the
``workspace.files.*`` RPCs (``agentos.gateway.rpc_workspace``). This module is
the filesystem half: it has no write path, and every read holds to the rules
below wherever the request came from.

* A request names a file by a path relative to the workspace root, with ``/``
  between segments. Absolute paths, ``..``, ``.``, empty segments,
  backslashes, drive letters and control characters are refused before the
  filesystem is touched (:func:`parse_relative_path`).
* No symlink below the root is followed, at any depth. On POSIX every segment
  is opened relative to its parent's descriptor with ``O_NOFOLLOW``, so there
  is no window between checking a directory and reading from it. A symlink is
  listed, marked as one, and cannot be opened.
* Only directories and regular files exist as far as a caller can tell. A
  regular file with more than one hard link is refused, as in
  ``agents.files.*``: a link's other name may be outside the workspace.
* Names that hold credentials or internal state are neither listed nor
  readable when named directly (:func:`is_denied_name`), and the sandbox's
  sensitive-path floor is applied to the resolved location on top of that.
* Every answer is capped: entries per directory, bytes of text, bytes of an
  image or a PDF.
"""

from __future__ import annotations

import errno
import mimetypes
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Literal

from agentos.redact import CREDENTIAL_FILE_NAMES

MAX_PATH_LENGTH = 1024
MAX_DEPTH = 32
MAX_SEGMENT_LENGTH = 255
#: Entries returned for one directory; the answer says when there were more.
MAX_DIR_ENTRIES = 500
#: Entries looked at in one directory before the scan stops.
MAX_DIR_SCAN = 20_000
MAX_TEXT_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_PDF_BYTES = 30 * 1024 * 1024
#: Bytes inspected to tell text from binary.
_SNIFF_BYTES = 8192

FileKind = Literal["markdown", "text", "code", "image", "pdf", "other"]

ErrorCode = Literal[
    "invalid_path",
    "not_found",
    "denied",
    "unsafe",
    "not_a_directory",
    "not_a_file",
    "too_large",
    "unsupported",
    "outside",
]


class WorkspaceFileError(Exception):
    """A refused or failed workspace read, with a code the client can switch on."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

_MARKDOWN_EXTENSIONS = frozenset({".md", ".markdown", ".mdx"})
_TEXT_EXTENSIONS = frozenset({".txt", ".log", ".text", ".rst", ".csv", ".tsv"})
#: Extension -> highlight.js language. Anything here is previewed as code.
_CODE_LANGUAGES: dict[str, str] = {
    ".json": "json",
    ".jsonl": "json",
    ".ndjson": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "ini",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "ini",
    ".xml": "xml",
    ".html": "xml",
    ".htm": "xml",
    ".css": "css",
    ".scss": "scss",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".py": "python",
    ".rb": "ruby",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".php": "php",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".sql": "sql",
    ".sol": "solidity",
    ".diff": "diff",
    ".patch": "diff",
    ".j2": "django",
    ".dockerfile": "dockerfile",
    ".graphql": "graphql",
    ".lua": "lua",
    ".r": "r",
}
_CODE_FILE_NAMES: dict[str, str] = {
    "dockerfile": "dockerfile",
    "makefile": "makefile",
}
#: Image extension -> the media type the download is served with. SVG is
#: served as an image only: the client shows it in ``<img>``, where it cannot
#: run script.
_IMAGE_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".avif": "image/avif",
    ".svg": "image/svg+xml",
}

#: Key, certificate and keystore material.
_DENIED_SUFFIXES: tuple[str, ...] = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".kdbx",
    ".gpg",
    ".age",
    ".ppk",
    # Databases (the trading ledger among them) and their sidecars.
    ".sqlite",
    ".sqlite3",
    ".db",
    ".sqlite-wal",
    ".sqlite-shm",
    ".sqlite-journal",
    ".db-wal",
    ".db-shm",
    ".db-journal",
    # Lock files beside MEMORY.md / USER.md: internal state, never content.
    ".lock",
)
_DENIED_NAMES: frozenset[str] = frozenset(
    {
        *(name.lower() for name in CREDENTIAL_FILE_NAMES),
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "wallets",
        "keystore",
        "credentials.json",
        "service-account.json",
        "service_account.json",
    }
)
_DENIED_STEMS: frozenset[str] = frozenset({"secrets", "secret"})


def is_denied_name(name: str) -> bool:
    """Whether a path segment is hidden from the browser and refused when named.

    Every dot-prefixed name is denied: ``.env`` and its variants, ``.git``,
    ``.ssh``, ``.aws``, ``.npmrc`` and the rest of the dotfile credential
    homes, without a list to keep in step with them. On top of that come key
    and keystore files, databases, lock files and files named for secrets.
    Matching ignores case, since the workspace may sit on a case-insensitive
    volume.
    """
    lowered = name.lower()
    if not lowered or lowered.startswith("."):
        return True
    if lowered in _DENIED_NAMES:
        return True
    if lowered.endswith(_DENIED_SUFFIXES):
        return True
    stem = lowered.split(".", 1)[0]
    return stem in _DENIED_STEMS


def _extension(name: str) -> str:
    lowered = name.lower()
    dot = lowered.rfind(".")
    return lowered[dot:] if dot > 0 else ""


def classify(name: str) -> tuple[FileKind, str]:
    """``(kind, language)`` for a file name; the language is for highlighting."""
    ext = _extension(name)
    if ext in _MARKDOWN_EXTENSIONS:
        return "markdown", "markdown"
    if ext in _IMAGE_TYPES:
        return "image", ""
    if ext == ".pdf":
        return "pdf", ""
    if ext in _TEXT_EXTENSIONS:
        return "text", ""
    language = _CODE_LANGUAGES.get(ext) or _CODE_FILE_NAMES.get(name.lower())
    if language:
        return "code", language
    return "other", ""


def media_type(name: str) -> str:
    """The media type a download of ``name`` is served with."""
    ext = _extension(name)
    if ext in _IMAGE_TYPES:
        return _IMAGE_TYPES[ext]
    if ext == ".pdf":
        return "application/pdf"
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def download_limit(kind: FileKind) -> int | None:
    """The size cap for a downloadable kind, or None when it is not one."""
    if kind == "image":
        return MAX_IMAGE_BYTES
    if kind == "pdf":
        return MAX_PDF_BYTES
    return None


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def parse_relative_path(raw: Any) -> tuple[str, ...]:
    """Split a client path into segments, or raise ``invalid_path`` / ``denied``.

    ``""`` and ``"/"``-free ``"."``-free input only: the root is the empty
    string. Nothing here touches the filesystem.
    """
    if raw is None or raw == "":
        return ()
    if not isinstance(raw, str):
        raise WorkspaceFileError("invalid_path", "path must be a string")
    if len(raw) > MAX_PATH_LENGTH:
        raise WorkspaceFileError("invalid_path", "path is too long")
    if "\\" in raw:
        raise WorkspaceFileError("invalid_path", "path must use forward slashes")
    if raw.startswith("/") or raw.startswith("~"):
        raise WorkspaceFileError("invalid_path", "path must be relative to the workspace")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise WorkspaceFileError("invalid_path", "path contains control characters")
    parts = tuple(raw.split("/"))
    if len(parts) > MAX_DEPTH:
        raise WorkspaceFileError("invalid_path", "path is too deep")
    for part in parts:
        if part in ("", ".", ".."):
            raise WorkspaceFileError("invalid_path", "path must not contain empty or dot segments")
        if len(part.encode("utf-8", "surrogatepass")) > MAX_SEGMENT_LENGTH:
            raise WorkspaceFileError("invalid_path", "path segment is too long")
        # A drive letter or an NTFS stream name: never a plain segment.
        if ":" in part:
            raise WorkspaceFileError("invalid_path", "path segment must not contain ':'")
        if part != part.strip() or part.endswith("."):
            raise WorkspaceFileError("invalid_path", "path segment has surrounding space or dot")
    for part in parts:
        if is_denied_name(part):
            raise WorkspaceFileError("denied", "this file is not available in the file browser")
    return parts


#: The segment the file tools treat as a workspace alias (``/workspace/a.md``,
#: ``~/.agentos/workspace/a.md``): see :mod:`agentos.tools.path_aliases`.
_WORKSPACE_ALIAS_SEGMENT = "workspace"


def _lexical(path: str) -> PurePath:
    return PurePath(os.path.normpath(path))


def relative_location(raw: Any, root: Path) -> str:
    """The workspace-relative form of a path an agent wrote, or raise.

    File tools accept a path relative to the workspace, an absolute path
    inside it, or an alias such as ``/workspace/notes.md``; transcripts carry
    whichever the model typed. This maps one back to the browser's relative
    form *lexically*: it reads nothing below the root and says nothing about
    whether the file exists, so it reveals no more than the root itself. The
    answer is then held to :func:`parse_relative_path`, and opening it goes
    through every check a listed file does.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise WorkspaceFileError("invalid_path", "path must be a non-empty string")
    text = raw.strip()
    if len(text) > MAX_PATH_LENGTH:
        raise WorkspaceFileError("invalid_path", "path is too long")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise WorkspaceFileError("invalid_path", "path contains control characters")
    if text.startswith("~"):
        text = os.path.expanduser(text)
    candidate = _lexical(text)
    parts: tuple[str, ...] | None = None
    # Rooted counts as absolute: ``/workspace/a.md`` has no drive on Windows.
    if candidate.is_absolute() or candidate.root:
        roots = [_lexical(str(root.expanduser()))]
        try:
            roots.append(_lexical(str(root.expanduser().resolve(strict=False))))
        except (OSError, RuntimeError, ValueError):
            pass
        for base in roots:
            if candidate == base:
                parts = ()
                break
            if candidate.is_relative_to(base):
                parts = candidate.relative_to(base).parts
                break
        if parts is None:
            segments = candidate.parts
            if _WORKSPACE_ALIAS_SEGMENT in segments:
                last = len(segments) - 1 - segments[::-1].index(_WORKSPACE_ALIAS_SEGMENT)
                parts = segments[last + 1 :]
        if parts is None:
            raise WorkspaceFileError("outside", "this path is outside the workspace")
    else:
        parts = candidate.parts
        if parts and parts[0] == "..":
            raise WorkspaceFileError("outside", "this path is outside the workspace")
        if parts == (".",):
            parts = ()
    relative = "/".join(parts)
    parse_relative_path(relative)
    return relative


def _sensitive(path: Path) -> bool:
    """The sandbox's sensitive-path floor, applied to a resolved location."""
    from agentos.sandbox.sensitive_paths import is_sensitive_path

    try:
        return is_sensitive_path(str(path)) is not None
    except Exception:  # noqa: BLE001 - a floor that cannot answer refuses
        return True


def _check_sensitive(root: Path, parts: tuple[str, ...]) -> None:
    if _sensitive(root.joinpath(*parts) if parts else root):
        raise WorkspaceFileError("denied", "this location is not available in the file browser")


# ---------------------------------------------------------------------------
# Opening without following symlinks
# ---------------------------------------------------------------------------

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)

#: Descriptor-relative opens: POSIX. Windows takes the path-based fallback.
_HAS_DIR_FD = (
    bool(_O_NOFOLLOW)
    and bool(_O_DIRECTORY)
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.scandir in os.supports_fd
)


def _map_os_error(exc: OSError, *, directory: bool) -> WorkspaceFileError:
    if exc.errno in (errno.ENOENT,):
        return WorkspaceFileError("not_found", "no such file or directory")
    if exc.errno in (errno.ELOOP, errno.EMLINK):
        return WorkspaceFileError("unsafe", "symbolic links are not followed")
    if exc.errno == errno.ENOTDIR:
        return WorkspaceFileError("not_a_directory", "not a directory")
    if exc.errno == errno.EISDIR:
        return WorkspaceFileError("not_a_file", "not a file")
    if exc.errno in (errno.EACCES, errno.EPERM):
        return WorkspaceFileError("denied", "permission denied")
    if exc.errno == errno.ENAMETOOLONG:
        return WorkspaceFileError("invalid_path", "path is too long")
    kind = "directory" if directory else "file"
    return WorkspaceFileError("not_found", f"cannot open {kind}")


def _open_root(root: Path) -> int:
    # The root itself is the operator's configuration and may be a symlink;
    # nothing below it may be.
    try:
        return os.open(root, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    except OSError as exc:
        raise _map_os_error(exc, directory=True) from exc


def _open_child_dir(parent_fd: int, name: str) -> int:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise _map_os_error(exc, directory=True) from exc
    if stat.S_ISLNK(info.st_mode):
        raise WorkspaceFileError("unsafe", "symbolic links are not followed")
    if not stat.S_ISDIR(info.st_mode):
        raise WorkspaceFileError("not_a_directory", "not a directory")
    try:
        return os.open(
            name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=parent_fd
        )
    except OSError as exc:
        raise _map_os_error(exc, directory=True) from exc


def _open_dir_fd(root: Path, parts: tuple[str, ...]) -> int:
    """A descriptor for ``root/parts``, reached without following a symlink."""
    fd = _open_root(root)
    try:
        for part in parts:
            child = _open_child_dir(fd, part)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _validate_regular(info: os.stat_result) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise WorkspaceFileError("unsafe", "symbolic links are not followed")
    if stat.S_ISDIR(info.st_mode):
        raise WorkspaceFileError("not_a_file", "not a file")
    if not stat.S_ISREG(info.st_mode):
        raise WorkspaceFileError("unsafe", "not a regular file")
    if getattr(info, "st_nlink", 1) > 1:
        raise WorkspaceFileError("unsafe", "hard-linked files are not opened")


def _fallback_dir(root: Path, parts: tuple[str, ...]) -> Path:
    """Path-based walk for platforms without descriptor-relative opens."""
    try:
        base = root.resolve(strict=True)
    except OSError as exc:
        raise _map_os_error(exc, directory=True) from exc
    current = base
    for part in parts:
        current = current / part
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise _map_os_error(exc, directory=True) from exc
        if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
            raise WorkspaceFileError("unsafe", "symbolic links are not followed")
        if not stat.S_ISDIR(info.st_mode):
            raise WorkspaceFileError("not_a_directory", "not a directory")
    try:
        current.resolve(strict=True).relative_to(base)
    except (OSError, ValueError) as exc:
        raise WorkspaceFileError("unsafe", "path leaves the workspace") from exc
    return current


def _is_reparse_point(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _open_file(root: Path, parts: tuple[str, ...]) -> tuple[int, os.stat_result]:
    """An open descriptor and its ``fstat`` for the regular file at ``parts``."""
    if not parts:
        raise WorkspaceFileError("not_a_file", "not a file")
    _check_sensitive(root, parts)
    name = parts[-1]
    flags = os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC | _O_NONBLOCK | _O_BINARY
    if _HAS_DIR_FD:
        parent_fd = _open_dir_fd(root, parts[:-1])
        try:
            try:
                before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise _map_os_error(exc, directory=False) from exc
            # Refused before the open: opening a FIFO or a device is not free.
            _validate_regular(before)
            try:
                fd = os.open(name, flags, dir_fd=parent_fd)
            except OSError as exc:
                raise _map_os_error(exc, directory=False) from exc
        finally:
            os.close(parent_fd)
    else:
        target = _fallback_dir(root, parts[:-1]) / name
        try:
            before = os.lstat(target)
        except OSError as exc:
            raise _map_os_error(exc, directory=False) from exc
        if _is_reparse_point(before):
            raise WorkspaceFileError("unsafe", "symbolic links are not followed")
        _validate_regular(before)
        try:
            fd = os.open(target, flags)
        except OSError as exc:
            raise _map_os_error(exc, directory=False) from exc
    try:
        info = os.fstat(fd)
        # The descriptor is what gets read, so it is what gets judged.
        _validate_regular(info)
    except BaseException:
        os.close(fd)
        raise
    return fd, info


def _read_capped(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        chunk = os.read(fd, min(remaining, 1024 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def _join(parts: tuple[str, ...], name: str) -> str:
    return "/".join((*parts, name))


def _entry(parts: tuple[str, ...], name: str, info: os.stat_result) -> dict[str, Any] | None:
    mode = info.st_mode
    base: dict[str, Any] = {
        "name": name,
        "path": _join(parts, name),
        "mtimeMs": int(info.st_mtime * 1000),
    }
    if stat.S_ISLNK(mode) or _is_reparse_point(info):
        return {**base, "type": "symlink", "openable": False, "reason": "symlink"}
    if stat.S_ISDIR(mode):
        return {**base, "type": "directory", "openable": True}
    if not stat.S_ISREG(mode):
        return None
    kind, language = classify(name)
    entry: dict[str, Any] = {
        **base,
        "type": "file",
        "kind": kind,
        "size": info.st_size,
        "openable": True,
    }
    if language:
        entry["language"] = language
    if getattr(info, "st_nlink", 1) > 1:
        entry.update({"openable": False, "reason": "hardlink"})
    return entry


def list_directory(root: Path, parts: tuple[str, ...]) -> dict[str, Any]:
    """The visible entries of ``root/parts``: directories first, then by name.

    A missing workspace root lists as empty rather than as an error, so a
    fresh install shows an empty browser. ``truncated`` says the directory
    held more than :data:`MAX_DIR_ENTRIES` visible entries.
    """
    _check_sensitive(root, parts)
    found: list[tuple[str, os.stat_result]] = []
    scanned = 0
    try:
        if _HAS_DIR_FD:
            fd = _open_dir_fd(root, parts)
            try:
                with os.scandir(fd) as iterator:
                    for item in iterator:
                        scanned += 1
                        if scanned > MAX_DIR_SCAN:
                            break
                        if is_denied_name(item.name):
                            continue
                        try:
                            found.append((item.name, item.stat(follow_symlinks=False)))
                        except OSError:
                            continue
            finally:
                os.close(fd)
        else:
            directory = _fallback_dir(root, parts)
            with os.scandir(directory) as iterator:
                for item in iterator:
                    scanned += 1
                    if scanned > MAX_DIR_SCAN:
                        break
                    if is_denied_name(item.name):
                        continue
                    try:
                        found.append((item.name, item.stat(follow_symlinks=False)))
                    except OSError:
                        continue
    except WorkspaceFileError as exc:
        if exc.code == "not_found" and not parts:
            return {"path": "", "entries": [], "truncated": False, "total": 0, "exists": False}
        raise
    except OSError as exc:
        raise _map_os_error(exc, directory=True) from exc

    entries = [entry for name, info in found if (entry := _entry(parts, name, info)) is not None]
    entries.sort(key=lambda e: (e["type"] != "directory", e["name"].casefold(), e["name"]))
    total = len(entries)
    return {
        "path": "/".join(parts),
        "entries": entries[:MAX_DIR_ENTRIES],
        "truncated": total > MAX_DIR_ENTRIES or scanned > MAX_DIR_SCAN,
        "total": total,
        "exists": True,
    }


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _looks_binary(sample: bytes) -> bool:
    if b"\x00" in sample:
        return True
    if not sample:
        return False
    control = sum(1 for byte in sample if byte < 9 or 13 < byte < 32 and byte != 27)
    return control / len(sample) > 0.1


def _decode(data: bytes, *, truncated: bool) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    # A cut in the middle of a multi-byte character is not a decoding error.
    return data.decode("utf-8", errors="ignore" if truncated else "replace")


def read_text(root: Path, parts: tuple[str, ...]) -> dict[str, Any]:
    """The text of a file, at most :data:`MAX_TEXT_BYTES` of it.

    ``previewable`` is false, with no content, for an image or a PDF (those
    go through a download ticket) and for anything that is not text.
    """
    name = parts[-1] if parts else ""
    kind, language = classify(name)
    fd, info = _open_file(root, parts)
    try:
        result: dict[str, Any] = {
            "path": "/".join(parts),
            "name": name,
            "kind": kind,
            "size": info.st_size,
            "mtimeMs": int(info.st_mtime * 1000),
            "previewable": False,
            "truncated": False,
            "content": "",
        }
        if language:
            result["language"] = language
        if kind in ("image", "pdf"):
            result["reason"] = "download"
            return result
        data = _read_capped(fd, MAX_TEXT_BYTES + 1)
    finally:
        os.close(fd)
    truncated = len(data) > MAX_TEXT_BYTES
    if truncated:
        data = data[:MAX_TEXT_BYTES]
    if _looks_binary(data[:_SNIFF_BYTES]):
        result["reason"] = "binary"
        return result
    result.update(
        {
            "previewable": True,
            "truncated": truncated,
            "content": _decode(data, truncated=truncated),
            "kind": "text" if kind == "other" else kind,
        }
    )
    return result


@dataclass(frozen=True, slots=True)
class WorkspaceDownload:
    """An image or a PDF, read in full under its size cap."""

    name: str
    mime: str
    size: int
    kind: FileKind
    data: bytes = b""


def stat_download(root: Path, parts: tuple[str, ...]) -> WorkspaceDownload:
    """Check that ``parts`` is a downloadable file within its cap; read nothing."""
    return _download(root, parts, read=False)


def read_download(root: Path, parts: tuple[str, ...]) -> WorkspaceDownload:
    """The bytes of an image or a PDF, re-checked from scratch."""
    return _download(root, parts, read=True)


def _download(root: Path, parts: tuple[str, ...], *, read: bool) -> WorkspaceDownload:
    name = parts[-1] if parts else ""
    kind, _ = classify(name)
    limit = download_limit(kind)
    if limit is None:
        raise WorkspaceFileError("unsupported", "only images and PDFs are downloaded")
    fd, info = _open_file(root, parts)
    try:
        if info.st_size > limit:
            raise WorkspaceFileError("too_large", "file is too large to preview")
        data = b""
        if read:
            data = _read_capped(fd, limit + 1)
            if len(data) > limit:
                raise WorkspaceFileError("too_large", "file is too large to preview")
    finally:
        os.close(fd)
    return WorkspaceDownload(
        name=name,
        mime=media_type(name),
        size=len(data) if read else info.st_size,
        kind=kind,
        data=data,
    )


__all__ = [
    "MAX_DIR_ENTRIES",
    "MAX_IMAGE_BYTES",
    "MAX_PDF_BYTES",
    "MAX_TEXT_BYTES",
    "WorkspaceDownload",
    "WorkspaceFileError",
    "classify",
    "download_limit",
    "is_denied_name",
    "list_directory",
    "media_type",
    "parse_relative_path",
    "read_download",
    "read_text",
    "relative_location",
    "stat_download",
]
