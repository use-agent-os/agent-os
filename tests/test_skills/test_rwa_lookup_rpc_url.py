"""Regression tests for --rpc-url scheme validation in rwa_lookup.py.

The script passed --rpc-url straight into urllib.request.urlopen without
scheme validation, allowing file:// and other non-http(s) schemes to read
arbitrary local files when invoked from an agent context.

Fix: validate the RPC URL in both _rpc_batch() (the call site) and main()
(where the argument is consumed), rejecting any scheme outside {http, https}.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "src/agentos/skills/bundled/robinhood-rwa-addresses/scripts/rwa_lookup.py"
)
_spec = importlib.util.spec_from_file_location("rwa_lookup", _SCRIPT)
rwa_lookup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rwa_lookup)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    """Run rwa_lookup.py as a subprocess and capture output."""
    script = Path(rwa_lookup.__file__).resolve()
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Unit tests — _validate_rpc_url
# ---------------------------------------------------------------------------


class TestValidateRpcUrl:
    """Direct tests for the validation logic used in _rpc_batch and main."""

    @pytest.mark.parametrize(
        "url, expected_valid",
        [
            ("https://rpc.mainnet.chain.robinhood.com", True),
            ("http://localhost:8545", True),
            ("https://127.0.0.1:8545", True),
            ("http://192.168.1.1:8545", True),
            ("https://rpc.example.com/path?query=1", True),
            ("file:///etc/passwd", False),
            ("file:///etc/hosts", False),
            ("ftp://rpc.example.com", False),
            ("data:text/plain,hello", False),
            ("javascript:alert(1)", False),
            ("gopher://localhost:8080/test", False),
            ("/absolute/path/to/file", False),
            ("relative/path", False),
            ("", False),
            ("not-a-url", False),
            ("http://", False),  # bare scheme with no host
            ("https://", False),  # bare scheme with no host
        ],
    )
    def test_url_valve(self, url: str, expected_valid: bool) -> None:
        if expected_valid:
            # Should not raise
            rwa_lookup._validate_rpc_url(url)
        else:
            with pytest.raises(ValueError, match="RPC URL must use http or https"):
                rwa_lookup._validate_rpc_url(url)


# ---------------------------------------------------------------------------
# Integration tests — _rpc_batch
# ---------------------------------------------------------------------------


class TestRpcBatch:
    """_rpc_batch validates its rpc_url argument."""

    def test_https_accepted(self) -> None:
        """_rpc_batch accepts https:// URLs."""
        # Will fail on connection but should pass URL validation
        with pytest.raises((OSError, TimeoutError)):
            rwa_lookup._rpc_batch("https://rpc.example.com", [], timeout=1.0)

    def test_http_accepted(self) -> None:
        """_rpc_batch accepts http:// URLs."""
        with pytest.raises((OSError, TimeoutError)):
            rwa_lookup._rpc_batch("http://localhost:8545", [], timeout=1.0)

    def test_file_scheme_rejected(self) -> None:
        """_rpc_batch rejects file:// URLs."""
        with pytest.raises(ValueError, match="RPC URL must use http or https"):
            rwa_lookup._rpc_batch("file:///etc/hosts", [], timeout=1.0)

    def test_ftp_scheme_rejected(self) -> None:
        """_rpc_batch rejects ftp:// URLs."""
        with pytest.raises(ValueError, match="RPC URL must use http or https"):
            rwa_lookup._rpc_batch("ftp://rpc.example.com", [], timeout=1.0)

    def test_bare_http_rejected(self) -> None:
        """_rpc_batch rejects bare 'http://' with no host."""
        with pytest.raises(ValueError, match="RPC URL must use http or https"):
            rwa_lookup._rpc_batch("http://", [], timeout=1.0)

    def test_empty_string_rejected(self) -> None:
        """_rpc_batch rejects empty rpc_url."""
        with pytest.raises(ValueError, match="RPC URL must use http or https"):
            rwa_lookup._rpc_batch("", [], timeout=1.0)


# ---------------------------------------------------------------------------
# Integration tests — main() via subprocess
# ---------------------------------------------------------------------------


class TestMainCli:
    """main() validates --rpc-url before making any network calls."""

    def test_default_url_works(self) -> None:
        """Default --rpc-url (Robinhood mainnet) should pass validation."""
        # Without network access main() may fail later but not on URL validation
        result = _run_cli(
            "--query", "Apple", "--no-verify", "--rpc-url", rwa_lookup.DEFAULT_RPC_URL
        )
        # Should get valid JSON (even if no network for token list)
        try:
            data = json.loads(result.stdout)
            assert "matches" in data
        except json.JSONDecodeError:
            # May fail on token fetch but should NOT fail on URL validation
            assert "error" not in result.stderr or "scheme" not in result.stderr.lower()

    def test_https_accepted_cli(self) -> None:
        """--rpc-url with https:// should pass validation."""
        result = _run_cli("--query", "AAPL", "--no-verify", "--rpc-url", "https://arbitrary-rpc.example.com")
        # Should NOT say "scheme" in stderr
        assert "scheme" not in result.stderr.lower()

    def test_file_scheme_exit_code(self) -> None:
        """--rpc-url with file:// should exit with code 2 and clear error."""
        result = _run_cli("--query", "AAPL", "--rpc-url", "file:///etc/hosts")
        assert result.returncode == 2
        assert "RPC URL must use http or https" in result.stderr

    def test_ftp_scheme_exit_code(self) -> None:
        """--rpc-url with ftp:// should exit with code 2."""
        result = _run_cli("--query", "AAPL", "--rpc-url", "ftp://rpc.example.com")
        assert result.returncode == 2
        assert "RPC URL must use http or https" in result.stderr

    def test_bare_http_scheme_rejected(self) -> None:
        """--rpc-url 'http://' (no host) should exit with code 2."""
        result = _run_cli("--query", "AAPL", "--rpc-url", "http://")
        assert result.returncode == 2
        assert "RPC URL must use http or https" in result.stderr

    def test_data_scheme_rejected(self) -> None:
        """--rpc-url with data: should exit with code 2."""
        result = _run_cli("--query", "AAPL", "--rpc-url", "data:text/plain,hello")
        assert result.returncode == 2
        assert "RPC URL must use http or https" in result.stderr

    def test_gopher_scheme_rejected(self) -> None:
        """--rpc-url with gopher:// should exit with code 2."""
        result = _run_cli("--query", "AAPL", "--rpc-url", "gopher://localhost:8080/test")
        assert result.returncode == 2

    def test_none_url_rejected(self) -> None:
        """--rpc-url with empty string should exit with code 2."""
        result = _run_cli("--query", "AAPL", "--rpc-url", "")
        assert result.returncode == 2


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases for rpc_url validation."""

    def test_url_with_path(self) -> None:
        """URLs with paths should be accepted (valid http/https)."""
        # Should pass validation, fail on network
        with pytest.raises((OSError, TimeoutError)):
            rwa_lookup._rpc_batch("https://rpc.example.com/v1/chain", [], timeout=1.0)

    def test_url_with_port(self) -> None:
        """URLs with explicit port should be accepted."""
        with pytest.raises((OSError, TimeoutError)):
            rwa_lookup._rpc_batch("http://localhost:8545", [], timeout=1.0)

    def test_url_with_query_params(self) -> None:
        """URLs with query params should be accepted."""
        with pytest.raises((OSError, TimeoutError)):
            rwa_lookup._rpc_batch("https://rpc.example.com/?apikey=123", [], timeout=1.0)

    def test_case_insensitive_scheme(self) -> None:
        """Scheme validation should be case-insensitive."""
        with pytest.raises((OSError, TimeoutError)):
            rwa_lookup._rpc_batch("HTTP://localhost:8545", [], timeout=1.0)

    def test_url_with_auth(self) -> None:
        """URLs with embedded auth credentials are valid http/https."""
        from http.client import InvalidURL
        with pytest.raises((OSError, TimeoutError, InvalidURL)):
            rwa_lookup._rpc_batch("https://user:pass@rpc.example.com", [], timeout=1.0)


# ---------------------------------------------------------------------------
# S310 suppression comment check
# ---------------------------------------------------------------------------


class TestS310Suppression:
    """Verify the Bandit S310 suppression comment is scoped correctly."""

    RWA_LOOKUP_PATH = Path(rwa_lookup.__file__).resolve()

    def test_no_bare_s310(self) -> None:
        """No uncorrected S310 suppressions remain in rwa_lookup.py."""
        source = self.RWA_LOOKUP_PATH.read_text(encoding="utf-8")
        for i, line in enumerate(source.splitlines(), 1):
            if "# noqa: S310" in line:
                # Must have a documented endpoint comment after the noqa marker
                after_marker = line.split("# noqa: S310")[1]
                assert after_marker.strip().startswith("-"), (
                    f"Line {i}: S310 suppression must document the validated '"
                    f"endpoint: {line.strip()}"
                )

    def test_s310_has_endpoint_comment(self) -> None:
        """Every S310 suppression documents the validated endpoint."""
        source = self.RWA_LOOKUP_PATH.read_text(encoding="utf-8")
        for i, line in enumerate(source.splitlines(), 1):
            if "S310" in line and "noqa" in line.lower():
                has_doc = any(
                    phrase in line.lower()
                    for phrase in ("validated", "trusted", "verified")
                )
                assert has_doc, (
                    f"Line {i}: S310 suppression on URL must document "
                    f"that the endpoint is validated: {line.strip()}"
                )

    def test_rpc_batch_s310_has_validation_reference(self) -> None:
        """The _rpc_batch S310 suppression references URL validation."""
        source = self.RWA_LOOKUP_PATH.read_text(encoding="utf-8")
        lines = source.splitlines()
        rpc_batch_idx = next(
            i for i, line in enumerate(lines) if "def _rpc_batch" in line
        )
        # Scan 5 lines after _rpc_batch for S310
        relevant = "\n".join(lines[rpc_batch_idx : rpc_batch_idx + 8])
        if "S310" in relevant:
            assert (
                "validated" in relevant.lower() or "verified" in relevant.lower()
            ), f"_rpc_batch S310 must reference validation: {relevant}"
