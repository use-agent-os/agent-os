from __future__ import annotations

import hashlib
import json

from agentos.engine.agent import _omit_large_json_tool_fields


def test_large_body_fields_are_omitted_from_json_tool_result() -> None:
    large_body = "x" * 20_001
    large_base64 = "y" * 20_001
    content = json.dumps(
        {
            "status": 200,
            "body": large_body,
            "nested": {"body_base64": large_base64},
            "small": "kept",
        }
    )

    sanitized, changed = _omit_large_json_tool_fields(content)
    payload = json.loads(sanitized)

    assert changed is True
    assert payload["body"] == {
        "omitted": True,
        "omitted_chars": len(large_body),
        "sha256": hashlib.sha256(large_body.encode("utf-8")).hexdigest(),
        "reason": "large_tool_result_field",
    }
    assert payload["nested"]["body_base64"]["omitted"] is True
    assert payload["small"] == "kept"


def test_small_body_fields_are_left_unchanged() -> None:
    content = json.dumps({"body": "small", "body_base64": "YWJj"})

    sanitized, changed = _omit_large_json_tool_fields(content)

    assert changed is False
    assert sanitized == content
