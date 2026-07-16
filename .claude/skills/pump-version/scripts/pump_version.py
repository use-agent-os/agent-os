#!/usr/bin/env python3
"""Bump the AgentOS release version atomically across every file that
tests/test_release_consistency.py and tests/test_install_scripts.py check.

Usage:
    python pump_version.py 2026.7.16 --notes "Short description of the release"
    python pump_version.py v2026.7.16 --date 2026-07-16 --notes "..."
    python pump_version.py 2026.7.14 --notes "Rollback" --allow-downgrade

Version scheme is CalVer YYYY.M.D, optionally with a .postN suffix for a
same-day re-release (e.g. 2026.7.14.post1). No segment may have a leading
zero (2026.07.15 is invalid) -- PEP 440 drops leading zeros when it
normalizes wheel filenames, so a padded version makes the built wheel's
name disagree with what CI expects and wheelhouse-release.yml fails.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from datetime import date as date_cls
from pathlib import Path

VERSION_RE = re.compile(
    r"^(?P<year>0|[1-9]\d*)\.(?P<month>0|[1-9]\d*)\.(?P<day>0|[1-9]\d*)"
    r"(?:\.post(?P<post>0|[1-9]\d*))?$"
)


class PumpError(Exception):
    pass


def find_repo_root(start: Path) -> Path:
    """Walk up from `start` looking for the AgentOS pyproject.toml.

    Doesn't assume a fixed nesting depth so the skill still works if it's
    copied to a global skills directory or the repo is relocated.
    """
    for candidate in [start, *start.parents]:
        pyproject = candidate / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            continue
        if data.get("project", {}).get("name") == "use-agent-os":
            return candidate
    raise PumpError(
        f"could not find the AgentOS repo root (pyproject.toml with "
        f"project.name == 'use-agent-os') walking up from {start}"
    )


def parse_version(raw: str) -> tuple[str, tuple[int, int, int, int]]:
    """Return (normalized_string, comparable_tuple) or raise PumpError."""
    normalized = raw[1:] if raw.startswith(("v", "V")) else raw
    match = VERSION_RE.match(normalized)
    if not match:
        raise PumpError(
            f"'{raw}' is not a valid CalVer version (expected YYYY.M.D or "
            f"YYYY.M.D.postN, no leading zeros in any segment, e.g. "
            f"2026.7.16 or 2026.7.16.post1) -- got normalized form "
            f"'{normalized}'"
        )
    post = int(match.group("post")) if match.group("post") else 0
    comparable = (
        int(match.group("year")),
        int(match.group("month")),
        int(match.group("day")),
        post,
    )
    return normalized, comparable


def current_version(repo_root: Path) -> str:
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["project"]["version"]


def check_tag_absent(repo_root: Path, tag: str) -> None:
    local = subprocess.run(
        ["git", "tag", "-l", tag], cwd=repo_root, capture_output=True, text=True, check=True
    )
    if local.stdout.strip():
        raise PumpError(
            f"git tag '{tag}' already exists locally. Re-tagging an "
            f"existing version is what caused the 2026-07-14 rollback "
            f"incident -- delete the tag deliberately first if this is "
            f"really what you want, then re-run."
        )
    remote = subprocess.run(
        ["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if remote.returncode == 0 and remote.stdout.strip():
        raise PumpError(
            f"git tag '{tag}' already exists on origin. Re-tagging an "
            f"existing version is what caused the 2026-07-14 rollback "
            f"incident -- delete the remote tag deliberately first if "
            f"this is really what you want, then re-run."
        )


def replace_exact(path: Path, old: str, new: str, expected_count: int) -> None:
    text = path.read_text(encoding="utf-8")
    actual_count = text.count(old)
    if actual_count != expected_count:
        raise PumpError(
            f"{path}: expected {expected_count} occurrence(s) of "
            f"'{old}' but found {actual_count}. The file's format may "
            f"have changed since this script was written -- stop and "
            f"check the diff by hand instead of trusting a partial "
            f"automated edit."
        )
    path.write_text(text.replace(old, new), encoding="utf-8")


def bump_changelog(repo_root: Path, new_version: str, release_date: str) -> None:
    path = repo_root / "CHANGELOG.md"
    text = path.read_text(encoding="utf-8")
    new_header = f"## [{new_version}]"
    if new_header in text:
        raise PumpError(
            f"{path} already has a '{new_header}' section -- refusing to "
            f"insert a duplicate. Remove it first if you really want to "
            f"redo this bump."
        )
    anchor = "## [Unreleased]\n\n"
    if anchor not in text:
        raise PumpError(
            f"{path}: could not find the expected '## [Unreleased]' "
            f"section header followed by a blank line -- check the file "
            f"format by hand."
        )
    replacement = f"## [Unreleased]\n\n## [{new_version}] - {release_date}\n\n"
    path.write_text(text.replace(anchor, replacement, 1), encoding="utf-8")


def bump_releases_md(repo_root: Path, new_version: str, new_tag: str, release_date: str, notes: str) -> None:
    path = repo_root / "RELEASES.md"
    text = path.read_text(encoding="utf-8")
    if f"| {new_version} |" in text:
        raise PumpError(
            f"{path} already has a row for version {new_version} -- "
            f"refusing to insert a duplicate."
        )
    separator = "|---|---|---|---|\n"
    if separator not in text:
        raise PumpError(
            f"{path}: could not find the expected table separator row "
            f"'{separator.strip()}' -- check the file format by hand."
        )
    new_row = f"| {new_version} | {new_tag} | {release_date} | {notes} |\n"
    path.write_text(text.replace(separator, separator + new_row, 1), encoding="utf-8")


def run_uv_lock(repo_root: Path) -> None:
    result = subprocess.run(["uv", "lock"], cwd=repo_root, capture_output=True, text=True)
    if result.returncode != 0:
        raise PumpError(
            f"'uv lock' failed (exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version", help="New version, e.g. 2026.7.16 or v2026.7.16")
    parser.add_argument("--date", default=None, help="Release date YYYY-MM-DD (default: today)")
    parser.add_argument("--notes", required=True, help="One-line RELEASES.md summary of this release")
    parser.add_argument(
        "--allow-downgrade",
        action="store_true",
        help="Allow setting a version <= the current one (rollback releases)",
    )
    args = parser.parse_args()

    try:
        new_version, new_tuple = parse_version(args.version)
    except PumpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    release_date = args.date or date_cls.today().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", release_date):
        print(f"error: --date must be YYYY-MM-DD, got '{release_date}'", file=sys.stderr)
        return 1

    repo_root = find_repo_root(Path(__file__).resolve())

    try:
        old_version = current_version(repo_root)
        _, old_tuple = parse_version(old_version)

        if new_tuple <= old_tuple and not args.allow_downgrade:
            raise PumpError(
                f"new version {new_version} is not greater than the "
                f"current version {old_version}. Pass --allow-downgrade "
                f"if this is a deliberate rollback release."
            )

        new_tag = f"v{new_version}"
        check_tag_absent(repo_root, new_tag)

        replace_exact(repo_root / "pyproject.toml", f'version = "{old_version}"', f'version = "{new_version}"', 1)
        replace_exact(
            repo_root / "tests" / "test_release_consistency.py",
            f'CURRENT_VERSION = "{old_version}"',
            f'CURRENT_VERSION = "{new_version}"',
            1,
        )
        replace_exact(
            repo_root / "tests" / "test_install_scripts.py",
            f'CURRENT_RELEASE_TAG = "v{old_version}"',
            f'CURRENT_RELEASE_TAG = "v{new_version}"',
            1,
        )

        old_occurrences_sh = (repo_root / "install.sh").read_text(encoding="utf-8").count(old_version)
        replace_exact(repo_root / "install.sh", old_version, new_version, old_occurrences_sh)

        old_occurrences_ps1 = (repo_root / "install.ps1").read_text(encoding="utf-8").count(old_version)
        replace_exact(repo_root / "install.ps1", old_version, new_version, old_occurrences_ps1)

        old_occurrences_readme = (repo_root / "README.md").read_text(encoding="utf-8").count(old_version)
        replace_exact(repo_root / "README.md", old_version, new_version, old_occurrences_readme)

        bump_changelog(repo_root, new_version, release_date)
        bump_releases_md(repo_root, new_version, new_tag, release_date, args.notes)

        run_uv_lock(repo_root)

    except PumpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"Bumped {old_version} -> {new_version} (tag {new_tag}, date {release_date}).")
    print("Files touched: pyproject.toml, uv.lock, install.sh, install.ps1, README.md,")
    print("CHANGELOG.md, RELEASES.md, tests/test_release_consistency.py, tests/test_install_scripts.py")
    print()
    print("Next steps (not automated by this script):")
    print("  1. git diff --stat   # sanity-check nothing unexpected changed")
    print("  2. pytest tests/test_release_consistency.py tests/test_install_scripts.py")
    print("  3. Review the new CHANGELOG.md / RELEASES.md entries by hand")
    print(f"  4. git commit, then git tag -a {new_tag} -m '...', then push both")
    return 0


if __name__ == "__main__":
    sys.exit(main())
