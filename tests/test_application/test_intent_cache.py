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


class TestMultiCommandDeletes:
    """Intent extraction and caching for rmdir, rd, del, erase, unlink, Remove-Item."""

    def test_rmdir_and_rd_extraction(self) -> None:
        cache = IntentApprovalCache()
        cache.record("rmdir /s /q /a/b")
        assert cache.check("rmdir /s /q /a/b") is True
        assert cache.check("rd /s /q /a/b") is True
        assert cache.check("rmdir /s /q /a/c") is False

    def test_del_and_erase_extraction(self) -> None:
        cache = IntentApprovalCache()
        cache.record("del /f /q /tmp/file.txt")
        assert cache.check("del /f /q /tmp/file.txt") is True
        assert cache.check("erase /f /q /tmp/file.txt") is True
        assert cache.check("del /f /q /tmp/other.txt") is False

    def test_unlink_extraction(self) -> None:
        cache = IntentApprovalCache()
        cache.record("unlink /tmp/socket.sock")
        assert cache.check("unlink /tmp/socket.sock") is True
        assert cache.check("unlink /tmp/diff.sock") is False

    def test_powershell_remove_item_extraction(self) -> None:
        cache = IntentApprovalCache()
        cache.record("Remove-Item -Path /var/log/app.log -Force")
        assert cache.check("Remove-Item -Path /var/log/app.log -Force") is True
        assert cache.check("remove-item /var/log/app.log") is True
        assert cache.check("Remove-Item -Path /var/log/other.log") is False

