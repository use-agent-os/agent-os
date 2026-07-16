---
name: pump-version
description: Bump the AgentOS release version atomically across every file the release-consistency tests check (pyproject.toml, uv.lock, install.sh, install.ps1, README.md, CHANGELOG.md, RELEASES.md, tests/test_release_consistency.py, tests/test_install_scripts.py). Use this whenever the user asks to "bump version", "pump version", "prepare a release", "cut a release", "release AgentOS vX.Y.Z", or otherwise wants to move the project to a new CalVer version ahead of tagging/publishing -- even if they only mention one or two of the files, since a partial manual bump is exactly what breaks CI. Do not hand-edit these files individually; always route version changes through this skill's script.
---

# pump-version

AgentOS uses CalVer (`YYYY.M.D`, optionally `.postN` for a same-day
re-release) and pins that exact version string into nine different files.
`tests/test_release_consistency.py` and `tests/test_install_scripts.py`
assert every one of them agrees, so a bump that only touches
`pyproject.toml` fails CI. This skill's script edits all nine in one
atomic pass and refuses to run if any of them don't look like what it
expects, rather than silently leaving some stale.

## Why a script instead of editing by hand

Two real incidents drove this:

- A bump once forgot to regenerate `uv.lock`, so `pyproject.toml` said
  one version and the lockfile said another; CI failed and needed a
  same-day follow-up commit.
- A version was bumped backwards during a rollback with no downgrade
  check anywhere in the process, and combined with no tag protection at
  the time, led to a multi-step cleanup (deleted GitHub release, deleted
  local+remote git tags, force-push, re-tag).

The script guards against both: it always runs `uv lock` after editing
`pyproject.toml`, and it refuses a version that isn't strictly greater
than the current one unless you pass `--allow-downgrade`. It also
refuses if the target git tag already exists locally or on origin,
since re-tagging an existing version is what triggered the second
incident.

## How to run it

```bash
python .claude/skills/pump-version/scripts/pump_version.py 2026.7.16 \
  --notes "Short description of what this release contains"
```

- Positional `version`: new version, with or without a leading `v`
  (`2026.7.16` or `v2026.7.16`). Must be `YYYY.M.D` or `YYYY.M.D.postN`
  with no leading zeros in any segment -- PEP 440 strips leading zeros
  when normalizing wheel filenames, so a padded version like
  `2026.07.16` makes the built wheel's name disagree with what CI
  expects and `wheelhouse-release.yml` fails.
- `--notes "..."` (required): one-line summary for the new `RELEASES.md`
  row. Ask the user what the release is about if it's not obvious from
  recent commits -- this can't be auto-derived.
- `--date YYYY-MM-DD` (optional): defaults to today. Only override for a
  backdated or scheduled release.
- `--allow-downgrade` (optional): required to set a version that is not
  strictly greater than the current one. Only pass this for a
  deliberate rollback, and confirm with the user first -- this is
  exactly the scenario that caused the 2026-07-14 incident above.

## What it does

1. Reads the current version out of `pyproject.toml`.
2. Validates the new version's format and, unless `--allow-downgrade`,
   that it's strictly greater than the current one.
3. Confirms tag `vX` doesn't already exist locally or on `origin`.
4. Replaces the version string in: `pyproject.toml`, `install.sh`,
   `install.ps1`, `README.md`, `tests/test_release_consistency.py`
   (`CURRENT_VERSION`), `tests/test_install_scripts.py`
   (`CURRENT_RELEASE_TAG`). Each replacement asserts the expected number
   of matches first and aborts the whole run without touching the file
   if the count is off -- a file's format changing since this script was
   last touched should stop the bump, not produce a half-correct edit.
5. Inserts a new `## [X] - DATE` section into `CHANGELOG.md` right after
   `## [Unreleased]`, which promotes whatever is currently listed under
   Unreleased into the new release's section and leaves Unreleased empty
   above it -- matching how every real bump commit in this repo's
   history has done it.
6. Inserts a new top row into the `RELEASES.md` table.
7. Runs `uv lock` to regenerate the lockfile against the new
   `pyproject.toml` version.

It does **not** commit, tag, or push anything -- that stays a manual,
deliberate step (see below), consistent with not auto-committing without
being asked.

## After running it

1. `git diff --stat` -- sanity-check only the expected files changed.
2. `pytest tests/test_release_consistency.py tests/test_install_scripts.py`
   -- both should pass now that every pinned string agrees.
3. Read the new `CHANGELOG.md` and `RELEASES.md` entries by hand. The
   CHANGELOG promotion only moves the section header; if `[Unreleased]`
   was actually empty, add real changelog bullets before committing.
4. Commit, then `git tag -a vX -m "..."`, then push the commit and the
   tag -- only when the user asks for this step explicitly. Pushing the
   tag triggers `.github/workflows/wheelhouse-release.yml`.
