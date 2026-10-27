"""Did the turn that changed code ever check that the code still works?

A turn that edits three files and then answers "done" is the most common way an
agent is confidently wrong. Nothing in the loop notices: every tool call
succeeded, the model produced text, the turn ended cleanly. The only thing
missing is the one step that would have caught the mistake.

This module keeps a passive ledger of two things per turn — which files were
mutated, and whether a verification command ran *after* the last mutation — and
answers one question at the end: is the model about to stop on unverified
edits?

It is policy only. It runs nothing, and it has no opinion on what the right
command is; it recognises that one ran. Whether the answer becomes a log line,
a warning, or a follow-up turn belongs to the runtime.

Two exclusions keep it from crying wolf, both learned the hard way in the
runtimes that shipped this before:

* **Prose is not code.** A turn that edits only Markdown, text or data files has
  nothing a test could exercise. Demanding verification for a README edit
  teaches the model to ignore the nudge.
* **Chat is not a workspace.** On a messaging surface the agent is answering a
  person, not maintaining a checkout, and there is usually no test command to
  run at all.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import asdict, dataclass, field
from typing import Any

# Editing one of these produces nothing a build or test suite can exercise.
_NON_CODE_SUFFIXES = frozenset(
    {
        ".md",
        ".markdown",
        ".mdx",
        ".rst",
        ".txt",
        ".log",
        ".csv",
        ".tsv",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".lock",
        ".svg",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".pdf",
    }
)

# Programs whose presence in a command means the turn checked its own work.
_VERIFICATION_PROGRAMS = frozenset(
    {
        "pytest",
        "tox",
        "nox",
        "unittest",
        "mypy",
        "ruff",
        "flake8",
        "pylint",
        "black",
        "isort",
        "eslint",
        "prettier",
        "tsc",
        "vitest",
        "jest",
        "mocha",
        "playwright",
        "cypress",
        "cargo",
        "go",
        "gradle",
        "mvn",
        "make",
        "cmake",
        "ctest",
        "bazel",
        "rspec",
        "phpunit",
        "dotnet",
        "swift",
    }
)

# Subcommands that turn an otherwise ambiguous program into a verification run.
_VERIFICATION_SUBCOMMANDS = frozenset({"test", "check", "lint", "build", "vet", "typecheck"})

# Runners that only say what comes next; the real program is the following word.
_RUNNER_PREFIXES = frozenset({"uv", "uvx", "npm", "npx", "pnpm", "yarn", "bun", "poetry", "pipenv"})

_MAX_PATHS_IN_NUDGE = 8

# Built-in tools that change files on disk, and so count as a mutation. Shell
# and code-execution tools may change files too, but their commands are not
# parsed for paths.
MUTATING_TOOL_NAMES = frozenset({"write_file", "edit_file", "apply_patch"})

_PATCH_FILE_MARKERS = ("*** Update File:", "*** Add File:", "*** Delete File:")


def mutated_paths(arguments: dict[str, Any]) -> list[str]:
    """Pull the touched paths out of a mutating tool's arguments."""

    paths: list[str] = []
    for key in ("path", "file_path", "target"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
    patch = arguments.get("patch")
    if isinstance(patch, str):
        # apply_patch names its files inside the patch body.
        for line in patch.splitlines():
            stripped = line.strip()
            for marker in _PATCH_FILE_MARKERS:
                if stripped.startswith(marker):
                    candidate = stripped[len(marker) :].strip()
                    if candidate:
                        paths.append(candidate)
    return paths


@dataclass(frozen=True)
class VerificationDecision:
    """Whether the turn is stopping on unverified edits, and what changed."""

    needs_verification: bool
    reason: str
    changed_paths: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_code_path(path: str) -> bool:
    """Whether editing *path* could plausibly break something a check would catch."""

    text = str(path or "").strip()
    if not text:
        return False
    name = text.replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in name:
        # No suffix at all — a Makefile, a Dockerfile, a shell script. Treat as
        # code: these are exactly the files whose breakage is silent.
        return True
    suffix = name[name.rfind(".") :].casefold()
    return suffix not in _NON_CODE_SUFFIXES


def looks_like_verification(command: str) -> bool:
    """Whether *command* is the turn checking its own work.

    Recognition, not prescription: the point is to notice that some check ran,
    not to decide which one should have.
    """

    text = str(command or "").strip()
    if not text:
        return False
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = re.split(r"\s+", text)

    words = [token.rsplit("/", 1)[-1].casefold() for token in tokens if token]
    for index, word in enumerate(words):
        if word in _VERIFICATION_PROGRAMS:
            if word in {"cargo", "go", "dotnet", "swift", "gradle", "mvn"}:
                # These do many things; only some of them verify anything.
                following = words[index + 1] if index + 1 < len(words) else ""
                if following in _VERIFICATION_SUBCOMMANDS:
                    return True
                continue
            return True
        if word in _RUNNER_PREFIXES:
            continue
        if word in _VERIFICATION_SUBCOMMANDS and index > 0:
            # `npm test`, `pnpm check`, `make lint`.
            return True
    return False


class VerificationLedger:
    """Per-turn record of mutations and the checks that followed them."""

    def __init__(self) -> None:
        self._changed: dict[str, None] = {}
        self._verified_after_last_change = False

    def record_mutation(self, paths: list[str] | tuple[str, ...]) -> None:
        """Note files this turn changed. Any mutation invalidates prior evidence."""

        added = False
        for path in paths:
            if is_code_path(path):
                self._changed[str(path)] = None
                added = True
        if added:
            # Evidence gathered before this edit says nothing about it.
            self._verified_after_last_change = False

    def record_command(self, command: str) -> None:
        if looks_like_verification(command):
            self._verified_after_last_change = True

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(self._changed)

    def decide(self, *, is_messaging_surface: bool = False) -> VerificationDecision:
        """Answer whether stopping here would leave edits unchecked."""

        if not self._changed:
            return VerificationDecision(False, "no_code_changes")
        if self._verified_after_last_change:
            return VerificationDecision(
                False, "verified", changed_paths=self.changed_paths
            )
        if is_messaging_surface:
            # Answering a person in a chat, not maintaining a checkout.
            return VerificationDecision(
                False, "messaging_surface", changed_paths=self.changed_paths
            )
        return VerificationDecision(
            True,
            "unverified_code_changes",
            changed_paths=self.changed_paths,
            details={"changed_count": len(self._changed)},
        )


def build_verification_nudge(decision: VerificationDecision) -> str:
    """A sentence naming what changed and what was not done about it."""

    if not decision.needs_verification:
        return ""
    paths = list(decision.changed_paths)
    shown = paths[:_MAX_PATHS_IN_NUDGE]
    listed = ", ".join(shown)
    if len(paths) > len(shown):
        listed += f", and {len(paths) - len(shown)} more"
    return (
        f"This turn changed {listed} and no test, build or lint command ran"
        " afterwards. Run the check that covers the change before calling it done,"
        " or say plainly that it is unverified."
    )
