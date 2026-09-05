"""Browser eval policy: denylist (direct + obfuscated), SSRF pre-scan, redaction.

Pure functions — no engine, no browser.
"""

from __future__ import annotations

import pytest

from agentos.tools.browser_eval_policy import (
    enforce_eval_policy,
    expression_targets_private_url,
    redact_browser_output,
    risky_eval_reason,
)


class TestDenylist:
    def test_off_by_default_allows_everything(self) -> None:
        assert (
            enforce_eval_policy(
                "document.cookie", restrict_evaluate=False, allow_unsafe_evaluate=False
            )
            is None
        )

    @pytest.mark.parametrize(
        "expression",
        [
            "document.cookie",
            "window.localStorage.getItem('x')",
            "sessionStorage.clear()",
            "fetch('https://example.com')",
            "new XMLHttpRequest()",
            "navigator.clipboard.readText()",
        ],
    )
    def test_restrict_blocks_direct_primitives(self, expression: str) -> None:
        result = enforce_eval_policy(
            expression, restrict_evaluate=True, allow_unsafe_evaluate=False
        )
        assert result is not None
        assert "restrict_evaluate" in result

    def test_restrict_blocks_bracket_obfuscation(self) -> None:
        # document["cookie"] must be caught even though document.cookie isn't spelled.
        assert (
            enforce_eval_policy(
                'document["cookie"]', restrict_evaluate=True, allow_unsafe_evaluate=False
            )
            is not None
        )

    def test_restrict_blocks_concatenation_obfuscation(self) -> None:
        assert (
            enforce_eval_policy(
                'document["coo" + "kie"]', restrict_evaluate=True, allow_unsafe_evaluate=False
            )
            is not None
        )

    def test_allow_unsafe_overrides_denylist(self) -> None:
        assert (
            enforce_eval_policy(
                "document.cookie", restrict_evaluate=True, allow_unsafe_evaluate=True
            )
            is None
        )

    def test_benign_expression_passes_even_when_restricted(self) -> None:
        assert (
            enforce_eval_policy(
                "document.title", restrict_evaluate=True, allow_unsafe_evaluate=False
            )
            is None
        )

    def test_reason_names_the_primitive(self) -> None:
        assert risky_eval_reason("document.cookie") == "document.cookie"
        assert risky_eval_reason("fetch('x')") == "network request"
        assert risky_eval_reason("document.title") is None


class TestUrlPreScan:
    def test_flags_metadata_endpoint(self) -> None:
        blocked = expression_targets_private_url("fetch('http://169.254.169.254/latest/meta-data')")
        assert blocked is not None
        assert "169.254.169.254" in blocked

    def test_flags_loopback(self) -> None:
        blocked = expression_targets_private_url("fetch('http://127.0.0.1:8080/secret')")
        assert blocked is not None

    def test_ignores_public_url(self) -> None:
        assert expression_targets_private_url("fetch('https://example.com/api')") is None

    def test_no_url_literal(self) -> None:
        assert expression_targets_private_url("document.title") is None

    def test_flags_protocol_relative_metadata(self) -> None:
        # `//host/path` inherits the page's scheme — same target, no scheme word
        # for the plain-text scanner to see (issue #1092).
        blocked = expression_targets_private_url("fetch('//169.254.169.254/latest/meta-data/')")
        assert blocked is not None
        assert "169.254.169.254" in blocked

    def test_flags_protocol_relative_loopback(self) -> None:
        blocked = expression_targets_private_url("fetch('//127.0.0.1:8080/admin')")
        assert blocked is not None

    def test_flags_split_string_scheme(self) -> None:
        # `'htt' + 'p://…'` hides the scheme across two literals; the
        # concatenation of decoded literals must reconstruct it (issue #1092).
        blocked = expression_targets_private_url(
            "fetch('htt' + 'p://169.254.169.254/latest/meta-data/')"
        )
        assert blocked is not None
        assert "169.254.169.254" in blocked

    def test_ignores_public_protocol_relative(self) -> None:
        # A public protocol-relative target stays allowed, same as `https://`.
        assert expression_targets_private_url("fetch('//example.com/api')") is None

    def test_ignores_concatenated_public_url(self) -> None:
        assert expression_targets_private_url("fetch('htt' + 'ps://example.com/api')") is None


class TestRedaction:
    def test_masks_string_secret(self) -> None:
        out = redact_browser_output("token sk-abc" + "defghijklmnopqrstuvwxyz0123456789")
        assert "defghijklmnopqrstuvwxyz0123456789" not in out

    def test_recurses_into_containers(self) -> None:
        payload = {"a": ["Bearer " + "x" * 40], "b": {"c": "plain text"}}
        out = redact_browser_output(payload)
        assert out["b"]["c"] == "plain text"
        assert isinstance(out["a"], list)

    def test_passes_through_non_strings(self) -> None:
        assert redact_browser_output(42) == 42
        assert redact_browser_output(True) is True
        assert redact_browser_output(None) is None
