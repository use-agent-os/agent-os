"""Recognising a transport timeout (its retry cap is in ``test_attempt_policy``)."""

from __future__ import annotations

from agentos.provider.failures import is_transport_timeout

# ---------------------------------------------------------------------------
# is_transport_timeout unit tests
# ---------------------------------------------------------------------------


class TestIsTransportTimeout:
    def test_timeout_code(self) -> None:
        assert is_transport_timeout(raw_code="timeout") is True

    def test_timeout_code_case_insensitive(self) -> None:
        assert is_transport_timeout(raw_code="Timeout") is True

    def test_timed_out_in_message(self) -> None:
        assert is_transport_timeout(message="Request timed out: ReadTimeout") is True

    def test_request_error_is_not_timeout(self) -> None:
        assert is_transport_timeout(raw_code="request_error", message="Connection reset") is False

    def test_503_is_not_timeout(self) -> None:
        assert is_transport_timeout(raw_code="503", message="upstream 503") is False

    def test_empty_inputs(self) -> None:
        assert is_transport_timeout() is False
