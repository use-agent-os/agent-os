"""Issue #2260: git-diff exited 128 in a repository with no first commit.

``_diff_for_mode`` spelled ``HEAD`` unconditionally. Before the first commit
lands ``HEAD`` names nothing, so git exits 128 with ``ambiguous argument
'HEAD'`` and the skill fails on the one repository state where a diff is most
obviously wanted — everything staged, nothing committed.

Three of the four modes were affected. ``staged_files`` never spelled ``HEAD``
and already worked, which is the shape of the fix: where the revision cannot be
resolved, ``--cached`` carries the comparison on its own.

Whether ``HEAD`` exists is now asked directly, with the same
``rev-parse --verify --quiet HEAD`` probe ``tools/builtin/git.py::_diff_revision``
already uses, rather than inferred from a diff that failed. Those are not the
same question: a diff can fail for reasons unrelated to ``HEAD``, and retrying
*those* without the revision computes a different comparison and reports it as
success. A repository whose object store is damaged is the easy example —
``rev-parse`` succeeds, ``git diff HEAD`` exits 128, and plain ``git diff``
exits 0 with a worktree-versus-index diff that is not what was asked for.
"""

from __future__ import annotations

import importlib.util
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "src" / "agentos" / "skills" / "bundled" / "git-diff" / "scripts" / "git_diff.py"

MODES = ("cached_fallback_worktree", "cached", "worktree", "staged_files")


def _load():
    spec = importlib.util.spec_from_file_location("git_diff_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["git_diff_script"] = module
    spec.loader.exec_module(module)
    return module


script = _load()


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=str(cwd),
        capture_output=True,
        check=False,
    )


@pytest.fixture
def unborn(tmp_path: Path) -> Path:
    """A repository with a staged file and no commit at all."""
    git(tmp_path, "init", "-q", ".")
    (tmp_path / "a.txt").write_bytes(b"staged before any commit\n")
    git(tmp_path, "add", "a.txt")
    return tmp_path


@pytest.fixture
def committed(tmp_path: Path) -> Path:
    """An ordinary repository: one commit, one staged edit, one unstaged edit."""
    git(tmp_path, "init", "-q", ".")
    (tmp_path / "a.txt").write_bytes(b"one\n")
    git(tmp_path, "add", "a.txt")
    git(tmp_path, "commit", "-qm", "first")
    (tmp_path / "a.txt").write_bytes(b"one\ntwo\n")
    git(tmp_path, "add", "a.txt")
    (tmp_path / "b.txt").write_bytes(b"unstaged\n")
    return tmp_path


# ── the reported bug ────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", MODES)
def test_no_mode_fails_on_an_unborn_branch(unborn: Path, mode: str) -> None:
    """The issue's reproduction, across every mode rather than the one it names."""
    rc, out, err = script._diff_for_mode(mode, unborn)

    assert rc == 0, f"{mode} exited {rc}: {err.decode('utf-8', 'replace')[:120]}"
    assert b"ambiguous argument" not in err


@pytest.mark.parametrize("mode", ["cached_fallback_worktree", "cached", "staged_files"])
def test_the_staged_file_is_actually_reported(unborn: Path, mode: str) -> None:
    """Exiting 0 is not enough — the staged content is the whole change set in
    a repository with no commit, so it has to appear."""
    _rc, out, _err = script._diff_for_mode(mode, unborn)

    assert b"a.txt" in out


def test_worktree_mode_on_an_unborn_branch_reports_no_unstaged_change(unborn: Path) -> None:
    """With no commit and nothing unstaged there is genuinely nothing to show,
    and that must read as an empty diff rather than an error."""
    rc, out, _err = script._diff_for_mode("worktree", unborn)

    assert rc == 0
    assert out.strip() == b""


def test_an_unstaged_edit_on_an_unborn_branch_is_reported(unborn: Path) -> None:
    (unborn / "a.txt").write_bytes(b"staged before any commit\nthen edited\n")

    rc, out, _err = script._diff_for_mode("worktree", unborn)

    assert rc == 0
    assert b"then edited" in out


def test_an_empty_repository_with_nothing_staged_is_not_an_error(tmp_path: Path) -> None:
    """``git init`` and nothing else — the first thing a new project is."""
    git(tmp_path, "init", "-q", ".")

    for mode in MODES:
        rc, out, _err = script._diff_for_mode(mode, tmp_path)
        assert rc == 0, mode
        assert out.strip() == b"", mode


# ── the probe answers the right question ────────────────────────────────────


def test_has_head_is_false_before_the_first_commit(unborn: Path) -> None:
    assert script._has_head(unborn) is False


def test_has_head_is_true_after_the_first_commit(committed: Path) -> None:
    assert script._has_head(committed) is True


def test_a_damaged_object_store_is_reported_not_silently_retried(committed: Path) -> None:
    """The reason the decision is a probe rather than "did the diff fail?".

    Deleting the tree object ``HEAD`` points at leaves a repository where
    ``rev-parse`` still resolves ``HEAD`` (rc 0), ``git diff HEAD`` cannot read
    the tree (rc 128), and plain ``git diff`` succeeds (rc 0) with a
    worktree-versus-index diff. Retrying on any failure would hand that back as
    a successful answer to a question nobody asked, hiding a corrupt repo.
    """
    tree = git(committed, "rev-parse", "HEAD^{tree}").stdout.decode().strip()
    obj = committed / ".git" / "objects" / tree[:2] / tree[2:]
    if not obj.is_file():
        pytest.skip("object is packed, not loose; cannot damage it portably")
    # Loose objects are written read-only, which Windows enforces on unlink.
    obj.chmod(stat.S_IWRITE | stat.S_IREAD)
    obj.unlink()

    assert script._has_head(committed) is True, "HEAD still resolves; only the tree is gone"

    rc, _out, err = script._diff_for_mode("worktree", committed)

    assert rc != 0, "a damaged object store must not be reported as a clean diff"
    assert err.strip()


# ── ordinary repositories are unchanged ─────────────────────────────────────


def test_cached_reports_staged_work(committed: Path) -> None:
    rc, out, _err = script._diff_for_mode("cached", committed)

    assert rc == 0
    assert b"two" in out


def test_worktree_reports_staged_and_unstaged_together(committed: Path) -> None:
    """``git diff HEAD`` is the spelling that shows both halves, which is what
    the revision is there for — dropping it unconditionally would lose that."""
    rc, out, _err = script._diff_for_mode("worktree", committed)

    assert rc == 0
    assert b"two" in out


def test_the_revision_is_still_used_when_head_exists(committed: Path) -> None:
    """Guards against "fix it by always dropping HEAD": with a commit present,
    the argv must still name it."""
    assert script._diff_argv(cached=False, head=True) == ["diff", "HEAD"]
    assert script._diff_argv(cached=True, head=True) == ["diff", "--cached", "HEAD"]
    assert script._diff_argv(cached=False, head=False) == ["diff"]
    assert script._diff_argv(cached=True, head=False) == ["diff", "--cached"]


def test_cached_fallback_prefers_staged_then_falls_back(committed: Path) -> None:
    rc, out, _err = script._diff_for_mode("cached_fallback_worktree", committed)

    assert rc == 0
    assert b"two" in out, "staged work should win when there is any"


def test_cached_fallback_shows_unstaged_when_nothing_is_staged(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q", ".")
    (tmp_path / "a.txt").write_bytes(b"one\n")
    git(tmp_path, "add", "a.txt")
    git(tmp_path, "commit", "-qm", "first")
    (tmp_path / "a.txt").write_bytes(b"one\nunstaged\n")

    rc, out, _err = script._diff_for_mode("cached_fallback_worktree", tmp_path)

    assert rc == 0
    assert b"unstaged" in out


def test_staged_files_lists_names_only(committed: Path) -> None:
    rc, out, _err = script._diff_for_mode("staged_files", committed)

    assert rc == 0
    assert out.strip() == b"a.txt"


def test_an_unsupported_mode_still_raises(committed: Path) -> None:
    with pytest.raises(ValueError, match="unsupported mode"):
        script._diff_for_mode("not-a-mode", committed)


def test_a_directory_that_is_not_a_repository_fails(tmp_path: Path) -> None:
    """Not a git repository at all is a real error and must stay one."""
    rc, _out, err = script._diff_for_mode("cached", tmp_path)

    assert rc != 0
    assert err.strip()
