"""Tests for the env write-policy gate."""

from __future__ import annotations

import pytest

from agentos import env_policy
from agentos.env_policy import EnvPolicyError


class TestNameValidation:
    @pytest.mark.parametrize("name", ["A", "_A", "OPENAI_API_KEY", "a1", "_", "X_9_Z"])
    def test_accepts_portable_names(self, name: str) -> None:
        env_policy.assert_valid_name(name)

    @pytest.mark.parametrize("name", ["", "1BAD", "A-B", "A B", "A.B", "127_0_0_1", "é"])
    def test_rejects_non_portable_names(self, name: str) -> None:
        with pytest.raises(EnvPolicyError, match="Invalid environment variable name"):
            env_policy.assert_valid_name(name)

    def test_rejects_non_string(self) -> None:
        with pytest.raises(EnvPolicyError):
            env_policy.assert_valid_name(None)  # type: ignore[arg-type]


class TestDenylist:
    @pytest.mark.parametrize(
        "name",
        [
            # loader / linker
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "DYLD_INSERT_LIBRARIES",
            # interpreter
            "PYTHONPATH",
            "PYTHONSTARTUP",
            "NODE_OPTIONS",
            # shell and implicitly-invoked commands
            "PATH",
            "SHELL",
            "EDITOR",
            "BASH_ENV",
            "GIT_SSH_COMMAND",
            # AgentOS posture and state location
            "AGENTOS_SENSITIVE_PATHS_DISABLED",
            "AGENTOS_SAFE_BIN_ALLOW",
            "AGENTOS_AGENT_PERMISSIONS",
            "AGENTOS_TRUST_ENV",
            "AGENTOS_GATEWAY_TOKEN",
            "AGENTOS_STATE_DIR",
            "AGENTOS_GATEWAY_PORT",
            "AGENTOS_HTTP_DOWNLOAD_LIMIT",
            # egress steering
            "AGENTOS_LLM_PROXY",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
        ],
    )
    def test_execution_and_posture_names_are_refused(self, name: str) -> None:
        assert env_policy.is_writable(name) is False
        with pytest.raises(EnvPolicyError, match="cannot be written through AgentOS"):
            env_policy.assert_writable(name)

    @pytest.mark.parametrize(
        "name",
        [
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "BASE_RPC_URL",
            "TELEGRAM_BOT_TOKEN",
            # AGENTOS_ is NOT blanket-blocked: ordinary credentials use it too.
            "AGENTOS_LLM_API_KEY",
            "AGENTOS_LLM_PROVIDER",
        ],
    )
    def test_ordinary_credentials_stay_writable(self, name: str) -> None:
        assert env_policy.is_writable(name) is True
        env_policy.assert_writable(name)

    def test_invalid_name_is_not_writable_even_when_absent_from_denylist(self) -> None:
        assert env_policy.is_writable("1BAD") is False

    def test_proxy_names_are_denied_in_both_conventional_cases(self) -> None:
        """Libraries honour ``http_proxy`` as readily as ``HTTP_PROXY``.

        Denying only the upper-case spelling would leave the exfiltration path
        of issue #550 open through the lower-case one.
        """
        for upper in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
            assert upper in env_policy.WRITE_DENYLIST
            assert upper.lower() in env_policy.WRITE_DENYLIST

    @pytest.mark.parametrize(
        "name",
        [
            "Http_Proxy",
            "hTTps_PrOxY",
            "All_Proxy",
            "No_Proxy",
            "Agentos_Llm_Proxy",
        ],
    )
    def test_proxy_names_are_denied_in_any_casing(self, name: str) -> None:
        """Exact-case matching would leave a mixed-case bypass.

        ``agentos.env`` pushes ``.env`` keys into ``os.environ`` verbatim, and
        ``urllib.request.getproxies_environment()`` — what httpx reads proxies
        through — lower-cases every name it finds, so ``Http_Proxy`` routes
        traffic exactly as ``HTTP_PROXY`` does.
        """
        assert env_policy.is_writable(name) is False
        with pytest.raises(EnvPolicyError, match="cannot be written through AgentOS"):
            env_policy.assert_writable(name)

    def test_case_insensitivity_does_not_leak_to_the_rest_of_the_denylist(self) -> None:
        """Only the proxy family is matched case-insensitively.

        ``path`` and ``Ld_Preload`` are not names any loader or shell reads, and
        widening the whole denylist would start refusing ordinary lower-case
        application variables.
        """
        assert env_policy.is_writable("path") is True
        assert env_policy.is_writable("Ld_Preload") is True

    def test_trust_env_cannot_be_written(self) -> None:
        """``AGENTOS_TRUST_ENV`` gates whether the ambient ``*_PROXY`` names are
        honoured at all, so a writable value re-opens the same route."""
        with pytest.raises(EnvPolicyError, match="cannot be written through AgentOS"):
            env_policy.assert_writable("AGENTOS_TRUST_ENV")

    def test_llm_proxy_cannot_be_written_while_llm_settings_still_can(self) -> None:
        """The denial is name-by-name, not a block on the LLM config family."""
        with pytest.raises(EnvPolicyError, match="cannot be written through AgentOS"):
            env_policy.assert_writable("AGENTOS_LLM_PROXY")
        env_policy.assert_writable("AGENTOS_LLM_BASE_URL")
        env_policy.assert_writable("AGENTOS_LLM_MODEL")


class TestSanitizeValue:
    @pytest.mark.parametrize("value", ["", "plain", "with space", "tab\there", "unicode-á", "#h"])
    def test_accepts_single_line_values(self, value: str) -> None:
        assert env_policy.sanitize_value("K", value) == value

    @pytest.mark.parametrize("value", ["a\nb", "a\r\nb", "a\r", "trailing\n"])
    def test_rejects_line_breaks_instead_of_truncating(self, value: str) -> None:
        # Silently stripping would store a mangled credential that fails much
        # later; refusing surfaces the problem at the point of the mistake.
        with pytest.raises(EnvPolicyError, match="line break"):
            env_policy.sanitize_value("K", value)

    @pytest.mark.parametrize("value", ["a\x00b", "a\x07b", "a\x7fb"])
    def test_rejects_control_characters(self, value: str) -> None:
        with pytest.raises(EnvPolicyError, match="control character"):
            env_policy.sanitize_value("K", value)

    @pytest.mark.parametrize(
        "value",
        ["head\u0085tail", "head\u2028tail", "head\u2029tail"],
        ids=["next-line", "line-separator", "paragraph-separator"],
    )
    def test_rejects_the_invisible_separators_the_reader_splits_on(self, value: str) -> None:
        # U+0085, U+2028 and U+2029 are not C0, so the control-character gate
        # never saw them, and the reader's splitlines() breaks on all three:
        # the value was written whole and read back truncated at the separator.
        with pytest.raises(EnvPolicyError, match="line break"):
            env_policy.sanitize_value("K", value)

    @pytest.mark.parametrize(
        "char",
        list("\n\r\v\f\x1c\x1d\x1e\u0085\u2028\u2029"),
        ids=[
            "lf",
            "cr",
            "vt",
            "ff",
            "fs",
            "gs",
            "rs",
            "nel",
            "line-separator",
            "paragraph-separator",
        ],
    )
    def test_every_character_the_reader_splits_on_is_refused(self, char: str) -> None:
        """The refused set is derived from the reader, not kept by hand.

        ``agentos.env._parse_env_file`` reads with ``str.splitlines()``, so a
        value holding any character it breaks on cannot survive a write/read
        cycle whatever this module's message says.
        """
        assert len(f"a{char}b".splitlines()) == 2
        with pytest.raises(EnvPolicyError):
            env_policy.sanitize_value("K", f"a{char}b")

    @pytest.mark.parametrize(
        "value",
        ["a\u00a0b", "a\u2007b", "a\u200bb", "a\u3000b"],
        ids=["nbsp", "figure-space", "zero-width-space", "ideographic-space"],
    )
    def test_still_accepts_invisible_characters_that_are_not_line_breaks(self, value: str) -> None:
        # The gate is about what splits a line, not about what is invisible:
        # a non-breaking space inside a passphrase is still storable.
        assert env_policy.sanitize_value("K", value) == value

    def test_rejects_non_string(self) -> None:
        with pytest.raises(EnvPolicyError, match="must be a string"):
            env_policy.sanitize_value("K", 42)  # type: ignore[arg-type]


class TestSecretHeuristics:
    @pytest.mark.parametrize(
        "name",
        ["OPENAI_API_KEY", "SLACK_BOT_TOKEN", "APP_SECRET", "DB_PASSWORD", "VERTEX_CREDENTIALS"],
    )
    def test_credential_shaped_names_are_secret(self, name: str) -> None:
        assert env_policy.is_secret_name(name) is True

    @pytest.mark.parametrize("name", ["BASE_RPC_URL", "OPENAI_BASE_URL", "LOG_LEVEL", "TZ"])
    def test_plain_settings_are_not_secret(self, name: str) -> None:
        assert env_policy.is_secret_name(name) is False


class TestMask:
    def test_none_stays_none(self) -> None:
        assert env_policy.mask(None) is None

    def test_short_values_are_replaced_wholesale(self) -> None:
        # Revealing 4 of 6 characters is not masking.
        masked = env_policy.mask("short")
        assert masked == "•" * 8
        assert "short" not in masked

    def test_long_values_keep_only_recognisable_edges(self) -> None:
        value = "sk-proj-" + "x" * 40 + "wxyz"
        masked = env_policy.mask(value)
        assert masked == "sk-p…wxyz"
        assert value not in masked


class TestOperatorOnlyConfigEnv:
    """``.env`` is the config at the next boot: no surface may write a guardrail there."""

    @pytest.mark.parametrize(
        "name",
        [
            "AGENTOS_TRADING_DAILY_CAP_USD",
            "AGENTOS_TRADING_APPROVAL_THRESHOLD_USD",
            "AGENTOS_GATEWAY_TRADING__DAILY_CAP_USD",
            "AGENTOS_GATEWAY_TRADING",
            "agentos_gateway_sandbox__sandbox",
            "AGENTOS_SANDBOX_SANDBOX",
            "AGENTOS_GATEWAY_PERMISSIONS__DEFAULT_MODE",
            "AGENTOS_GATEWAY_HOOKS__LIFECYCLE",
            "AGENTOS_GATEWAY_TOOLS__INVOKE__ALLOW",
            "AGENTOS_GATEWAY_AUTH__MODE",
            "AGENTOS_AUTH_MODE",
            "AGENTOS_GATEWAY_SAFETY__INJECTION_SCAN_MODE",
            "AGENTOS_GATEWAY_WORKSPACE_STRICT",
            "AGENTOS_GATEWAY_WORKSPACE_DIR",
            "AGENTOS_GATEWAY_ACP__PERMISSION_MODE",
        ],
    )
    def test_refused(self, name: str) -> None:
        assert not env_policy.is_writable(name)
        with pytest.raises(env_policy.EnvPolicyError):
            env_policy.assert_writable(name)

    @pytest.mark.parametrize(
        "name", ["AGENTOS_LLM_API_KEY", "OPENROUTER_API_KEY", "AGENTOS_GATEWAY_TRADINGX"]
    )
    def test_ordinary_names_still_writable(self, name: str) -> None:
        assert env_policy.is_writable(name)

    def test_roots_match_the_gateways_list(self) -> None:
        from agentos.gateway.config_commit import OPERATOR_ONLY_CONFIG_ROOTS

        assert env_policy._OPERATOR_ONLY_CONFIG_ROOTS == OPERATOR_ONLY_CONFIG_ROOTS
