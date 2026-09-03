"""Test for RpcError bug fix."""

import pytest
from src.agentos.skills.bundled.senior_unilp_manager.scripts.unilp.rpc import RpcError
from src.agentos.skills.bundled.poolsdotfun_token_launcher.scripts.poolsfun.rpc import (
    RpcError as PoolsfunRpcError,
)


class TestRpcErrorBugFix:
    """Test that RpcError handles both dict and string error formats."""

    def test_rpc_error_with_dict_error(self):
        """Test RpcError with dict error format."""
        error_dict = {"code": -32600, "message": "Invalid request", "data": {"details": "test"}}

        # Test from senior-unilp-manager
        rpc_error = RpcError("test_method", error_dict)
        assert rpc_error.code == -32600
        assert rpc_error.data == {"details": "test"}
        assert rpc_error.raw == error_dict
        assert "test_method: Invalid request" in str(rpc_error)

        # Test from poolsfun
        poolsfun_error = PoolsfunRpcError("test_method", error_dict)
        assert poolsfun_error.code == -32600
        assert poolsfun_error.data == {"details": "test"}
        assert poolsfun_error.raw == error_dict
        assert "test_method: Invalid request" in str(poolsfun_error)

    def test_rpc_error_with_string_error(self):
        """Test RpcError with string error format."""
        error_string = "Network error: Connection refused"

        # Test from senior-unilp-manager
        rpc_error = RpcError("test_method", error_string)
        assert rpc_error.code is None
        assert rpc_error.data is None
        assert rpc_error.raw == {"message": error_string}
        assert f"test_method: {error_string}" in str(rpc_error)

        # Test from poolsfun
        poolsfun_error = PoolsfunRpcError("test_method", error_string)
        assert poolsfun_error.code is None
        assert poolsfun_error.data is None
        assert poolsfun_error.raw == {"message": error_string}
        assert f"test_method: {error_string}" in str(poolsfun_error)

    def test_rpc_error_with_dict_no_message(self):
        """Test RpcError with dict that has no message field."""
        error_dict = {"code": -32600, "data": {"details": "test"}}

        # Test from senior-unilp-manager
        rpc_error = RpcError("test_method", error_dict)
        assert rpc_error.code == -32600
        assert rpc_error.data == {"details": "test"}
        assert rpc_error.raw == error_dict
        assert "test_method: {'code': -32600, 'data': {'details': 'test'}}" in str(rpc_error)

        # Test from poolsfun
        poolsfun_error = PoolsfunRpcError("test_method", error_dict)
        assert poolsfun_error.code == -32600
        assert poolsfun_error.data == {"details": "test"}
        assert poolsfun_error.raw == error_dict
        assert "test_method: {'code': -32600, 'data': {'details': 'test'}}" in str(poolsfun_error)

    def test_rpc_error_with_empty_dict(self):
        """Test RpcError with empty dict."""
        error_dict = {}

        # Test from senior-unilp-manager
        rpc_error = RpcError("test_method", error_dict)
        assert rpc_error.code is None
        assert rpc_error.data is None
        assert rpc_error.raw == error_dict
        assert "test_method: {}" in str(rpc_error)

        # Test from poolsfun
        poolsfun_error = PoolsfunRpcError("test_method", error_dict)
        assert poolsfun_error.code is None
        assert poolsfun_error.data is None
        assert poolsfun_error.raw == error_dict
        assert "test_method: {}" in str(poolsfun_error)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
