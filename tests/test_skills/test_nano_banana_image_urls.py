"""Unit tests for nano-banana-pro generate_image.py HTTP(S) URL and Data URI support."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "src/agentos/skills/bundled/nano-banana-pro/scripts/generate_image.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_agentos_test_nano_banana_script", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_agentos_test_nano_banana_script"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script() -> ModuleType:
    return _load_script()


def test_is_url(script: ModuleType) -> None:
    assert script._is_url("http://example.com/image.png") is True
    assert script._is_url("https://example.com/image.png") is True
    assert script._is_url("data:image/png;base64,abc==") is True
    assert script._is_url("image.png") is False
    assert script._is_url("/path/to/image.png") is False
    assert script._is_url("") is False


def test_encode_input_image_urls_pass_through(script: ModuleType) -> None:
    http_url = "https://example.com/ref.png"
    data_uri = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"

    assert script.encode_input_image(http_url) == http_url
    assert script.encode_input_image(data_uri) == data_uri


def test_encode_input_image_local_file(script: ModuleType, tmp_path: Path) -> None:
    local_file = tmp_path / "test.png"
    local_file.write_bytes(b"dummy_png_bytes")

    result = script.encode_input_image(str(local_file))
    assert result.startswith("data:image/png;base64,")
    decoded = base64.b64decode(result.split(",", 1)[1])
    assert decoded == b"dummy_png_bytes"


def test_fetch_url_to_bytes_data_url(script: ModuleType) -> None:
    expected_bytes = b"hello_world"
    data_uri = f"data:image/png;base64,{base64.b64encode(expected_bytes).decode('ascii')}"

    result = script._fetch_url_to_bytes(data_uri, api_key="sk-test")
    assert result == expected_bytes


def test_fetch_url_to_bytes_http_url(script: ModuleType) -> None:
    expected_bytes = b"remote_image_bytes"
    mock_resp = MagicMock()
    mock_resp.read.return_value = expected_bytes
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        result = script._fetch_url_to_bytes(
            "https://openrouter.ai/api/v1/files/img.png",
            api_key="sk-or-key",
        )
        assert result == expected_bytes
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer sk-or-key"
        assert req.get_header("Http-referer") == "https://useagentos.dev"


def test_try_one_attempt_with_http_response_url(script: ModuleType) -> None:
    expected_bytes = b"generated_http_image"
    response_json = {
        "choices": [
            {"message": {"images": [{"image_url": {"url": "https://example.com/output.png"}}]}}
        ]
    }

    def mock_urlopen(req, timeout=120):
        url = req.full_url
        mock_resp = MagicMock()
        if "chat/completions" in url:
            mock_resp.read.return_value = json.dumps(response_json).encode("utf-8")
        else:
            mock_resp.read.return_value = expected_bytes
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None
        return mock_resp

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        result = script._try_one_attempt(
            base_url="https://openrouter.ai/api/v1",
            api_key="sk-test",
            prompt="a cat",
            input_image="https://example.com/input.png",
            aspect_ratio="1:1",
            image_size="1K",
            model="google/gemini-3.1-flash-image-preview",
            timeout=120,
        )
        assert result == expected_bytes
