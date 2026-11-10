"""Browser eval policy: denylist (direct + obfuscated), SSRF pre-scan, redaction.

Pure functions — no engine, no browser.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

import pytest

from agentos.tools import ssrf
from agentos.tools.browser_eval_policy import (
    enforce_eval_policy,
    expression_targets_private_url,
    redact_browser_output,
    risky_eval_reason,
)

# Names the stub resolver answers; every other name fails like NXDOMAIN. IP
# literals resolve to themselves, as the real resolver does without a query.
_FAKE_DNS = {
    "example.com": "93.184.215.14",
    "intranet.test": "10.0.0.5",
}


def _fake_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> list[Any]:
    name = str(host).strip("[]").lower().rstrip(".")
    try:
        addr = ipaddress.ip_address(name)
    except ValueError:
        answer = _FAKE_DNS.get(name)
        if answer is None:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known") from None
        addr = ipaddress.ip_address(answer)
    family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (str(addr), port or 0))]


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
    @pytest.fixture(autouse=True)
    def _offline_resolver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The pre-scan resolves hostnames through ``agentos.tools.ssrf``. A real
        # resolver makes these tests depend on the network, and some resolvers
        # answer even for ``.invalid``, so name resolution is stubbed.
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)

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

    def test_flags_protocol_relative_metadata_endpoint(self) -> None:
        # `//host/…` inherits the page's scheme and reaches the same address.
        # The scan only matched `http(s)://` literals, so this spelling of the
        # metadata endpoint walked straight past the eval action's only network
        # guard — the post-eval page-URL recheck fires on navigation, and a
        # bare fetch never navigates.
        blocked = expression_targets_private_url("fetch('//169.254.169.254/latest/meta-data/')")
        assert blocked is not None
        assert "169.254.169.254" in blocked

    def test_flags_protocol_relative_loopback(self) -> None:
        blocked = expression_targets_private_url("fetch('//127.0.0.1:8080/admin')")
        assert blocked is not None
        assert "127.0.0.1" in blocked

    def test_flags_split_protocol(self) -> None:
        # Splitting the scheme across two literals defeated a scan that only
        # looked at the raw expression text.
        blocked = expression_targets_private_url(
            "fetch('htt' + 'p://169.254.169.254/latest/meta-data/')"
        )
        assert blocked is not None
        assert "169.254.169.254" in blocked

    def test_flags_split_protocol_relative_host(self) -> None:
        blocked = expression_targets_private_url("fetch('//169.254.' + '169.254/latest/')")
        assert blocked is not None
        assert "169.254.169.254" in blocked

    def test_line_comment_is_not_a_url(self) -> None:
        # `//` starts a JS line comment far more often than a URL, so the
        # protocol-relative match is confined to string literals. Without that
        # confinement an ordinary comment would be scanned as a hostname and
        # refused when it failed to resolve.
        assert expression_targets_private_url("// grab the title\ndocument.title") is None
        assert expression_targets_private_url("//no-space-comment\ndocument.title") is None

    def test_dynamically_built_url_is_not_refused(self) -> None:
        # Concatenating every literal to defeat a split protocol also glues
        # together the fragments of a URL whose host is a variable. The result
        # (`https:///api`) has no host, names no address, and must not be read
        # as a private target.
        assert expression_targets_private_url("fetch('https://' + host + '/api')") is None

    def test_unresolvable_derived_host_is_not_refused(self) -> None:
        # A `//…` fragment concatenated onto a base URL is indistinguishable
        # from a protocol-relative target until the host is resolved, so a
        # derived candidate only counts once it resolves to a private or
        # metadata address. `.invalid` never resolves (RFC 2606).
        assert expression_targets_private_url("fetch(base + '//not-a-host.invalid/x')") is None

    def test_derived_host_resolving_to_private_is_refused(self) -> None:
        # The other half of the derived-candidate rule: a protocol-relative
        # literal whose name does resolve to a private address is refused.
        blocked = expression_targets_private_url("fetch('//intranet.test/admin')")
        assert blocked is not None
        assert "intranet.test" in blocked

    def test_unresolvable_plain_literal_is_still_refused(self) -> None:
        # Deliberate asymmetry: a URL the model wrote out in full is not a
        # guess, so the strict pre-scan keeps failing closed on it.
        assert expression_targets_private_url("fetch('http://not-a-host.invalid/x')") is not None

    def test_division_is_not_a_url(self) -> None:
        assert expression_targets_private_url("var r = a / b / c; r") is None

    def test_dom_extraction_still_allowed(self) -> None:
        assert (
            expression_targets_private_url(
                "[...document.querySelectorAll('a')].map(a => a.textContent)"
            )
            is None
        )


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
