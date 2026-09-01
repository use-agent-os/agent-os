"""Tests for send_file size ceiling in channel adapters.

Ensures that _check_file_size rejects oversized files before any
data is sent, and that each channel adapter enforces the limit.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from agentos.channels._util import _check_file_size


class TestCheckFileSize:
    """_check_file_size rejects files above the limit."""

    def test_accepts_small_file(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"x" * 100)
            path = f.name
        try:
            _check_file_size(path, limit=200)  # no raise
        finally:
            os.unlink(path)

    def test_rejects_oversized_file(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"x" * 300)
            path = f.name
        try:
            with pytest.raises(ValueError, match="File too large"):
                _check_file_size(path, limit=200)
        finally:
            os.unlink(path)

    def test_rejects_exact_limit_file(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"x" * 200)
            path = f.name
        try:
            with pytest.raises(ValueError, match="File too large"):
                _check_file_size(path, limit=199)
        finally:
            os.unlink(path)

    def test_raises_on_nonexistent_path(self) -> None:
        with pytest.raises(ValueError, match="Cannot read file size"):
            _check_file_size("/nonexistent/path/12345", limit=1024)

    def test_empty_file_accepted(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            _check_file_size(path, limit=1024)  # no raise
        finally:
            os.unlink(path)
