"""Tests for atomic media write in transcript persistence.

Verifies the tmp + flush + fsync + os.replace flow, that the exception
path unlinks the tmp file, that os.replace is invoked, that a failed
write leaves no residue, and that concurrent writes to the same target
produce an uncorrupted file.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from unittest.mock import patch

import pytest

from agentos.gateway.transcripts import _atomic_write_bytes

# ---------------------------------------------------------------------------
# write_uses_replace
# ---------------------------------------------------------------------------

def test_write_uses_replace(tmp_path: Path) -> None:
    """os.replace must be called when writing atomically."""
    target = tmp_path / "target.bin"
    data = b"hello atomic"

    with patch("agentos.gateway.transcripts.os.replace", wraps=os.replace) as mock_replace:
        _atomic_write_bytes(target, data)
        mock_replace.assert_called_once()
        # First arg of the call must differ from target (it's the tmp path)
        called_src = Path(mock_replace.call_args[0][0])
        called_dst = Path(mock_replace.call_args[0][1])
        assert called_src != called_dst
        assert called_dst == target

    assert target.read_bytes() == data


# ---------------------------------------------------------------------------
# failed_write_no_residue
# ---------------------------------------------------------------------------

def test_failed_write_no_residue(tmp_path: Path) -> None:
    """If the write raises mid-way, target must not exist and tmp must be cleaned up."""
    target = tmp_path / "target.bin"
    data = b"should never land"

    original_open = open

    call_count = {"n": 0}

    def exploding_open(path, mode="r", **kw):
        if "wb" in mode or mode == "wb":
            call_count["n"] += 1
            # Return a context manager that raises on __exit__ after write
            class _ExplodingFile:
                def __init__(self):
                    self._f = original_open(path, mode, **kw)
                    self._path = path

                def write(self, b):
                    return self._f.write(b)

                def flush(self):
                    raise OSError("simulated disk failure")

                def fileno(self):
                    return self._f.fileno()

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    try:
                        self._f.close()
                    except Exception:
                        pass
                    return False  # re-raise

            return _ExplodingFile()
        return original_open(path, mode, **kw)

    with patch("builtins.open", side_effect=exploding_open):
        with pytest.raises(OSError, match="simulated disk failure"):
            _atomic_write_bytes(target, data)

    # Target must not exist
    assert not target.exists(), "target file must not exist after failed write"

    # No .tmp.* residue in the directory
    residue = list(tmp_path.glob("*.tmp.*"))
    assert residue == [], f"tmp residue found: {residue}"


# ---------------------------------------------------------------------------
# concurrent_write_same_target
# ---------------------------------------------------------------------------

def test_concurrent_write_same_target(tmp_path: Path) -> None:
    """Two concurrent writes to the same target must not corrupt the file.

    The final content must be exactly one of the two payloads (no mixing).
    """
    target = tmp_path / "shared.bin"
    payload_a = b"AAAA" * 4096  # 16 KiB, distinct pattern
    payload_b = b"BBBB" * 4096

    errors: list[Exception] = []

    def write_it(data: bytes) -> None:
        try:
            _atomic_write_bytes(target, data)
        except Exception as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(write_it, payload_a),
            pool.submit(write_it, payload_b),
        ]
        for f in as_completed(futures):
            f.result()  # propagate any unexpected exception

    assert not errors, f"unexpected errors during concurrent write: {errors}"
    assert target.exists(), "target file must exist after concurrent writes"

    final = target.read_bytes()
    assert final in (payload_a, payload_b), (
        f"file content is corrupted (length={len(final)}, "
        f"expected {len(payload_a)} bytes of either A or B pattern)"
    )
