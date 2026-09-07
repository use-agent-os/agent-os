"""Regression tests for IntentApprovalCache compound-command bypass fix.

PR #546 fixes P1 security issue #512: when ``rm A; rm -rf /`` is checked
against a cache that only approved ``rm A``, the second ``rm`` must be
rejected. The fix uses ``re.finditer`` + shell-separator-aware tokenization
instead of ``re.search``, so each ``rm`` invocation is parsed independently.

See https://github.com/use-agent-os/agent-os/pull/546
"""

from __future__ import annotations

import pytest

from agentos.application.intent_cache import IntentApprovalCache, _extract_intents


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


class TestDestructivenessEscalation:
    """Issue #849: a non-recursive approval must not cover a recursive delete.

    ``rm /tmp/logs`` on a directory fails without ``-r``; the user who approved
    that prompt approved a no-op. The cache must not then let ``rm -rf
    /tmp/logs`` wipe it without a fresh prompt, because ``-rf`` never appeared
    on any prompt the user saw.
    """

    def test_plain_delete_does_not_cover_recursive_force(self) -> None:
        cache = IntentApprovalCache()
        cache.record("rm /tmp/a")
        assert cache.check("rm /tmp/a") is True
        assert cache.check("rm -rf /tmp/a") is False

    @pytest.mark.parametrize(
        "escalated",
        [
            "rm -r /tmp/a",
            "rm -R /tmp/a",
            "rm -f /tmp/a",
            "rm -rf /tmp/a",
            "rm -fr /tmp/a",
            "rm -vrf /tmp/a",
            "rm -r -f /tmp/a",
            "rm --recursive /tmp/a",
            "rm --force /tmp/a",
            "rm --recursive --force /tmp/a",
            "rm -rf -- /tmp/a",
        ],
    )
    def test_every_escalating_flag_spelling_is_blocked(self, escalated: str) -> None:
        cache = IntentApprovalCache()
        cache.record("rm /tmp/a")
        assert cache.check(escalated) is False

    def test_recursive_approval_covers_plain_delete(self) -> None:
        """De-escalation is fine — the user already approved the stronger op."""
        cache = IntentApprovalCache()
        cache.record("rm -rf /tmp/a")
        assert cache.check("rm -rf /tmp/a") is True
        assert cache.check("rm /tmp/a") is True
        assert cache.check("rm -r /tmp/a") is True

    def test_force_alone_does_not_cover_recursive(self) -> None:
        cache = IntentApprovalCache()
        cache.record("rm -f /tmp/a")
        assert cache.check("rm /tmp/a") is True
        assert cache.check("rm -rf /tmp/a") is False

    def test_double_dash_terminator_is_not_a_flag(self) -> None:
        cache = IntentApprovalCache()
        cache.record("rm /tmp/a")
        assert cache.check("rm -- /tmp/a") is True

    def test_escalated_entry_is_scoped_per_target(self) -> None:
        cache = IntentApprovalCache()
        cache.record("rm -rf /tmp/a")
        assert cache.check("rm -rf /tmp/b") is False

    def test_mixed_invocations_record_each_level(self) -> None:
        cache = IntentApprovalCache()
        cache.record("rm /tmp/a; rm -rf /tmp/b")
        assert cache.check("rm /tmp/a; rm -rf /tmp/b") is True
        assert cache.check("rm -rf /tmp/a; rm -rf /tmp/b") is False

    def test_expiry_applies_to_escalated_entries(self) -> None:
        cache = IntentApprovalCache()
        cache.record("rm -rf /tmp/a", ttl=-1)
        assert cache.check("rm /tmp/a") is False
        assert cache.check("rm -rf /tmp/a") is False

    def test_always_scope_survives_clear_scope_at_every_level(self) -> None:
        cache = IntentApprovalCache()
        cache.record_always("rm -rf /tmp/a")
        cache.clear_scope("once")
        assert cache.check("rm /tmp/a") is True
        assert cache.check("rm -rf /tmp/a") is True


class TestParaphraseStillWorks:
    """The module exists to stop prompt fatigue — paraphrases must still hit.

    Equal-destructiveness paraphrases (``rm`` -> ``os.remove``, ``rm -rf`` ->
    ``shutil.rmtree``) keep matching; only an *increase* in destructiveness
    re-prompts.
    """

    def test_plain_rm_covers_os_remove(self) -> None:
        cache = IntentApprovalCache()
        cache.record("rm /tmp/x")
        assert cache.check('os.remove("/tmp/x")') is True
        assert cache.check('Path("/tmp/x").unlink()') is True
        assert cache.check('os.rmdir("/tmp/x")') is True

    def test_recursive_rm_covers_rmtree(self) -> None:
        cache = IntentApprovalCache()
        cache.record("rm -rf /tmp/x")
        assert cache.check('shutil.rmtree("/tmp/x")') is True

    def test_rmtree_covers_os_remove(self) -> None:
        cache = IntentApprovalCache()
        cache.record('shutil.rmtree("/tmp/x")')
        assert cache.check('os.remove("/tmp/x")') is True

    def test_os_remove_does_not_cover_rmtree(self) -> None:
        cache = IntentApprovalCache()
        cache.record('os.remove("/tmp/x")')
        assert cache.check('shutil.rmtree("/tmp/x")') is False

    def test_rmdir_does_not_cover_removedirs(self) -> None:
        """``os.removedirs`` prunes empty parents — it deletes past its target."""
        cache = IntentApprovalCache()
        cache.record('os.rmdir("/tmp/x")')
        assert cache.check('os.removedirs("/tmp/x")') is False


class TestForgetAcrossLevels:
    """``/forget <path>`` builds a plain ``rm <path>`` — it must clear every level."""

    def test_forget_plain_clears_recursive_entry(self) -> None:
        cache = IntentApprovalCache()
        cache.record_always("rm -rf /tmp/a")
        cache.forget("rm /tmp/a")
        assert cache.check("rm /tmp/a") is False
        assert cache.check("rm -rf /tmp/a") is False

    def test_forget_recursive_clears_plain_entry(self) -> None:
        cache = IntentApprovalCache()
        cache.record_always("rm /tmp/a")
        cache.forget("rm -rf /tmp/a")
        assert cache.check("rm /tmp/a") is False


class TestIntentKindWireShape:
    """The diagnostic ``/approvals`` view renders ``kind:target`` verbatim."""

    def test_kind_encodes_capabilities(self) -> None:
        assert _extract_intents("rm /tmp/a")[0][0] == "delete"
        assert _extract_intents("rm -r /tmp/a")[0][0] == "delete:recursive"
        assert _extract_intents("rm -f /tmp/a")[0][0] == "delete:force"
        assert _extract_intents("rm -rf /tmp/a")[0][0] == "delete:recursive+force"
        assert _extract_intents('shutil.rmtree("/tmp/a")')[0][0] == "delete:recursive"


class TestAbbreviatedLongOptions:
    """``getopt_long`` accepts unambiguous abbreviations — so must the grader."""

    @pytest.mark.parametrize(
        "escalated",
        ["rm --rec /tmp/a", "rm --recur /tmp/a", "rm --fo /tmp/a", "rm --for --rec /tmp/a"],
    )
    def test_abbreviated_flags_are_graded(self, escalated: str) -> None:
        cache = IntentApprovalCache()
        cache.record("rm /tmp/a")
        assert cache.check(escalated) is False

    def test_unrelated_long_option_is_not_graded(self) -> None:
        cache = IntentApprovalCache()
        cache.record("rm /tmp/a")
        assert cache.check("rm --verbose /tmp/a") is True
        assert cache.check("rm --dir /tmp/a") is True

    def test_flags_after_the_target_still_count(self) -> None:
        """GNU rm permutes arguments — ``rm X -rf`` is a recursive delete."""
        cache = IntentApprovalCache()
        cache.record("rm /tmp/a")
        assert cache.check("rm /tmp/a -rf") is False

    def test_terminator_shields_a_filename_that_looks_like_flags(self) -> None:
        """After ``--`` the tokens are filenames, so the delete stays plain."""
        cache = IntentApprovalCache()
        # Two targets: /tmp/a and a file literally named "-rf". Both stay plain.
        assert {kind for kind, _ in _extract_intents("rm /tmp/a -- -rf")} == {"delete"}
        cache.record("rm /tmp/a -- -rf")
        assert cache.check("rm /tmp/a") is True
        assert cache.check("rm -rf /tmp/a") is False


class TestParentPruningIsItsOwnEscalation:
    """``os.removedirs`` reaches *above* its target — no ``rm`` spelling does."""

    def test_recursive_rm_does_not_cover_removedirs(self) -> None:
        cache = IntentApprovalCache()
        cache.record("rm -rf /tmp/build/cache")
        assert cache.check('shutil.rmtree("/tmp/build/cache")') is True
        assert cache.check('os.removedirs("/tmp/build/cache")') is False

    def test_removedirs_covers_the_plain_recursive_delete(self) -> None:
        cache = IntentApprovalCache()
        cache.record('os.removedirs("/tmp/build/cache")')
        assert cache.check("rm -r /tmp/build/cache") is True
        assert cache.check('shutil.rmtree("/tmp/build/cache")') is True


class TestDocumentedAsymmetries:
    """Boundaries that are deliberate, not accidental — pin them so they show up
    in review if anyone changes the grading table.
    """

    def test_every_shell_to_python_paraphrase_still_short_circuits(self) -> None:
        """The direction the module exists for is preserved in full."""
        for approved, retry in [
            ("rm /tmp/x", 'os.remove("/tmp/x")'),
            ("rm /tmp/x", 'Path("/tmp/x").unlink()'),
            ("rm -f /tmp/x", 'os.remove("/tmp/x")'),
            ("rm -r /tmp/x", 'shutil.rmtree("/tmp/x")'),
            ("rm -rf /tmp/x", 'shutil.rmtree("/tmp/x")'),
        ]:
            cache = IntentApprovalCache()
            cache.record(approved)
            assert cache.check(retry) is True, f"{approved!r} should cover {retry!r}"

    def test_python_recursive_delete_does_not_grant_the_force_flag(self) -> None:
        """``-f`` has no Python analogue, so the reverse costs one prompt."""
        cache = IntentApprovalCache()
        cache.record('shutil.rmtree("/tmp/x")')
        assert cache.check("rm -r /tmp/x") is True
        assert cache.check("rm -rf /tmp/x") is False

    def test_empty_directory_removal_is_deliberately_ungraded(self) -> None:
        """``rm -d`` and ``os.rmdir`` delete an empty dir plain ``rm`` refuses.

        Ungraded on purpose: an empty directory holds nothing, so grading it
        would cost a prompt and protect nothing.
        """
        cache = IntentApprovalCache()
        cache.record("rm /tmp/d")
        assert cache.check("rm -d /tmp/d") is True
        assert cache.check('os.rmdir("/tmp/d")') is True


class TestMultiCommandDeletes:
    """Intent extraction, grading, and caching for rmdir, rd, del, erase, unlink, Remove-Item."""

    def test_rmdir_and_rd_extraction_and_grading(self) -> None:
        cache = IntentApprovalCache()
        # rmdir /s is recursive
        assert _extract_intents("rmdir /s /q /a/b")[0][0] == "delete:recursive"
        assert _extract_intents("rd /s /q /a/b")[0][0] == "delete:recursive"
        # plain rmdir is plain delete
        assert _extract_intents("rmdir /a/b")[0][0] == "delete"

        cache.record("rmdir /s /q /a/b")
        assert cache.check("rmdir /s /q /a/b") is True
        assert cache.check("rd /s /q /a/b") is True
        assert cache.check("rmdir /a/b") is True  # de-escalation
        assert cache.check("rmdir /s /q /a/c") is False

        # Plain approval does not cover recursive
        plain_cache = IntentApprovalCache()
        plain_cache.record("rmdir /a/b")
        assert plain_cache.check("rmdir /a/b") is True
        assert plain_cache.check("rmdir /s /q /a/b") is False

    def test_rmdir_and_rd_paths_not_swallowed_as_switches(self) -> None:
        """/a, /p, /f as path targets must not be dropped as switches for rmdir/rd."""
        intents_a = _extract_intents("rmdir /a")
        assert len(intents_a) == 1
        assert intents_a[0][1].endswith("a")

        intents_p = _extract_intents("rd /p")
        assert len(intents_p) == 1
        assert intents_p[0][1].endswith("p")

        intents_f = _extract_intents("rmdir /f")
        assert len(intents_f) == 1
        assert intents_f[0][1].endswith("f")

    def test_del_and_erase_extraction_and_grading(self) -> None:
        cache = IntentApprovalCache()
        # del /f is force, del /s is recursive, del /f /s is recursive+force
        assert _extract_intents("del /f /q /tmp/file.txt")[0][0] == "delete:force"
        assert _extract_intents("erase /f /q /tmp/file.txt")[0][0] == "delete:force"
        assert _extract_intents("del /s /tmp/file.txt")[0][0] == "delete:recursive"
        assert _extract_intents("del /f /s /tmp/file.txt")[0][0] == "delete:recursive+force"
        assert _extract_intents("del /tmp/file.txt")[0][0] == "delete"

        cache.record("del /f /q /tmp/file.txt")
        assert cache.check("del /f /q /tmp/file.txt") is True
        assert cache.check("erase /f /q /tmp/file.txt") is True
        assert cache.check("del /tmp/file.txt") is True  # de-escalation
        assert cache.check("del /s /tmp/file.txt") is False  # force does not cover recursive
        assert cache.check("del /f /q /tmp/other.txt") is False

    def test_unlink_extraction(self) -> None:
        cache = IntentApprovalCache()
        assert _extract_intents("unlink /tmp/socket.sock")[0][0] == "delete"
        cache.record("unlink /tmp/socket.sock")
        assert cache.check("unlink /tmp/socket.sock") is True
        assert cache.check("unlink /tmp/diff.sock") is False

    def test_powershell_remove_item_extraction_and_grading(self) -> None:
        cache = IntentApprovalCache()
        # Parameter names (-Path, -LiteralPath) must not be extracted as targets
        intents = _extract_intents("Remove-Item -Path /var/log/app.log -Force")
        assert len(intents) == 1
        assert intents[0][0] == "delete:force"
        assert intents[0][1].endswith("app.log")
        assert not any("-path" in t.lower() for _, t in intents)

        intents_recurse = _extract_intents(
            "Remove-Item -LiteralPath /var/log/app.log -Recurse -Force"
        )
        assert len(intents_recurse) == 1
        assert intents_recurse[0][0] == "delete:recursive+force"

        cache.record("Remove-Item -Path /var/log/app.log -Force")
        assert cache.check("Remove-Item -Path /var/log/app.log -Force") is True
        assert cache.check("remove-item /var/log/app.log") is True
        assert cache.check("Remove-Item -Recurse -Path /var/log/app.log") is False
        assert cache.check("Remove-Item -Path /var/log/other.log") is False

    def test_command_position_anchoring_ignores_non_command_words(self) -> None:
        """Verbs appearing inside arguments of read-only commands must not extract intents."""
        assert _extract_intents('grep -rn "del" /etc/passwd') == []
        assert _extract_intents('grep -rn "rd" /etc/passwd') == []
        assert _extract_intents('grep -rn "rm" /etc/passwd') == []
        assert _extract_intents('echo "Remove-Item" /var/log') == []
        assert _extract_intents("cat /etc/rmdir.conf") == []
        assert _extract_intents("cat /var/log/order_details.txt") == []
        assert _extract_intents("cat /etc/card_reader.conf") == []
        assert _extract_intents("python train_model.py") == []
        assert _extract_intents("ri String#length") == []
        assert _extract_intents("git rd branch_name") == []

    def test_compound_and_chained_commands_extract_correctly(self) -> None:
        """Command separators and subshells correctly scope delete intents."""
        intents_semi = _extract_intents("echo hello; del /f /q C:\\test.txt")
        assert len(intents_semi) == 1
        assert intents_semi[0][0] == "delete:force"
        assert intents_semi[0][1].endswith("test.txt")

        intents_and = _extract_intents("cd /tmp && rmdir /s /q test_dir")
        assert len(intents_and) == 1
        assert intents_and[0][0] == "delete:recursive"
        assert intents_and[0][1].endswith("test_dir")

        intents_subshell = _extract_intents("(Remove-Item -Force /tmp/sub.txt)")
        assert len(intents_subshell) == 1
        assert intents_subshell[0][0] == "delete:force"
        assert intents_subshell[0][1].endswith("sub.txt")

