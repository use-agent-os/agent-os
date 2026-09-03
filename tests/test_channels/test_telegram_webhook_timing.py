"""Test for Telegram webhook secret timing vulnerability fix."""

import pytest
import time
import hmac
from unittest.mock import Mock, patch
from starlette.requests import Request
from starlette.responses import Response

from src.agentos.channels.telegram import TelegramChannel


class TestTelegramWebhookTimingVulnerability:
    """Test that the Telegram webhook secret verification uses constant-time comparison."""

    @pytest.fixture
    def telegram_channel(self):
        """Create a Telegram channel with webhook secret."""
        with patch('src.agentos.channels.telegram.TelegramChannel.__init__', return_value=None):
            channel = TelegramChannel()
            channel.config = Mock()
            channel.config.webhook_secret_token = "test_secret_token_12345"
            channel.config.webhook_path = "/webhook"
            return channel

    def test_webhook_secret_timing_constant_time(self, telegram_channel):
        """Test that secret verification uses constant-time comparison."""
        # Mock the request
        mock_request = Mock(spec=Request)
        
        # Test with correct token
        mock_request.headers = {"X-Telegram-Bot-Api-Secret-Token": "test_secret_token_12345"}
        
        # Mock the request.json() method
        mock_request.json.return_value = {"update_id": 1}
        
        # Time the verification with correct token
        start_time = time.time()
        
        # This should use hmac.compare_digest internally
        result = telegram_channel._handle_webhook(mock_request)
        
        correct_time = time.time() - start_time
        
        # Test with wrong token (similar length)
        mock_request.headers = {"X-Telegram-Bot-Api-Secret-Token": "test_secret_token_1234X"}
        
        start_time = time.time()
        result_wrong = telegram_channel._handle_webhook(mock_request)
        wrong_time = time.time() - start_time
        
        # Test with wrong token (different length)
        mock_request.headers = {"X-Telegram-Bot-Api-Secret-Token": "short"}
        
        start_time = time.time()
        result_short = telegram_channel._handle_webhook(mock_request)
        short_time = time.time() - start_time
        
        # The timing difference should be minimal (constant-time)
        # Allow some variance due to system timing, but it shouldn't be dramatically different
        time_diff = abs(correct_time - wrong_time)
        max_acceptable_diff = 0.01  # 10ms tolerance
        
        print(f"Correct token time: {correct_time:.6f}s")
        print(f"Wrong token time: {wrong_time:.6f}s")
        print(f"Short token time: {short_time:.6f}s")
        print(f"Time difference: {time_diff:.6f}s")
        
        # The timing should be roughly constant
        assert time_diff < max_acceptable_diff, f"Timing vulnerability detected! Difference: {time_diff:.6f}s"
        
        # Should return 200 for correct token
        assert result.status_code == 200
        
        # Should return 401 for wrong token
        assert result_wrong.status_code == 401
        
        # Should return 401 for short token
        assert result_short.status_code == 401

    def test_hmac_compare_digest_used(self, telegram_channel):
        """Test that hmac.compare_digest is actually being used."""
        # Mock the request
        mock_request = Mock(spec=Request)
        mock_request.headers = {"X-Telegram-Bot-Api-Secret-Token": "test_secret_token_12345"}
        mock_request.json.return_value = {"update_id": 1}
        
        # Patch hmac.compare_digest to track if it's called
        with patch('hmac.compare_digest') as mock_compare_digest:
            mock_compare_digest.return_value = True
            
            result = telegram_channel._handle_webhook(mock_request)
            
            # Verify hmac.compare_digest was called with correct parameters
            mock_compare_digest.assert_called_once()
            args = mock_compare_digest.call_args[0]
            
            # First argument should be the secret
            assert args[0] == "test_secret_token_12345"
            
            # Second argument should be the header value
            assert args[1] == "test_secret_token_12345"
            
            # Should return 200 for correct token
            assert result.status_code == 200

    def test_webhook_secret_missing(self, telegram_channel):
        """Test behavior when webhook secret is not configured."""
        telegram_channel.config.webhook_secret_token = None
        
        mock_request = Mock(spec=Request)
        mock_request.headers = {"X-Telegram-Bot-Api-Secret-Token": "test_secret_token_12345"}
        
        result = telegram_channel._handle_webhook(mock_request)
        
        # Should return 503 when secret is not configured
        assert result.status_code == 503


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
