"""Regression tests for IntentApprovalCache compound-command bypass fix.

PR #546 fixes P1 security issue #512: when ``rm A; rm -rf /`` is checked
against a cache that only approved ``rm A``, the second ``rm`` must be
rejected. The fix uses ``re.finditer`` + shell-separator-aware tokenization
instead of ``re.search``, so each ``rm`` invocation is parsed independently.

See https://github.com/use-agent-os/agent-os/pull/546
"""

from __future__ import annotations

from agentos.application.intent_cache import IntentApprovalCache


class TestCompoundCommandSeparatorBypass:
    """Every shell separator must be caught by the permission cache.

    A single approved ``rm /a`` followed by a second ``rm /b`` via any of the
    six shell separators (``;``, ``&&``, ``||``, ``|``, ``&``, ``\\n``) must
    return ``False`` — the untargeted path was never approved.
    """

    def _check_separator(self, separator: str) -> None:
        cache = IntentApprovalCache()
        cache.record("rm /a")
        assert cache.check("rm /a") is True
        assert cache.check(f"rm /a{separator} rm /b") is False, (
            f"check('rm /a{separator} rm /b') should be False"
        )

    def test_semicolon(self) -> None:
        self._check_separator(";")

    def test_and_and(self) -> None:
        self._check_separator(" && ")

    def test_or_or(self) -> None:
        self._check_separator(" || ")

    def test_pipe(self) -> None:
        self._check_separator(" | ")

    def test_ampersand(self) -> None:
        self._check_separator(" & ")

    def test_newline(self) -> None:
        self._check_separator("\n")


class TestMultiTargetApproval:
    """Multi-target commands must require approval for all targets."""

    def test_all_targets_approved_passes(self) -> None:
        """rm /a /b recorded -> check('rm /a /b') is True."""
        cache = IntentApprovalCache()
        cache.record("rm /a /b")
        assert cache.check("rm /a /b") is True

    def test_extra_target_not_approved_fails(self) -> None:
        """rm /a /b recorded -> check('rm /a /b /c') is False — /c not approved."""
        cache = IntentApprovalCache()
        cache.record("rm /a /b")
        assert cache.check("rm /a /b /c") is False


class TestRecordAndCheck:
    """Basic record/check lifecycle."""

    def test_empty_command_returns_false(self) -> None:
        cache = IntentApprovalCache()
        assert cache.check("") is False

    def test_non_rm_command_returns_false(self) -> None:
        cache = IntentApprovalCache()
        cache.record("rm /a")
        assert cache.check("echo hello") is False

    def test_record_always_survives_clear_scope(self) -> None:
        cache = IntentApprovalCache()
        cache.record_always("rm /a")
        cache.clear_scope("once")
        assert cache.check("rm /a") is True

    def test_forget_removes_entry(self) -> None:
        cache = IntentApprovalCache()
        cache.record("rm /a")
        assert cache.check("rm /a") is True
        cache.forget("rm /a")
        assert cache.check("rm /a") is False

    def test_clear_drops_all(self) -> None:
        cache = IntentApprovalCache()
        cache.record("rm /a")
        cache.record("rm /b")
        cache.clear()
        assert cache.check("rm /a") is False
        assert cache.check("rm /b") is False


class TestDestructiveCommands:
    """Non-rm deletion commands must be recognised by the intent cache."""

    def test_rmdir_recognised(self) -> None:
        """rmdir /s /q / extracts root target."""
        cache = IntentApprovalCache()
        cache.record("rmdir /s /q /")
        assert cache.check("rmdir /s /q /") is True

    def test_rmdir_separator_bypass(self) -> None:
        """rmdir /a approved -> rmdir /a; rmdir /b is blocked."""
        cache = IntentApprovalCache()
        cache.record("rmdir /a")
        assert cache.check("rmdir /a") is True
        assert cache.check("rmdir /a; rmdir /b") is False

    def test_rd_recognised(self) -> None:
        """rd /s /q / extracts root target."""
        cache = IntentApprovalCache()
        cache.record("rd /s /q /")
        assert cache.check("rd /s /q /") is True

    def test_del_recognised(self) -> None:
        """del /f /q ~/.ssh/id_rsa extracts sensitive path."""
        cache = IntentApprovalCache()
        cache.record("del /f /q ~/.ssh/id_rsa")
        assert cache.check("del /f /q ~/.ssh/id_rsa") is True

    def test_del_sensitive_path_blocked(self) -> None:
        """del /f approved -> del /f; del /f /etc/passwd not approved."""
        cache = IntentApprovalCache()
        cache.record("del /f /tmp/foo")
        assert cache.check("del /f /tmp/foo") is True
        assert cache.check("del /f /tmp/foo; del /f /etc/passwd") is False

    def test_erase_recognised(self) -> None:
        """erase /f /q C:\\ extracts target (Windows)."""
        cache = IntentApprovalCache()
        cache.record("erase /f /q C:\\")
        assert cache.check("erase /f /q C:\\") is True

    def test_unlink_recognised(self) -> None:
        """unlink target.txt extracts target."""
        cache = IntentApprovalCache()
        cache.record("unlink target.txt")
        assert cache.check("unlink target.txt") is True

    def test_remove_item_recognised(self) -> None:
        """Remove-Item -Recurse -Force / extracts root target."""
        cache = IntentApprovalCache()
        cache.record("Remove-Item -Recurse -Force /")
        assert cache.check("Remove-Item -Recurse -Force /") is True

    def test_remove_item_separator_bypass(self) -> None:
        """Remove-Item /a -> Remove-Item /a; Remove-Item /b blocked."""
        cache = IntentApprovalCache()
        cache.record("Remove-Item /a")
        assert cache.check("Remove-Item /a") is True
        assert cache.check("Remove-Item /a; Remove-Item /b") is False

    def test_mixed_commands_independent(self) -> None:
        """rm /a; rmdir /b; Remove-Item /c each parsed independently."""
        cache = IntentApprovalCache()
        cache.record("rm /a")
        assert cache.check("rm /a; rmdir /b") is False  # /b not approved
        cache.record("rmdir /b")
        assert cache.check("rm /a; rmdir /b") is True  # both now approved

    def test_non_rm_returns_false(self) -> None:
        """Commands without destructive intent still return False."""
        cache = IntentApprovalCache()
        assert cache.check("rmdir") is False  # rmdir alone, no target
        assert cache.check("del \n") is False  # del alone, no target
        assert cache.check("echo hello") is False
        assert cache.check("") is False
