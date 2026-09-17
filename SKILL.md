---
name: agentos-contribution-workflow
description: >
  End-to-end workflow for pulling latest upstream main, analyzing project for high-impact
  acceptable bugs, branching, implementing changes, running quality gates, committing, pushing,
  and creating both an upstream Issue AND a linked Pull Request (PR) via gh CLI.
---

# AgentOS End-to-End Contribution Workflow

Standard workflow for contributing code to the AgentOS repository
(`use-agent-os/agent-os`). The primary objective of running this skill is to
identify a valid bug, create an upstream **Issue** for it (`gh issue create`),
and submit a matching upstream **Pull Request (PR)** (`gh pr create`) that
resolves the issue (`Fixes #<issue_number>`). For the case where an issue already has
someone else's PR on it, see **SKILLTANDINGAN.md**.

## Main Contribution Flow

### 1. Pull & Sync from Upstream

Always start by pulling the latest changes from `upstream/main` so the local
codebase and the fork are both current:

```bash
git fetch upstream
git checkout main
git rebase upstream/main
git push origin main
```

### 2. Analyse the Project & Check for Conflicts

- List the active upstream issues and PRs so you do not build a duplicate or a
  conflicting fix:
  ```bash
  gh issue list -R use-agent-os/agent-os --state open
  gh pr list -R use-agent-os/agent-os --state open
  ```
- **An issue labelled `status: ready` almost always has 2–5 competing PRs
  already.** Check first:
  ```bash
  gh pr list -R use-agent-os/agent-os --state open --limit 100 | grep -i "<topic>"
  ```
  If someone is already on it, either find a fresh bug that has no issue yet —
  far better odds — or read **SKILLTANDINGAN.md** and decide whether you can
  cover strictly more than the existing PR.
- **Check your own open issues and PRs first.** Different sessions can find the
  same bug and file it twice — this has happened (#1978/#2227 and #1979/#2228,
  with identical reproduction examples). A duplicate from the same author looks
  careless, and one of the two will be closed:
  ```bash
  gh issue list -R use-agent-os/agent-os --author <user> --state open --limit 40
  gh pr list -R use-agent-os/agent-os --author <user> --state open --limit 40
  ```
- Hunt for candidate bugs in the codebase (TODO/FIXME, unpopulated callback
  refs, logic gaps).

### 2a. Verify a Lead Before Writing Anything

Most things that "look like a bug" are not. Four leads died on inspection in a
single session, and every time the cause was the same: **the caller's context
had not been checked.** Answer these three questions before writing an issue.

1. **Has the value already been normalised before it reaches here?** Example:
   `boot.py` compares `config.llm.provider` raw — it looks like a case-folding
   bug, but `resolve_llm_runtime_config` fifty lines earlier **writes the folded
   value back** onto the same config object.
2. **Is this path actually concurrent / actually reached?** `StreamThrottle`
   checks its window before taking the lock, which looks like a race — but all
   three adapters call it from a single sequential loop.
3. **Is the failure silent, or is there in fact a clear error?** The search
   registry rejects an unknown provider with a `ValueError` that lists the valid
   ones. That is correct behaviour, not a bug.

Dropping a lead at minute ten is cheaper than filing a wrong report. A wrong
report costs more than filing nothing.

### 2b. What the Maintainer Accepts

Distilled from the closing comments on **rejected** PRs and from ~40 **merged**
ones. Re-read this before choosing a bug — it is the decisive filter.

**Both conditions are required, not either:**

1. **Demonstrable through a public entry point** — a builtin tool, a CLI
   command, an RPC, a channel that a normal user touches. Not an internal
   function call. The maintainer's words when closing a PR: *"if you can show
   the failure through a public entry point, reopen the issue first."*
2. **It produces a silently WRONG result**, not merely a different error. The
   harm has to land on someone other than a caller who deliberately sent bad
   input.

**Genres that are ALWAYS rejected** (do not spend time here):

- Parameter validation / error-code changes on a path that already fails.
  → *"the change only alters the error code for malformed authenticated requests
  that already fail"* (#1715), *"a blank name only reaches `projects.update` from
  a caller that sent it deliberately"* (#1671), *"malformed `limit`/`cursor` from
  an authenticated caller only errors or enlarges that caller's own response"*
  (#1666).
- Writing a value that has no consumer (#1711).
- A bug only visible from an internal function (#1728, #1731).
- Unicode / UTF-8 stdout encoding wrappers or non-UTF-8 console code page fixes on script output (treated as cosmetic / low-value boilerplate and rejected).

**Genres that get accepted** — look at the merged PR titles; every one has the
shape "something the user asked for produces a wrong result with no warning":

- `stop counting /dev/null as a shell write target`
- `check exact sheet name before positional index in read_spreadsheet`
- `stop reading status codes inside longer numbers`
- `drop the account-wide thread anchor that leaked replies across conversations`
- `resolve a relative workdir against the workspace directory`

**Amplifiers, strongest first:**

- **Precedent.** The same class of defect was already fixed and accepted
  somewhere else, and you found the place it missed. The ruling exists; you are
  only applying it.
- **A chain through the tool itself.** The damage comes from this tool's own
  output becoming this tool's input again — no hand-crafted file needed.
- **Assert on the raw form** (bytes, observed output), not on a value that has
  been translated on the way out. Many defects hide in the translation layer; a
  test comparing normalised results stays green against broken code.
- **State how many tests fail on `main`.** It is the proof the fix actually
  binds.
- **A self-documented invariant that is violated.** When a docstring promises
  something — "every write path funnels through…", "the first half never
  contains a half-open block", "text appearing more than once is rejected rather
  than guessed" — test the promise instead of believing it. Three of the best
  bugs in one session came from exactly that. A small fuzz (2–4k random cases)
  is often faster than reasoning.
- **Fix at the choke point, not at one caller.** If a function already
  canonicalises other things (`canonicalize_session_key`, `normalize_agent_id`),
  put the fix there: the argument becomes "internal consistency" rather than "a
  new addition".

**Reasonable PR size:** +60 to +300 lines, mostly tests.

**Never mix a fix and a new feature in one PR.** This maintainer trims scope
(#1724), and the genuinely correct half ends up held back waiting on a decision
about the feature. Split them.

**When the general fix is blocked by a contract, narrow it to the surface where
it is safe and say why up front.** Example: `split_text_for_limit` cannot change
because Telegram derives its watermark from `len(head)` against the source text,
so adding a closing marker there would drop text. The fix went into Discord's
chunker, which maps nothing back onto offsets. A reviewer will ask "why not in
the shared helper?" — answer it first, in the PR body.

### 3. Branch and Implement

- Branch from an up-to-date `main`:
  ```bash
  git checkout main
  git checkout -b fix/<scope>-<topic>
  ```
- Write an `implementation_plan.md` artifact and ask the user to confirm.
- Sync dependencies (`uv sync --extra dev --extra recommended`).
- Write the fix and add unit tests under `tests/`.
- **Give every claim its own failing test.** The maintainer's note when closing
  #1725: *"the stop_reason / output_tokens guards had no failing test of their
  own"*. If a fix has three parts, aim for three red tests. Tests that stay
  green on both sides are still valuable as **guards** (a positive control,
  evidence of no regression) — but label them as such; never count them as
  proof.
- **Include one positive control through a public entry point.** A negative test
  can pass vacuously on a misconfigured context; the positive control proves the
  path is live.
- **Prove the tests fail against the old code** before going further — this is
  what separates a real fix from a test that merely documents current behaviour:
  ```bash
  git stash push -q <changed-source-file>
  uv run pytest <new-test-file> -q      # must be RED
  git stash pop -q
  ```
  Record the count for the commit and PR ("3 of 4 tests fail on `main`"). This
  only works on a branch cut from `main`. On a PR branch that already carries
  the fix commit, `git stash` reverts to the *PR* version — see the trap in
  section 5.
- Run the quality gate:
  ```bash
  uv run pytest tests/path/to/test.py -q
  uv run ruff check src tests
  uv run ruff format --check <changed-files>   # NOT `ruff format src tests`
  uv run mypy src/agentos --show-error-codes
  ```
  > `pyproject.toml` pins only `ruff>=0.5`. The installed version is newer than
  > the one `main` was formatted with, so `ruff format src tests` rewrites ~431
  > unrelated files and buries your fix. Scope it to the files you touched.
  > If it already happened: `git stash push <your-files>` →
  > `git checkout -- .` → `git stash pop`.
  >
  > A heredoc writes LF into a CRLF checkout. If `ruff format --check` complains
  > about a file you appended to with a heredoc, run `ruff format <that-file>`.
  > If it complains about old lines you never touched, that is the ruff-version
  > churn above — leave it alone.
- `CHANGELOG.md` is **optional, and it carries risk.** Contributor PRs merge
  without one: of the last twelve merged PRs, every entry came from the
  maintainer's own PRs, and all seven external ones (iamhaniofficial ×6,
  tejajakarulloh) touched no `CHANGELOG.md` at all. The maintainer writes the
  release note when landing the change.

  So an entry buys nothing on its own, and a **misplaced** one is a strike — the
  supersede notes on #1877 and #1725 both named it ("this PR's hunk lands inside
  the already-released `[2026.9.14]` section after a 3-way merge"). If you add
  one, put it under `## [Unreleased]` → `### Fixed` and re-verify its anchor
  after every upstream release (section 6). If you would rather not carry that
  maintenance, omit it — that is what the contributors who get merged do.
- Write `walkthrough.md` and commit with a Conventional Commit:
  ```bash
  git add <files>
  git commit -m "fix(<scope>): <description>"
  ```
- Push the branch to the `origin` fork:
  ```bash
  git push -u origin fix/<topic>
  ```

### 4. File the Issue and Submit the PR (Mandatory Dual Output)

The goal of this workflow is to deliver both an upstream **Issue** and a matching **PR**:

1. **Create the upstream Issue first** using `gh issue create` (unless competing on an existing triaged issue).
2. **Submit the PR upstream** linking `Fixes #<issue_number>` so that the Issue and PR are explicitly paired.

Commands:
- Make sure issues are enabled on the repository:
  ```bash
  gh repo edit <owner>/<repo> --enable-issues
  ```
- Create the issue upstream:
  ```bash
  gh issue create -R use-agent-os/agent-os --title "[Bug]: <Title>" --body-file "<path-to-issue-body>"
  ```
- Submit the PR upstream, linking `Fixes #<issue_number>`:
  ```bash
  gh pr create -R use-agent-os/agent-os --head <user>:fix/<topic> --base main --title "fix(<scope>): <Title>" --body-file "<path-to-pr-body>"
  ```

**Issue body skeleton** — the order matters; the maintainer decides on the first
two sections:

1. `### Summary` — one paragraph: what is wrong, not where the code is.
2. `### Reproduction` — a paste-and-run script, then the **actual** output
   observed on a named `main` commit (quote the SHA), then an "Expected:" line.
3. `### Why this is reachable in normal use` — the section that saves the PR
   from the "only a deliberate caller gets here" rejection. Show the ordinary
   path.
4. `### Cause` — quote the lines, briefly.
5. `### Impact` — who is harmed and how.
6. `### Fix` — the direction, plus what you deliberately did **not** fix and why.

**PR body skeleton:** Summary → Why it matters → Changes → the design decision a
reviewer would question (explain it first) → a Verification block with the
quality-gate commands and their real output → `Fixes #<n>`.

Never quote a test count you have not actually run. Run it, then quote it.

### 5. After Submitting: Read the Triage

The maintainer (`andreapn`) leaves a **Triage** comment on every issue and
applies labels. The label tells you whether work remains:

| Label | Meaning | What to do |
|---|---|---|
| `status: needs review` | Issue accepted, its PR is queued for review | **Nothing.** Wait. |
| `status: ready` | Accepted, but triage set or changed the scope | **Read the comment.** If scope was cut, trim the PR. |
| Issue closed | Rejected | The PR closes with it; do not resubmit the same thing. |

A triage comment usually carries a numbered **Accepted scope** block and — this
is the important one — a **Not accepted** block. A PR that exceeds the accepted
scope gets closed even when its core is correct. If that happens:

1. `git fetch origin <branch>` then `git checkout <branch>`, and rebase onto
   `main` (an old branch can be dozens of commits behind).
2. Trim the diff until it matches the accepted-scope list **exactly** — no less,
   no more.
3. Delete tests covering the rejected scenario; rewrite them for the accepted
   one.
4. `git commit --amend`, then `git push --force-with-lease` (the rebase already
   forces this), update the PR title and body, and leave a comment naming what
   you kept and what you dropped.

> **Trap when re-verifying:** `git stash push <file>` on a PR branch reverts the
> file to the **PR** version, not to `main` — the tests look green on both sides
> and you draw the wrong conclusion. To compare against `main`: copy the working
> file to the scratchpad, `git checkout main -- <file>`, run the tests, then copy
> it back.

Never claim a test "fails on `main`" without having actually run it against
`main`. A test that passes on both sides is fine as a guard — just say so.

### 6. Maintain the Submitted PR Portfolio

Submitted PRs **rot on their own**. Run this sweep at the start of every session,
and after any upstream release.

**Status sweep:**

```bash
gh pr list -R use-agent-os/agent-os --author <user> --state open --limit 40 \
  --json number,title,mergeable --jq '.[] | "\(.number)\t\(.mergeable)\t\(.title[0:56])"'
```

**Four things to check every sweep:**

1. **CHANGELOG placement rots when a release is cut.** This is the one that
   bites most often. When a release is cut, the contents of `[Unreleased]` move
   into a `[<version>]` section and `[Unreleased]` is emptied. Your old branch's
   entry is still anchored on context that now lives **inside the released
   section**, so the three-way merge puts it there. The maintainer cited this
   twice as a reason a PR did not land cleanly (#1877, #1725).

   Quick check across a PR — the entry must land at line ≤ 9 (`[Unreleased]` is
   at line 7):
   ```bash
   gh pr diff <n> -R use-agent-os/agent-os \
     | awk '/^\+\+\+ b\/CHANGELOG.md/{f=1;next} f&&/^@@/{print;exit}'
   ```
   If it missed: rebase onto `main`, **save the entry text first**, reset
   `CHANGELOG.md` to the `main` version, re-insert it under `## [Unreleased]`
   with a `### Fixed` heading, then `git commit --amend` and
   `--force-with-lease`.

2. **Which of your PRs carry a CHANGELOG entry at all.** Not a defect — external
   PRs merge without one (section 3) — but it tells you which PRs need the
   check above after a release, and which are free of that maintenance.
   ```bash
   gh pr view <n> -R use-agent-os/agent-os --json files \
     --jq 'if ([.files[].path] | index("CHANGELOG.md")) then "has entry" else "none" end'
   ```

3. **Your own duplicates.** Compare titles and touched files across your PRs. If
   two touch the same file and the same bug, **consolidate them yourself before
   the maintainer finds them**: keep the more correct implementation, close the
   other with a concrete comparison table, and close the twin issue too.

4. **Two PRs touching one file.** Not necessarily a conflict, but prove it:
   ```bash
   git checkout -B trial/merge <branch-a>
   git merge origin/<branch-b> && uv run pytest <suite> -q
   git checkout main && git branch -D trial/merge
   ```

**Supersede risk.** A PR can be closed as *superseded* even when it is correct —
#1877 and #1725 both "correctly fix the issue" but lost to a PR that landed
first **and** measured better (held the configured rate at exactly 30.0 vs 29.1;
handled an empty `usage: {}` that an `isinstance` check alone lets through). The
lesson: speed matters, but what decides a head-to-head is **measured
behaviour** — put the numbers in the PR.

> **Script trap:** `set -e` does **not** catch a failure inside a pipe.
> `git rebase --quiet | tail -2` swallowed a conflict and the script went on to
> corrupt the next branch. After any batch operation, verify each branch
> individually (`git merge-base --is-ancestor main HEAD`, entry position, file
> scope, tests).
