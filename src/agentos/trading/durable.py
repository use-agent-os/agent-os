"""Flushing the vault's files to the disk itself.

A rename is atomic but not durable: until the file's data and the directory
entry are flushed, a power cut can leave the new name pointing at an empty
file. For a keystore that is the only copy of a wallet's key, that is the key.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def fsync_file(fd: int) -> None:
    """Flush *fd* to stable storage.

    On macOS ``fsync`` only hands the data to the drive, which may still hold
    it in a volatile cache; ``F_FULLFSYNC`` is the call that waits for the
    platters. A filesystem that refuses it falls back to ``fsync``.
    """
    if sys.platform == "darwin":
        import fcntl

        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError:
            pass
    os.fsync(fd)


def fsync_dir(path: Path) -> None:
    """Flush *path*'s directory entries so a rename in it survives a power cut.

    Best effort: Windows cannot open a directory this way and some network
    mounts refuse the fsync, neither of which undoes a write that landed.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
