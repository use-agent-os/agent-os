"""Regression tests for IntentApprovalCache compound-command bypass fix.

PR #546 fixes P1 security issue #512: when ``rm A; rm -rf /`` is checked
against a cache that only approved ``rm A``, the second ``rm`` must be
rejected. The fix uses ``re.finditer`` + shell-separator-aware tokenization
instead of ``re.search``, so each ``rm`` invocation is parsed independently.

See https://github.com/use-agent-os/agent-os/pull/546
"""

from __future__ import annotations

import os

import pytest

from agentos.application.intent_cache import (
    IntentApprovalCache,
    _extract_intents,
    _extract_shell_delete_targets,
)
from agentos.sandbox.sensitive_paths import sensitive_target_in_command

#: The Windows spelling these cases are written in. POSIX ``shlex`` strips the
#: backslash on a POSIX host, where cmd.exe does not run anyway, so the token
#: arrives as ``C:tmp`` there. The grade is what the cases assert; the token
#: shape is platform-dependent and named here rather than sidestepped by
#: writing a path without a separator.
WINDOWS_PATH = "C:\\tmp"
WINDOWS_TARGET = WINDOWS_PATH if os.name == "nt" else "C:tmp"


def _names_etc(targets: list[str]) -> bool:
    """Whether any extracted target names ``/etc``, on either path separator.

    Windows resolves a drive-relative ``/etc`` against the working drive, so
    the extracted target arrives as ``D:\\etc`` on the CI runner. Normalising
    the separator keeps one assertion honest on every platform, rather than
    gating the case behind ``skipif`` and losing it on half of CI.
    """
    return any(target.replace("\\", "/").endswith("/etc") for target in targets)


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


class TestQuotedRmIsNotACommand:
    """A ``rm`` inside a quoted argument is text, not a delete (#1349).

    The hard block these intents feed survives user approval, so a false
    positive here cannot be approved past — only ``/elevated full`` clears it.
    """

    @pytest.mark.parametrize(
        "command",
        [
            'grep -rn "rm" /etc/passwd',
            'git commit -m "rm the old config" /etc/hosts',
            'echo "use rm carefully" >> /root/notes.md',
            "echo 'rm -rf /' > note.txt",
            'rg --fixed-strings "rm -rf" /var/log/syslog',
        ],
    )
    def test_read_only_command_mentioning_rm_extracts_nothing(self, command: str) -> None:
        assert _extract_intents(command) == []

    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf /etc",
            "sudo rm -rf /etc",
            "env FOO=1 rm -rf /etc",
            "time rm -rf /etc",
            "nohup rm -rf /etc",
            "find . -name '*.log' | xargs rm -rf /etc",
            "cd /tmp && rm -rf /etc",
        ],
    )
    def test_real_deletes_are_still_extracted(self, command: str) -> None:
        # Guards against the tempting fix: requiring `rm` to start the string
        # or follow a separator reads as tighter but drops every one of these
        # command prefixes, trading a false positive for a bypass.
        targets = [target for _kind, target in _extract_intents(command)]
        assert _names_etc(targets), targets

    @pytest.mark.parametrize(
        "command",
        [
            'sh -c "rm -rf /etc/passwd"',
            'bash -c "rm -rf /root/.ssh/id_rsa"',
            "bash -c 'rm -rf /etc/'",
            'sh -c "rm -rf /etc /tmp"',
            'ssh h "rm -rf /var/log/x"',
            'ssh -p 22 host "rm -rf /var/log/x"',
            'sh -c "rm -rf ~/.ssh/id_rsa"',
            'docker exec c sh -c "rm -rf /etc/nginx"',
            'sh -c "rm -rf /boot/vmlinuz"',
            '/bin/sh -c "rm -rf /etc/passwd"',
            'bash -lc "rm -rf /etc/passwd"',
            # An option between the shell and ``-c`` moves the name off
            # ``tokens[-2]``. Requiring it there made each of these read as
            # data, losing the hard block; they are ordinary CI glue.
            'bash --login -c "rm -rf /etc/passwd"',
            'bash -e -c "rm -rf /etc/passwd"',
            'sh -e -c "rm -rf /etc/passwd"',
            'bash -o pipefail -c "rm -rf /etc/passwd"',
            'sh --norc -c "rm -rf /etc/passwd"',
            'bash --noprofile --norc -c "rm -rf /etc/passwd"',
        ],
    )
    def test_a_quoted_rm_a_shell_will_run_is_still_a_delete(self, command: str) -> None:
        """A quoted span is data only until something runs it.

        Skipping every quoted ``rm`` also skipped these, and
        ``_extract_intents`` is the only input to
        ``sensitive_target_in_command`` — so that is a hard block lost, not an
        approval-cache entry lost. The read-only cases above and these are the
        two halves of the same rule; asserting only one of them cannot tell the
        fix from the regression.
        """
        from agentos.sandbox.sensitive_paths import sensitive_target_in_command

        assert sensitive_target_in_command(command) is not None, command

    @pytest.mark.parametrize(
        "command",
        [
            'echo "bash -e -c rm -rf /etc"',
            'echo "bash -o pipefail -c rm -rf /etc"',
            'git commit -m -c "rm the old config" ',
        ],
    )
    def test_scanning_back_for_the_shell_does_not_widen_to_other_commands(
        self, command: str
    ) -> None:
        """The relaxation is bounded on both sides.

        Looking for the shell name anywhere before ``-c`` must not make a
        quoted mention of one executable: the introducer still has to be
        outside the quotes, and a prefix that is some other command stops the
        scan before any shell name could be reached.
        """
        from agentos.sandbox.sensitive_paths import sensitive_target_in_command

        assert sensitive_target_in_command(command) is None, command

    def test_a_quoted_argument_of_an_ordinary_command_stays_data(self) -> None:
        """The introducer is what matters, not the quoting.

        ``grep`` does not execute its pattern, so the #1349 false positive has
        to stay fixed even though the spelling looks identical.
        """
        from agentos.sandbox.sensitive_paths import sensitive_target_in_command

        assert sensitive_target_in_command('grep -rn "rm" /etc/passwd') is None
        assert sensitive_target_in_command('echo "rm -rf /"') is None
        assert sensitive_target_in_command('cat "rm notes.txt"') is None

    def test_quoted_and_real_rm_in_one_command(self) -> None:
        # The quoted mention is skipped; the real invocation after the
        # separator is not.
        intents = _extract_intents('echo "rm this later"; rm -rf /etc')
        targets = [target for _kind, target in intents]
        assert _names_etc(targets), targets
        assert not any(target.endswith("later") for target in targets), targets

    def test_unbalanced_quote_leaves_the_rest_quoted(self) -> None:
        # An unclosed quote quotes the remainder, which is what the shell does
        # with it too, so nothing after it is read as a command.
        assert _extract_intents('echo "rm -rf /etc') == []


class TestNonRmDeletionSpellings:
    """Issue #1015: only ``rm`` was extracted, so every other delete slipped past.

    ``_extract_shell_delete_targets`` is asserted directly here rather than
    through ``_extract_intents`` — raw targets arrive exactly as typed, so the
    cases stay readable and platform-independent without a path-normalisation
    helper standing between the command and the assertion.
    """

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("rmdir /tmp/empty", [("/tmp/empty", frozenset())]),
            ("rd /tmp/empty", [("/tmp/empty", frozenset())]),
            ("del /tmp/a", [("/tmp/a", frozenset())]),
            ("erase /tmp/a", [("/tmp/a", frozenset())]),
            ("unlink /tmp/a", [("/tmp/a", frozenset())]),
            ("Remove-Item /tmp/a", [("/tmp/a", frozenset())]),
        ],
    )
    def test_every_enumerated_spelling_yields_its_target(
        self, command: str, expected: list[tuple[str, frozenset[str]]]
    ) -> None:
        assert _extract_shell_delete_targets(command) == expected

    def test_rm_extraction_is_unchanged(self) -> None:
        """The guard: widening the alternation must not disturb ``rm`` itself."""
        assert _extract_shell_delete_targets("rm -rf /etc") == [
            ("/etc", frozenset({"recursive", "force"}))
        ]

    def test_rmdir_is_not_consumed_as_a_bare_rm(self) -> None:
        """``rm`` comes after ``rmdir`` in the alternation for exactly this."""
        assert _extract_shell_delete_targets("rmdir -p /a/b") == [("/a/b", frozenset({"parents"}))]

    def test_bare_command_has_no_target(self) -> None:
        assert _extract_shell_delete_targets("rmdir") == []
        assert _extract_shell_delete_targets("del") == []

    def test_each_invocation_keeps_its_own_capabilities(self) -> None:
        """The ``/s`` on the second must not leak onto the first."""
        targets = _extract_shell_delete_targets("del /tmp/a; rd /s /tmp/b")
        assert ("/tmp/a", frozenset()) in targets
        assert ("/tmp/b", frozenset({"recursive"})) in targets


class TestNonRmDeletionGrading:
    """Each spelling's escalating flags must grade, or approvals escalate silently."""

    @pytest.mark.parametrize(
        ("command", "capabilities"),
        [
            ("rd /s C:\\tmp", {"recursive"}),
            ("rd /q C:\\tmp", {"force"}),
            ("rd /s /q C:\\tmp", {"recursive", "force"}),
            ("rmdir /S /Q C:\\tmp", {"recursive", "force"}),
            ("del /f C:\\tmp", {"force"}),
            ("del /s C:\\tmp", {"recursive"}),
            ("erase /f /q C:\\tmp", {"force"}),
            ("rmdir -p /a/b", {"parents"}),
            ("rmdir --parents /a/b", {"parents"}),
            ("Remove-Item -Recurse /a", {"recursive"}),
            ("Remove-Item -Force /a", {"force"}),
            ("Remove-Item -Recurse -Force /a", {"recursive", "force"}),
            ("unlink /a", set()),
        ],
    )
    def test_flags_grade_the_invocation(self, command: str, capabilities: set[str]) -> None:
        targets = _extract_shell_delete_targets(command)
        graded = [caps for target, caps in targets if not target.startswith("/s")]
        assert any(caps == frozenset(capabilities) for caps in graded), targets

    def test_powershell_parameters_are_prefix_matched(self) -> None:
        """PowerShell accepts any unambiguous abbreviation, so ``-rec`` recurses."""
        assert _extract_shell_delete_targets("Remove-Item -rec -f /a") == [
            ("/a", frozenset({"recursive", "force"}))
        ]

    def test_windows_command_names_are_case_insensitive(self) -> None:
        """cmd.exe and PowerShell resolve their own names without regard to case."""
        assert _extract_shell_delete_targets("DEL /F C:\\tmp")
        assert _extract_shell_delete_targets("REMOVE-ITEM -Recurse /a") == [
            ("/a", frozenset({"recursive"}))
        ]

    def test_rm_stays_case_sensitive(self) -> None:
        """POSIX ``rm`` is spelled in lower case; widening it was not asked for."""
        assert _extract_shell_delete_targets("RM -rf /etc") == []

    def test_end_of_flags_is_honoured(self) -> None:
        assert _extract_shell_delete_targets("rmdir -- -p") == [("-p", frozenset())]


class TestNonRmDeletionEscalation:
    """The direction the issue did not report: approvals must not escalate.

    #1015 asks for these commands to be *seen*. Being seen is not enough — a
    plain ``del`` approval covering ``del /s`` would hand back the very
    escalation #849 closed for ``rm``.
    """

    @pytest.mark.parametrize(
        ("approved", "escalated"),
        [
            ("rd C:\\tmp", "rd /s C:\\tmp"),
            ("del C:\\tmp", "del /f C:\\tmp"),
            ("erase C:\\tmp", "erase /s C:\\tmp"),
            ("rmdir /a/b", "rmdir -p /a/b"),
            ("Remove-Item /a", "Remove-Item -Recurse /a"),
            ("Remove-Item /a", "Remove-Item -Force /a"),
        ],
    )
    def test_plain_approval_does_not_cover_the_escalated_form(
        self, approved: str, escalated: str
    ) -> None:
        cache = IntentApprovalCache()
        cache.record(approved)
        assert cache.check(approved) is True
        assert cache.check(escalated) is False

    def test_escalated_approval_covers_the_plain_form(self) -> None:
        """De-escalation is fine — the stronger op was already approved."""
        cache = IntentApprovalCache()
        cache.record("Remove-Item -Recurse -Force /a")
        assert cache.check("Remove-Item /a") is True

    def test_separator_bypass_is_closed_for_the_new_spellings(self) -> None:
        """``del A; rd /s /q /`` must not ride in on the approval for ``del A``."""
        cache = IntentApprovalCache()
        cache.record("del /tmp/a")
        assert cache.check("del /tmp/a; rd /s /q /") is False


class TestNonRmDeletionHardBlocks:
    """Issue #1015's headline: these commands must reach the hard blocks."""

    @pytest.mark.parametrize(
        "command",
        [
            "rmdir /s /q /",
            "del /f /q ~/.ssh/id_rsa",
            "Remove-Item -Recurse -Force /",
            "unlink ~/.ssh/id_rsa",
            "rd /s /q /",
            "erase ~/.ssh/id_rsa",
        ],
    )
    def test_destructive_command_is_hard_blocked(self, command: str) -> None:
        assert sensitive_target_in_command(command) is not None

    def test_reading_those_paths_is_still_ordinary_work(self) -> None:
        """The guard: the block keys on a delete, not on the path appearing."""
        assert sensitive_target_in_command("ls /") is None
        assert sensitive_target_in_command("cat ~/.ssh/id_rsa") is None

    def test_a_quoted_delete_is_data_not_a_command(self) -> None:
        """Consistent with ``rm``: ``grep -rn 'del' /etc/passwd`` is not a delete."""
        assert sensitive_target_in_command("grep -rn 'del' /etc/passwd") is None

    def test_a_shell_c_delete_is_a_command(self) -> None:
        """``sh -c "…"`` is executed, so the quoted span is command text.

        The target is followed by ``&&`` rather than ending the string: a
        closing quote at end-of-input is glued onto the last target (``/"``),
        which is a pre-existing artefact of the tail capture — ``sh -c "rm -rf
        /"`` is not blocked on ``main`` either. That gap is not #1015's and is
        left alone here; this asserts the parity the fix is responsible for.
        """
        assert sensitive_target_in_command('sh -c "rd /s /q / && echo done"') is not None
        assert sensitive_target_in_command('sh -c "rm -rf / && echo done"') is not None


class TestWindowsPathTokenisation:
    """The Windows spelling, asserted as it actually tokenises on each host."""

    def test_a_windows_path_keeps_its_grade_on_either_host(self) -> None:
        """``rd /s /q C:\\Windows`` is recursive+force wherever it is parsed."""
        targets = _extract_shell_delete_targets(f"rd /s /q {WINDOWS_PATH}")

        assert (WINDOWS_TARGET, frozenset({"recursive", "force"})) in targets

    def test_the_backslash_survives_only_where_cmd_exe_runs(self) -> None:
        """Pre-existing tokenisation, pinned rather than worked around.

        POSIX ``shlex`` reads ``C:\\tmp`` as an escape and yields ``C:tmp``. A
        POSIX host does not run cmd.exe, so nothing is lost there — but the
        difference is real and belongs in a test rather than in a path written
        without a separator.
        """
        targets = [target for target, _ in _extract_shell_delete_targets(f"rd {WINDOWS_PATH}")]

        assert (WINDOWS_PATH in targets) is (os.name == "nt")
        assert WINDOWS_TARGET in targets


class TestPowerShellBoundParameters:
    """``-Parameter:value`` binds on one token, and both halves have to be read.

    Under PowerShell every deletion verb here is an alias of ``Remove-Item``,
    and a parameter can carry its value with ``:`` as readily as with a space.
    ``_rm_invocation_targets`` skips any token starting with ``-``, so the path
    went missing along with the hard block that depends on it.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "Remove-Item -Path:/etc/passwd -Recurse",
            "Remove-Item -LiteralPath:/ -Recurse -Force",
            "Remove-Item -Path=/etc/passwd",
            "del -Path:/etc/passwd",
        ],
    )
    def test_a_bound_path_still_reaches_the_hard_block(self, command: str) -> None:
        assert sensitive_target_in_command(command) is not None

    def test_a_bound_path_is_extracted_as_the_target(self) -> None:
        assert _extract_shell_delete_targets("Remove-Item -Path:/etc/passwd") == [
            ("/etc/passwd", frozenset())
        ]

    @pytest.mark.parametrize(
        "token",
        ["-Recurse:$true", "-recurse:$true", "-rec:$true", "-Recurse=$true"],
    )
    def test_a_bound_switch_still_grades_the_invocation(self, token: str) -> None:
        """``option.startswith(whole_token)`` matched nothing, so it graded plain."""
        targets = _extract_shell_delete_targets(f"Remove-Item {token} /tmp/x")

        assert ("/tmp/x", frozenset({"recursive"})) in targets

    def test_a_bound_switch_is_not_mistaken_for_a_path(self) -> None:
        """``$true`` is a value, not something to delete."""
        targets = [target for target, _ in _extract_shell_delete_targets("rd -Recurse:$true /a")]

        assert targets == ["/a"]

    def test_a_plain_approval_does_not_cover_a_bound_switch(self) -> None:
        """The escalation this class exists to close, through the cache."""
        cache = IntentApprovalCache()
        cache.record("Remove-Item /tmp/x")

        assert cache.check("Remove-Item /tmp/x") is True
        assert cache.check("Remove-Item -Recurse:$true /tmp/x") is False


class TestCommandPositionAnchor:
    """``del`` and ``rd`` are ordinary words, so position decides.

    A hard block cannot be approved past, so a false positive here does not
    cost a prompt — it makes reading a file impossible. ``rm`` keeps its
    position-free matching: it is specific enough that a bare occurrence is the
    command, and requiring a position would drop ``sudo rm``/``xargs rm``.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "grep -rn del /etc/passwd",
            "echo del /etc/passwd",
            "cat /srv/del/x /etc/passwd",
            "grep -rn rd /etc/passwd",
            "ls /var/erase /etc/passwd",
        ],
    )
    def test_a_verb_that_is_not_the_command_is_not_a_delete(self, command: str) -> None:
        assert sensitive_target_in_command(command) is None
        assert _extract_shell_delete_targets(command) == []

    def test_a_url_path_segment_is_not_a_delete(self) -> None:
        assert _extract_intents("curl https://x.io/rd/asset -o /tmp/a") == []

    @pytest.mark.parametrize(
        "command",
        [
            "del /etc/passwd",
            "sudo del /etc/passwd",
            "sudo -u root del /etc/passwd",
            "doas del /etc/passwd",
            "env FOO=1 del /etc/passwd",
            "FOO=1 del /etc/passwd",
            "time rd /s /q /etc",
            "nohup del /etc/passwd",
            "command del /etc/passwd",
            "xargs del /etc/passwd",
            "busybox rm /etc/passwd",
            "ls; del /etc/passwd",
            "ls && del /etc/passwd",
            "ls | xargs del /etc/passwd",
            "/usr/bin/rmdir /etc/foo",
        ],
    )
    def test_a_verb_in_command_position_is_still_a_delete(self, command: str) -> None:
        assert sensitive_target_in_command(command) is not None

    def test_rm_keeps_its_position_free_matching(self) -> None:
        """Passes either way by design: the anchor is for the short verbs only."""
        assert sensitive_target_in_command("sudo rm -rf /etc") is not None
        assert sensitive_target_in_command("xargs rm -rf /etc") is not None
        assert sensitive_target_in_command("timeout 5 rm -rf /etc") is not None


class TestDeleteVerbsInsideFlags:
    """A verb spelled inside a flag is not an invocation.

    ``(?<![\\w.])`` admitted a leading ``-``, so ``-rd`` and ``--rm`` matched.
    That swallowed the real command's flags and regraded it.
    """

    def test_a_flag_spelled_like_a_verb_does_not_swallow_the_real_command(self) -> None:
        """``xargs -rd '\\n' rm -rf /etc`` is a recursive force delete of /etc.

        The target is matched with :func:`_names_etc` rather than compared
        literally: Windows resolves a drive-relative ``/etc`` against the
        working drive, so the runner sees ``D:\\etc``.
        """
        intents = _extract_intents("xargs -rd '\\n' rm -rf /etc")

        assert [kind for kind, _ in intents] == ["delete:recursive+force"]
        assert _names_etc([target for _, target in intents]), intents

    def test_a_flag_spelled_like_a_verb_invents_no_intent(self) -> None:
        assert _extract_intents("docker run --rm -it ubuntu bash") == []
        assert _extract_shell_delete_targets("tar --delete -f a.tar x") == []

    @pytest.mark.parametrize(
        ("verb", "flag", "capability"),
        [
            ("rd", "-Recurse", "recursive"),
            ("del", "-Force", "force"),
            ("erase", "-Recurse", "recursive"),
            ("rmdir", "-Force", "force"),
        ],
    )
    def test_a_powershell_alias_grades_remove_item_parameters(
        self, verb: str, flag: str, capability: str
    ) -> None:
        """PowerShell resolves these names to ``Remove-Item``, so they take its flags.

        Without the shared table the parameter graded nothing and a plain
        approval covered the escalated form — the same hole as ``-Recurse:$true``,
        one spelling over.
        """
        assert _extract_shell_delete_targets(f"{verb} {flag} /a") == [
            ("/a", frozenset({capability}))
        ]

    def test_a_posix_flag_on_the_same_verb_still_grades(self) -> None:
        """The fall-through: a PowerShell table must not swallow ``rmdir -p``."""
        assert _extract_shell_delete_targets("rmdir -p /a/b") == [("/a/b", frozenset({"parents"}))]
        assert _extract_shell_delete_targets("rmdir --parents /a") == [
            ("/a", frozenset({"parents"}))
        ]


class TestCompoundCommandOpeners:
    """A verb after a group opener or a reserved word is still the command.

    The anchor only knew separators and wrappers, so ``{ del …; }``,
    ``do unlink …`` and ``$(rmdir …)`` yielded nothing while the same ``rm``
    was hard-blocked — the short verbs lost a block ``rm`` keeps.
    """

    @pytest.mark.parametrize(
        ("command", "rm_command"),
        [
            ("{ del /etc/passwd; }", "{ rm /etc/passwd; }"),
            (
                "for f in a b; do unlink ~/.ssh/id_rsa; done",
                "for f in a b; do rm ~/.ssh/id_rsa; done",
            ),
            ("if true; then del /etc/passwd; fi", "if true; then rm /etc/passwd; fi"),
            ("if false; then :; else rd /s /q /etc; fi", "if false; then :; else rm -rf /etc; fi"),
            ("while true; do erase /etc/passwd; done", "while true; do rm /etc/passwd; done"),
            ("`rmdir /etc/foo`", "`rm /etc/foo`"),
            ("$(rmdir /etc/foo)", "$(rm /etc/foo)"),
            ("echo $(del /etc/passwd)", "echo $(rm /etc/passwd)"),
            ("(unlink ~/.ssh/id_rsa)", "(rm ~/.ssh/id_rsa)"),
            ("! del /etc/passwd", "! rm /etc/passwd"),
            ("then sudo del /etc/passwd", "then sudo rm /etc/passwd"),
        ],
    )
    def test_a_verb_after_an_opener_is_hard_blocked_like_rm(
        self, command: str, rm_command: str
    ) -> None:
        assert sensitive_target_in_command(rm_command) is not None
        assert sensitive_target_in_command(command) is not None

    @pytest.mark.parametrize(
        ("command", "rm_command"),
        [
            ("`rmdir /etc/foo`", "`rm /etc/foo`"),
            ("$(rmdir /etc/foo)", "$(rm /etc/foo)"),
            ("(unlink ~/.ssh/id_rsa)", "(rm ~/.ssh/id_rsa)"),
            ("{ unlink ~/.ssh/id_rsa; }", "{ rm ~/.ssh/id_rsa; }"),
        ],
    )
    def test_a_verb_after_an_opener_extracts_the_target_rm_does(
        self, command: str, rm_command: str
    ) -> None:
        """Including the trailing-paren artefact, which is ``rm``'s on ``main`` too."""
        assert _extract_shell_delete_targets(command) == _extract_shell_delete_targets(rm_command)
        assert _extract_shell_delete_targets(command) != []

    def test_else_keeps_the_invocation_grade(self) -> None:
        assert _extract_shell_delete_targets("if a; then :; else rd /s /q /; fi") == [
            ("/s", frozenset({"recursive", "force"})),
            ("/q", frozenset({"recursive", "force"})),
            ("/", frozenset({"recursive", "force"})),
        ]

    @pytest.mark.parametrize(
        "command",
        [
            "echo then del /etc/passwd",
            "grep -rn do del /etc/passwd",
            "echo `pwd` del /etc/passwd",
            "echo $(pwd) del /etc/passwd",
            "echo ${del} /etc/passwd",
            "cat /srv/{a,del}/x /etc/passwd",
        ],
    )
    def test_an_opener_or_keyword_out_of_position_is_still_text(self, command: str) -> None:
        """Passes either way by design: the openers must not widen the anchor."""
        assert sensitive_target_in_command(command) is None
        assert _extract_shell_delete_targets(command) == []
