"""Issue #2191: an approved destructive intent was cached process-wide.

``IntentApprovalCache`` keyed a grant by ``(kind, target)``, so the answer one
operator gave in one session answered every other session's prompt too. That is
worse than it sounds: ``shell._check_exec_approval`` consults the cache and
returns *before* ``queue.request(...)``, so the second session never produced an
approval row and no prompt appeared on any surface -- the command simply ran,
leaving one informational log line behind.

Three more consequences of the missing dimension, all covered here:

* ``clear_scope("once")`` swept the whole process, so a message in one session
  disarmed another session's in-flight grants;
* no ``session_of`` meant ``drop_session_state`` could not reap a finished
  session's grants, and ``record_always`` carries a 365-day TTL;
* targets were normalised against the *gateway's* cwd, so ``rm -rf build`` in
  one workspace matched ``rm -rf build`` in another.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos.application.intent_cache import IntentApprovalCache
from agentos.util.bounded_registry import drop_session_state

ALICE = "web:alice"
BOB = "web:bob"
TARGET = "/srv/data/reports"


@pytest.fixture()
def cache() -> IntentApprovalCache:
    return IntentApprovalCache()


def _abs(path: str) -> str:
    return str(Path(path).expanduser().resolve(strict=False))


# ── a grant answers only its own session ────────────────────────────────────


def test_another_session_does_not_inherit_the_grant(cache: IntentApprovalCache) -> None:
    """The reported case: session B runs session A's approved rm with no prompt."""
    cache.record_always(f"rm -rf {TARGET}", session_key=ALICE)

    assert cache.check(f"rm -rf {TARGET}", session_key=ALICE) is True
    assert cache.check(f"rm -rf {TARGET}", session_key=BOB) is False


def test_a_paraphrase_in_another_session_is_not_covered(cache: IntentApprovalCache) -> None:
    """Paraphrase matching is the feature; crossing sessions is not."""
    cache.record_always(f"rm -rf {TARGET}", session_key=ALICE)

    assert cache.check(f'shutil.rmtree("{TARGET}")', session_key=BOB) is False
    assert cache.check(f'shutil.rmtree("{TARGET}")', session_key=ALICE) is True


def test_a_sessionless_check_does_not_meet_a_session_grant(
    cache: IntentApprovalCache,
) -> None:
    """A caller that forgot to pass a session must not be handed someone's
    grant. The two file under different keys and the check re-prompts."""
    cache.record_always(f"rm -rf {TARGET}", session_key=ALICE)

    assert cache.check(f"rm -rf {TARGET}") is False


def test_a_sessionless_grant_does_not_answer_a_real_session(
    cache: IntentApprovalCache,
) -> None:
    """The same rule in the other direction, which is the one that matters for
    safety: an unattributed grant is not an answer to a real prompt."""
    cache.record_always(f"rm -rf {TARGET}")

    assert cache.check(f"rm -rf {TARGET}", session_key=BOB) is False


def test_the_session_is_part_of_the_key(cache: IntentApprovalCache) -> None:
    cache.record_always(f"rm -rf {TARGET}", session_key=ALICE)

    assert [key[0] for key in cache._entries.keys()] == [ALICE]


def test_two_sessions_hold_independent_grants(cache: IntentApprovalCache) -> None:
    cache.record_always(f"rm -rf {TARGET}", session_key=ALICE)
    cache.record_always(f"rm -rf {TARGET}", session_key=BOB)

    cache.forget(f"rm {TARGET}", session_key=ALICE)

    assert cache.check(f"rm -rf {TARGET}", session_key=ALICE) is False
    assert cache.check(f"rm -rf {TARGET}", session_key=BOB) is True


def test_the_destructiveness_grading_still_holds_within_a_session(
    cache: IntentApprovalCache,
) -> None:
    """Unchanged behaviour, pinned: a plain delete never covers a recursive one.
    The session dimension must not weaken the grade dimension."""
    cache.record_always(f"rm {TARGET}", session_key=ALICE)

    assert cache.check(f"rm {TARGET}", session_key=ALICE) is True
    assert cache.check(f"rm -rf {TARGET}", session_key=ALICE) is False


# ── clear_scope no longer sweeps the process ────────────────────────────────


def test_clearing_one_session_leaves_another_armed(cache: IntentApprovalCache) -> None:
    """A message in session B used to disarm session A mid-turn."""
    cache.record(f"rm -rf {TARGET}", session_key=ALICE, scope="once")
    cache.record(f"rm -rf {TARGET}", session_key=BOB, scope="once")

    cache.clear_scope("once", session_key=BOB)

    assert cache.check(f"rm -rf {TARGET}", session_key=ALICE) is True
    assert cache.check(f"rm -rf {TARGET}", session_key=BOB) is False


def test_clearing_once_leaves_always_alone(cache: IntentApprovalCache) -> None:
    """Scope semantics survive the new session filter."""
    cache.record_always(f"rm -rf {TARGET}", session_key=ALICE)

    cache.clear_scope("once", session_key=ALICE)

    assert cache.check(f"rm -rf {TARGET}", session_key=ALICE) is True


def test_clear_scope_without_a_session_still_sweeps_everything(
    cache: IntentApprovalCache,
) -> None:
    """Shutdown wants the whole process cleared, and still gets it."""
    cache.record(f"rm -rf {TARGET}", session_key=ALICE, scope="once")
    cache.record(f"rm -rf {TARGET}", session_key=BOB, scope="once")

    cache.clear_scope("once")

    assert cache.check(f"rm -rf {TARGET}", session_key=ALICE) is False
    assert cache.check(f"rm -rf {TARGET}", session_key=BOB) is False


# ── grants die with their session ───────────────────────────────────────────


def test_dropping_a_session_reaps_its_grants(cache: IntentApprovalCache) -> None:
    """`record_always` carries a 365-day TTL, so without `session_of` a grant
    outlived the session that made it for the life of the process."""
    cache.record_always(f"rm -rf {TARGET}", session_key=ALICE)
    cache.record_always(f"rm -rf {TARGET}", session_key=BOB)

    drop_session_state(ALICE)

    assert cache.check(f"rm -rf {TARGET}", session_key=ALICE) is False
    assert cache.check(f"rm -rf {TARGET}", session_key=BOB) is True


def test_dropping_a_session_reports_what_it_reaped(cache: IntentApprovalCache) -> None:
    cache.record_always(f"rm -rf {TARGET}", session_key=ALICE)

    assert drop_session_state(ALICE) >= 1


def test_a_sessionless_entry_is_not_reaped_by_any_session_drop(
    cache: IntentApprovalCache,
) -> None:
    """``session_of`` returns None for the empty session, so the sweep skips it
    rather than attributing it to whichever session is ending."""
    cache.record_always(f"rm -rf {TARGET}")

    drop_session_state(ALICE)

    assert cache.check(f"rm -rf {TARGET}") is True


# ── targets resolve against the session's workspace ─────────────────────────


def test_the_same_relative_target_in_two_workspaces_is_two_intents(
    cache: IntentApprovalCache, tmp_path: Path
) -> None:
    """`rm -rf build` means a different directory per workspace. Normalised
    against the gateway's cwd they collapsed onto one key."""
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"

    cache.record_always("rm -rf build", session_key=ALICE, base_dir=workspace_a)

    assert cache.check("rm -rf build", session_key=ALICE, base_dir=workspace_a) is True
    assert cache.check("rm -rf build", session_key=ALICE, base_dir=workspace_b) is False


def test_a_relative_grant_matches_its_own_absolute_spelling(
    cache: IntentApprovalCache,
) -> None:
    """Approving `rm -rf build` and then running the absolute path is the same
    intent, and only resolves that way when the workspace is supplied.

    Written with forward slashes on purpose: the command goes through ``shlex``,
    which treats a backslash as an escape, so a native Windows path in a command
    string is a property of the tokeniser rather than of this cache.
    """
    workspace = "/srv/app"
    cache.record_always("rm -rf build", session_key=ALICE, base_dir=workspace)

    assert cache.check("rm -rf /srv/app/build", session_key=ALICE, base_dir=workspace) is True


def test_an_absolute_target_ignores_the_workspace(
    cache: IntentApprovalCache, tmp_path: Path
) -> None:
    """A base dir must not be joined onto a path that is already absolute.

    The target is built from ``tmp_path`` rather than written as ``/srv/...``:
    on Windows a rooted path with no drive letter is *drive-relative*, so
    ``Path("/srv").is_absolute()`` is False there and joining the base dir onto
    it is correct behaviour, not the bug this pins. ``as_posix`` keeps forward
    slashes, which makes the path absolute on Windows and also keeps it clear
    of ``shlex``, whose escape handling eats a native backslash path inside a
    command string.
    """
    absolute_target = (tmp_path / "data").as_posix()
    other_workspace = tmp_path / "somewhere-else"

    cache.record_always(f"rm -rf {absolute_target}", session_key=ALICE, base_dir=other_workspace)

    recorded_target = next(iter(cache._entries))[2]
    assert recorded_target == _abs(absolute_target)
    assert "somewhere-else" not in recorded_target


# ── forget is deliberately broader than check ───────────────────────────────


def test_forget_without_a_session_clears_every_session(
    cache: IntentApprovalCache,
) -> None:
    """`/forget` and `exec.approval.forget` carry no session. Scoping them to
    the empty session would have quietly stopped clearing anything real --
    and forgetting too much only costs a prompt, while checking too much runs
    an unapproved command."""
    cache.record_always(f"rm -rf {TARGET}", session_key=ALICE)
    cache.record_always(f"rm -rf {TARGET}", session_key=BOB)

    cache.forget(f"rm {TARGET}")

    assert cache.check(f"rm -rf {TARGET}", session_key=ALICE) is False
    assert cache.check(f"rm -rf {TARGET}", session_key=BOB) is False


def test_forget_still_clears_the_escalated_grade(cache: IntentApprovalCache) -> None:
    """Unchanged behaviour: `/forget <path>` builds a plain `rm <path>` and has
    to clear the recursive entry too."""
    cache.record_always(f"rm -rf {TARGET}", session_key=ALICE)

    cache.forget(f"rm {TARGET}", session_key=ALICE)

    assert cache.check(f"rm -rf {TARGET}", session_key=ALICE) is False


def test_forget_leaves_an_unrelated_target_alone(cache: IntentApprovalCache) -> None:
    cache.record_always(f"rm -rf {TARGET}", session_key=ALICE)
    cache.record_always("rm -rf /srv/other", session_key=ALICE)

    cache.forget(f"rm {TARGET}")

    assert cache.check("rm -rf /srv/other", session_key=ALICE) is True


# ── multi-target commands keep their all-or-nothing rule ────────────────────


def test_every_target_must_be_approved_in_this_session(
    cache: IntentApprovalCache,
) -> None:
    cache.record_always("rm -rf /srv/a", session_key=ALICE)

    assert cache.check("rm -rf /srv/a /srv/b", session_key=ALICE) is False


def test_a_partial_grant_from_another_session_does_not_complete_the_set(
    cache: IntentApprovalCache,
) -> None:
    """The multi-target rule and the session rule have to hold together: B's
    approval of one path must not complete A's two-path command."""
    cache.record_always("rm -rf /srv/a", session_key=ALICE)
    cache.record_always("rm -rf /srv/b", session_key=BOB)

    assert cache.check("rm -rf /srv/a /srv/b", session_key=ALICE) is False
