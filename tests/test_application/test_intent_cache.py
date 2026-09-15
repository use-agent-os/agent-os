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


class TestSessionScopedGrants:
    """An approval is bounded by the session it was granted in.

    ``ApprovalQueue.resolve`` files the elevated mode carried by an approval
    under its ``sessionKey`` and the intent it grants under no key at all, so a
    prompt answered in one session used to answer the next session's prompt
    too — silently, since the short-circuit in ``shell._check_exec_approval``
    returns before the queue is ever asked.
    """

    def _target(self, path: str) -> str:
        from pathlib import Path

        return str(Path(path).resolve(strict=False))

    def test_a_grant_does_not_reach_another_session(self) -> None:
        cache = IntentApprovalCache()
        cache.record_always("rm -rf /tmp/agentos-reports", session_key="web:alice")

        assert cache.check("rm -rf /tmp/agentos-reports", session_key="web:alice") is True
        assert cache.check("rm -rf /tmp/agentos-reports", session_key="web:bob") is False

    def test_a_paraphrase_crosses_spellings_but_not_sessions(self) -> None:
        """The whole point of the cache still works — inside one session."""
        cache = IntentApprovalCache()
        cache.record_always("rm -rf /tmp/agentos-build", session_key="agent:alpha:main")

        assert (
            cache.check('shutil.rmtree("/tmp/agentos-build")', session_key="agent:alpha:main")
            is True
        )
        assert (
            cache.check('shutil.rmtree("/tmp/agentos-build")', session_key="agent:beta:main")
            is False
        )

    def test_the_session_is_part_of_the_stored_key(self) -> None:
        cache = IntentApprovalCache()
        cache.record("rm /tmp/agentos-one", session_key="web:alice")

        assert cache._entries.keys() == [  # noqa: SLF001 - state assertion
            ("web:alice", "delete", self._target("/tmp/agentos-one"))
        ]

    def test_two_sessions_keep_independent_grades_for_one_target(self) -> None:
        """Neither entry may overwrite the other, nor answer for it."""
        cache = IntentApprovalCache()
        cache.record("rm /tmp/agentos-shared", session_key="web:alice")
        cache.record_always("rm -rf /tmp/agentos-shared", session_key="web:bob")

        assert cache.check("rm -rf /tmp/agentos-shared", session_key="web:alice") is False
        assert cache.check("rm /tmp/agentos-shared", session_key="web:alice") is True
        assert cache.check("rm -rf /tmp/agentos-shared", session_key="web:bob") is True
        assert len(cache._entries) == 2  # noqa: SLF001 - state assertion

    def test_a_context_less_grant_does_not_cover_a_named_session(self) -> None:
        """A tool call with no context files as ``""`` on both sides.

        The direction matters: an unattributed grant answering a real session's
        prompt would reopen the bypass from the other end.
        """
        cache = IntentApprovalCache()
        cache.record_always("rm /tmp/agentos-orphan")

        assert cache.check("rm /tmp/agentos-orphan") is True
        assert cache.check("rm /tmp/agentos-orphan", session_key="web:alice") is False

    def test_blank_session_keys_normalize_to_one_scope(self) -> None:
        cache = IntentApprovalCache()
        cache.record_always("rm /tmp/agentos-blank", session_key="  ")

        assert cache.check("rm /tmp/agentos-blank", session_key="") is True

    def test_a_new_turn_clears_only_its_own_sessions_once_grants(self) -> None:
        cache = IntentApprovalCache()
        cache.record("rm /tmp/agentos-a", session_key="web:alice")
        cache.record("rm /tmp/agentos-b", session_key="web:bob")

        cache.clear_scope("once", session_key="web:bob")

        assert cache.check("rm /tmp/agentos-a", session_key="web:alice") is True
        assert cache.check("rm /tmp/agentos-b", session_key="web:bob") is False

    def test_clear_scope_without_a_session_still_sweeps_every_session(self) -> None:
        cache = IntentApprovalCache()
        cache.record("rm /tmp/agentos-a", session_key="web:alice")
        cache.record("rm /tmp/agentos-b", session_key="web:bob")

        cache.clear_scope("once")

        assert cache.check("rm /tmp/agentos-a", session_key="web:alice") is False
        assert cache.check("rm /tmp/agentos-b", session_key="web:bob") is False

    def test_a_sibling_sessions_turn_cannot_drop_an_always_grant(self) -> None:
        cache = IntentApprovalCache()
        cache.record_always("rm /tmp/agentos-keep", session_key="web:alice")

        cache.clear_scope("once", session_key="web:bob")
        cache.clear_scope("once", session_key="web:alice")

        assert cache.check("rm /tmp/agentos-keep", session_key="web:alice") is True

    def test_forget_is_an_operator_command_and_clears_every_session(self) -> None:
        """``/approvals forget <path>`` has no session of its own."""
        cache = IntentApprovalCache()
        cache.record_always("rm -rf /tmp/agentos-gone", session_key="web:alice")
        cache.record_always("rm -rf /tmp/agentos-gone", session_key="web:bob")

        cache.forget("rm /tmp/agentos-gone")

        assert cache.check("rm -rf /tmp/agentos-gone", session_key="web:alice") is False
        assert cache.check("rm -rf /tmp/agentos-gone", session_key="web:bob") is False
        assert len(cache._entries) == 0  # noqa: SLF001 - state assertion

    def test_forget_can_be_narrowed_to_one_session(self) -> None:
        cache = IntentApprovalCache()
        cache.record_always("rm -rf /tmp/agentos-narrow", session_key="web:alice")
        cache.record_always("rm -rf /tmp/agentos-narrow", session_key="web:bob")

        cache.forget("rm /tmp/agentos-narrow", session_key="web:alice")

        assert cache.check("rm -rf /tmp/agentos-narrow", session_key="web:alice") is False
        assert cache.check("rm -rf /tmp/agentos-narrow", session_key="web:bob") is True

    def test_forget_still_clears_every_grade_of_the_target(self) -> None:
        cache = IntentApprovalCache()
        cache.record_always("rm -rf /tmp/agentos-graded", session_key="web:alice")
        cache.record_always("os.removedirs('/tmp/agentos-graded')", session_key="web:alice")

        cache.forget("rm /tmp/agentos-graded")

        assert len(cache._entries) == 0  # noqa: SLF001 - state assertion

    def test_a_finished_session_takes_its_grants_with_it(self) -> None:
        """``drop_session_state`` is the deterministic half of the contract.

        Without ``session_of`` the registry could not answer the question, so a
        year-long ``always`` grant outlived the session that made it.
        """
        from agentos.util.bounded_registry import drop_session_state

        cache = IntentApprovalCache()
        cache.record_always("rm -rf /tmp/agentos-ends", session_key="web:alice")
        cache.record_always("rm -rf /tmp/agentos-ends", session_key="web:bob")

        assert drop_session_state("web:alice") >= 1

        assert cache.check("rm -rf /tmp/agentos-ends", session_key="web:alice") is False
        assert cache.check("rm -rf /tmp/agentos-ends", session_key="web:bob") is True

    def test_multi_target_commands_are_scoped_target_by_target(self) -> None:
        cache = IntentApprovalCache()
        cache.record("rm /tmp/agentos-x /tmp/agentos-y", session_key="web:alice")

        assert cache.check("rm /tmp/agentos-x /tmp/agentos-y", session_key="web:alice") is True
        assert cache.check("rm /tmp/agentos-x /tmp/agentos-y", session_key="web:bob") is False
        cache.record("rm /tmp/agentos-x", session_key="web:bob")
        assert cache.check("rm /tmp/agentos-x /tmp/agentos-y", session_key="web:bob") is False

    def test_expiry_is_still_honoured_within_a_session(self) -> None:
        cache = IntentApprovalCache()
        cache.record("rm /tmp/agentos-expired", ttl=-1.0, session_key="web:alice")

        assert cache.check("rm /tmp/agentos-expired", session_key="web:alice") is False
