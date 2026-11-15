"""Bundled skills' hot-wallet keys: held back from the agent, released per command.

``senior-unilp-manager``, ``poolsdotfun-token-launcher`` and the ``gmgn-*``
skills sign with a key the operator puts in the environment
(:data:`agentos.env_policy.SKILL_SIGNING_KEY_ENV`), outside the trading
engine's vault, guardrails and parked-order approvals. Their only gate used to
be a ``PLAN_HASH`` the same script prints on its dry run -- an agent satisfies
it by running the command twice (WAL-10) -- and for GMGN a confirmation prompt
the installed ``gmgn-cli`` no longer has (WAL-04). With the key in every child
environment, no script-side check could matter anyway: an agent can sign with
it directly.

So the key no longer crosses into a child process at all
(:func:`agentos.tools.env_passthrough.build_subprocess_env` strips it), and
this module is the one place it is handed back out:

* only to a **direct** invocation of a known entry point -- one command, no
  pipe, redirect, substitution or second command, at most a leading
  ``S="<dir>"`` assignment as the SKILL.md files write it -- which AgentOS
  then runs itself, without a shell, from its **own packaged copy** of the
  script (or the pinned ``gmgn-cli``), so nothing the agent wrote or edited
  runs with the key;
* and, when that invocation moves funds (``--broadcast``, a ratchet mandate
  being armed, a GMGN swap or order), only after the **operator** approved
  that exact command. The approval goes through the exec approval queue but
  ignores ``/elevated``, ``bypass`` and ``auto-approve``: none of those is a
  human looking at this transaction.

The one unattended path is ``ratchet.py tick --broadcast`` (it is what cron
runs). It gets the key with no prompt, but only for mandates the operator
approved: an approved ``arm --confirm`` records the mandate's id and a digest
of its bounds in a store on the sensitive-path floor, and the tick refuses any
mandate not listed there with the same bounds. A tick always pays the signer
itself, never a third address.

Not covered, and said so in the skills' docs: a key the operator keeps in a
file the CLI reads itself (``~/.config/gmgn/.env``) is outside this custody,
and a same-user agent with the OS sandbox off that rewrites AgentOS's own
installed files can do anything AgentOS can.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import sys
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from agentos.env_policy import SKILL_SIGNING_KEY_ENV
from agentos.tools.env_passthrough import SIGNING_KEYS_HELD_ENV, build_subprocess_env

__all__ = [
    "ARM_RECEIPT_ENV",
    "GATED_ENV",
    "GMGN_CLI_VERSION",
    "KEYS_HELD_ENV",
    "OPERATOR_MANDATES_ENV",
    "SKILL_SIGNING_KEY_ENV",
    "SigningInvocation",
    "SigningRefusedError",
    "approved_mandates",
    "classify_command",
    "finish_invocation",
    "invocation_env",
    "mandate_digest",
    "record_mandate",
    "script_signing_invocation",
    "signing_store_dir",
]

#: The ``gmgn-cli`` release AgentOS has reviewed. The ``gmgn-*`` skills install
#: exactly this version, and the key is never handed to any other one.
GMGN_CLI_VERSION = "1.7.1"

#: Set (to ``1``) in every child environment, so a skill script can tell the
#: user why its key is missing instead of just "not set".
KEYS_HELD_ENV = SIGNING_KEYS_HELD_ENV

#: JSON ``{mandate_id: bounds_digest}`` of operator-approved ratchet mandates,
#: given to a gated ``ratchet.py tick``.
OPERATOR_MANDATES_ENV = "UNILP_OPERATOR_MANDATES"

#: A file the gated ``ratchet.py arm --confirm`` writes its receipt to.
ARM_RECEIPT_ENV = "UNILP_ARM_RECEIPT"

#: Set for a gated run. The skill scripts then read no dotenv file at all: the
#: environment is the gateway's, so the RPC endpoint a broadcast uses is the
#: operator's setting or a ``--rpc`` the operator saw on the approval card --
#: never a ``.env`` the agent dropped in the working directory.
GATED_ENV = "AGENTOS_SKILL_SIGNING_GATED"

_BUNDLED = Path(__file__).resolve().parents[1] / "skills" / "bundled"

_UNILP = "senior-unilp-manager"
_POOLS = "poolsdotfun-token-launcher"

#: (skill directory, script) -> (key it signs with, its package of helpers).
_SCRIPTS: dict[tuple[str, str], tuple[str, str]] = {
    (_UNILP, "lp_write.py"): ("UNIV4_LP_PRIVATE_KEY", "unilp"),
    (_UNILP, "lp_read.py"): ("UNIV4_LP_PRIVATE_KEY", "unilp"),
    (_UNILP, "ratchet.py"): ("UNIV4_LP_PRIVATE_KEY", "unilp"),
    (_POOLS, "pools_write.py"): ("POOLSFUN_PRIVATE_KEY", "poolsfun"),
    (_POOLS, "pools_read.py"): ("POOLSFUN_PRIVATE_KEY", "poolsfun"),
}

#: The flags an unattended tick may carry and still skip the prompt. Anything
#: else (``--rpc``, ``--deadline-secs``, ...) changes what the fire does, so it
#: is the operator's call.
_UNATTENDED_TICK_FLAGS = frozenset({"_", "all", "id", "broadcast", "json", "alert-only", "chain"})

_GMGN_MOVES_FUNDS: tuple[tuple[str, ...], ...] = (
    ("swap",),
    ("multi-swap",),
    ("order", "strategy", "create"),
    ("order", "strategy", "cancel"),
    ("cooking", "create"),
)
#: Critical-auth reads: they need the key to sign the API request, move nothing.
_GMGN_READS: tuple[tuple[str, ...], ...] = (
    ("order", "quote"),
    ("order", "get"),
    ("order", "strategy", "list"),
)

_MANDATE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_PYTHON_RE = re.compile(r"^python(?:3(?:\.\d+)?)?(?:\.exe)?$", re.IGNORECASE)
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)


class SigningRefusedError(ValueError):
    """The command is a signing invocation AgentOS will not run with the key."""


@dataclass(frozen=True)
class SigningInvocation:
    """One recognised command, and what AgentOS will actually execute for it."""

    skill: str
    entry: str
    argv: tuple[str, ...]
    key_env: str
    needs_approval: bool
    action: str
    #: ``ratchet.py tick --broadcast``: no prompt, approved mandates only.
    unattended_fire: bool = False
    #: ``ratchet.py arm --confirm <id>``: record ``id`` once the arm succeeds.
    mandate_to_record: str | None = None
    #: Directory to run in instead of the caller's (the unattended tick).
    cwd: str | None = None
    extra_env: Mapping[str, str] = field(default_factory=dict)


# --- the command shape -------------------------------------------------------


def _lex(command: str) -> list[list[list[tuple[str, str]]]] | None:
    """Split ``command`` into commands -> words -> segments, or ``None``.

    A deliberately small subset of POSIX ``sh``: quotes, backslash escapes,
    ``$NAME`` / ``${NAME}``, a leading ``~``, and ``;`` / ``&&`` / newline
    between commands. Anything else -- a pipe, a redirect, a glob, command
    substitution, a brace -- is not something AgentOS can run without a shell
    and still mean what the operator read, so the whole command is not
    recognised (and then runs through the shell, without the key).
    """
    for ch in command:
        if ch in " \t\n":
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp", "Co", "Cs", "Cn"):
            # Control, format (BIDI overrides, zero-width) and unassigned
            # characters can make the approval card read differently from
            # what runs.
            return None
    backslash_escapes = os.name != "nt"
    commands: list[list[list[tuple[str, str]]]] = [[]]
    word: list[tuple[str, str]] | None = None
    i, n = 0, len(command)

    def flush() -> None:
        nonlocal word
        if word is not None:
            commands[-1].append(word)
            word = None

    while i < n:
        ch = command[i]
        if ch in " \t":
            flush()
            i += 1
            continue
        if ch in "\n;":
            flush()
            commands.append([])
            i += 1
            continue
        if ch == "&":
            if command.startswith("&&", i):
                flush()
                commands.append([])
                i += 2
                continue
            return None
        if ch in "|<>()`{}*?[]#!":
            return None
        if word is None:
            word = []
        if ch == "\\" and backslash_escapes:
            if i + 1 >= n:
                return None
            word.append(("lit", command[i + 1]))
            i += 2
            continue
        if ch == "'":
            end = command.find("'", i + 1)
            if end < 0:
                return None
            word.append(("lit", command[i + 1 : end]))
            i = end + 1
            continue
        if ch == '"':
            i += 1
            while True:
                if i >= n:
                    return None
                c = command[i]
                if c == '"':
                    i += 1
                    break
                if c == "`":
                    return None
                if c == "\\" and backslash_escapes and i + 1 < n and command[i + 1] in '$`"\\\n':
                    word.append(("lit", command[i + 1]))
                    i += 2
                    continue
                if c == "$":
                    name, i = _variable(command, i)
                    if name is None:
                        return None
                    word.append(("qvar", name))
                    continue
                word.append(("lit", c))
                i += 1
            continue
        if ch == "$":
            name, i = _variable(command, i)
            if name is None:
                return None
            word.append(("var", name))
            continue
        if ch == "~" and (not word or _assignment_prefix(word)):
            following = command[i + 1] if i + 1 < n else ""
            if following not in ("", "/", " ", "\t", "\n", ";"):
                return None  # ~user
            # After NAME= the shell expands ~ only in a real assignment, so that
            # form is valid only as the leading S=... word.
            word.append(("tilde", "") if not word else ("assign-tilde", ""))
            i += 1
            continue
        word.append(("lit", ch))
        i += 1
    flush()
    return [c for c in commands if c]


def _variable(command: str, i: int) -> tuple[str | None, int]:
    if command.startswith("${", i):
        end = command.find("}", i + 2)
        name = command[i + 2 : end] if end > 0 else ""
        if not _NAME_RE.fullmatch(name):
            return None, i
        return name, end + 1
    match = _NAME_RE.match(command, i + 1)
    if match is None:
        return None, i
    return match.group(0), match.end()


def _assignment_prefix(word: list[tuple[str, str]]) -> bool:
    text = "".join(t for kind, t in word if kind == "lit")
    return len(word) >= 1 and all(k == "lit" for k, _ in word) and bool(_ASSIGNMENT_RE.match(text))


def _expand(
    word: list[tuple[str, str]], variables: Mapping[str, str], *, assignment: bool = False
) -> str | None:
    out: list[str] = []
    for kind, text in word:
        if kind == "lit":
            out.append(text)
        elif kind == "tilde" or (kind == "assign-tilde" and assignment):
            out.append(str(Path.home()))
        elif kind == "assign-tilde":
            return None
        else:
            value = variables.get(text)
            if value is None:
                return None
            if kind == "var" and (not value or any(c.isspace() or c in "*?[" for c in value)):
                return None  # the shell would split or glob it
            out.append(value)
    return "".join(out)


_TRAILING_STDERR_MERGE = re.compile(r"\s2>&1\s*$")


def _invocation_words(command: str) -> list[str] | None:
    # exec_command already merges stderr into what it returns, so a trailing
    # 2>&1 means nothing here; any other redirect is not accepted.
    commands = _lex(_TRAILING_STDERR_MERGE.sub("", command.strip()))
    if not commands or len(commands) > 2:
        return None
    variables: dict[str, str] = {"HOME": str(Path.home())}
    if len(commands) == 2:
        if len(commands[0]) != 1:
            return None
        assigned = _expand(commands[0][0], variables, assignment=True)
        match = _ASSIGNMENT_RE.match(assigned or "")
        if match is None:
            return None
        variables[match.group(1)] = match.group(2)
    words = [_expand(word, variables) for word in commands[-1]]
    if any(word is None for word in words):
        return None
    expanded = [word for word in words if word is not None]
    if expanded and expanded[0] == "exec":
        expanded = expanded[1:]
    if not expanded or _ASSIGNMENT_RE.match(expanded[0]):
        return None  # FOO=bar cmd: an environment override
    return expanded


def _is_python(word: str) -> bool:
    if _PYTHON_RE.match(Path(word).name):
        return True
    executable = sys.executable or ""
    return bool(executable) and os.path.normcase(word) == os.path.normcase(executable)


# --- the skills' own argument parser ----------------------------------------

_FMT_CACHE: dict[str, ModuleType] = {}


def _skill_fmt(skill: str, package: str) -> ModuleType:
    """The skill's ``fmt`` module, so flags are read exactly as the script reads them."""
    cached = _FMT_CACHE.get(package)
    if cached is not None:
        return cached
    root = _BUNDLED / skill / "scripts" / package
    private = f"_agentos_signing_{package}"
    spec = importlib.util.spec_from_file_location(
        private, root / "__init__.py", submodule_search_locations=[str(root)]
    )
    if spec is None or spec.loader is None:
        raise SigningRefusedError(f"cannot load {skill}'s argument parser")
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[private] = pkg
    spec.loader.exec_module(pkg)
    fmt_spec = importlib.util.spec_from_file_location(f"{private}.fmt", root / "fmt.py")
    if fmt_spec is None or fmt_spec.loader is None:
        raise SigningRefusedError(f"cannot load {skill}'s argument parser")
    fmt = importlib.util.module_from_spec(fmt_spec)
    sys.modules[f"{private}.fmt"] = fmt
    fmt_spec.loader.exec_module(fmt)
    _FMT_CACHE[package] = fmt
    return fmt


# --- classification ----------------------------------------------------------


def classify_command(command: str, cwd: str | None) -> SigningInvocation | None:
    """Recognise a direct invocation of a signing entry point.

    Returns ``None`` for anything else -- including a signing script run in a
    shape this module does not accept, which then runs as an ordinary command
    without the key and fails closed. Raises :class:`SigningRefusedError` for a
    recognised invocation AgentOS will not run (an unreviewed ``gmgn-cli``).
    """
    if "gmgn-cli" not in command and not any(name in command for _, name in _SCRIPTS):
        return None
    words = _invocation_words(command)
    if not words:
        return None
    head = Path(words[0]).name.lower()
    if head in ("gmgn-cli", "gmgn-cli.cmd", "gmgn-cli.exe"):
        return _classify_gmgn(command, words[1:])
    if len(words) < 2 or not _is_python(words[0]) or words[1].startswith("-"):
        return None
    script = Path(words[1]).expanduser()
    if not script.is_absolute():
        script = Path(cwd or os.getcwd()) / script
    entry = (script.parent.parent.name.lower(), script.name.lower())
    if script.parent.name.lower() != "scripts" or entry not in _SCRIPTS:
        return None
    key_env, package = _SCRIPTS[entry]
    packaged = _BUNDLED / entry[0] / "scripts" / entry[1]
    if not packaged.is_file():
        return None
    args = words[2:]
    parsed: dict[str, Any] = _skill_fmt(entry[0], package).parse_args(list(args))
    return _classify_script(entry, key_env, packaged, args, parsed)


def _classify_script(
    entry: tuple[str, str],
    key_env: str,
    packaged: Path,
    args: list[str],
    parsed: dict[str, Any],
) -> SigningInvocation:
    skill, script = entry
    sub = parsed["_"][0] if parsed.get("_") else ""
    label = f"{skill} {script} {sub}".rstrip()
    # -I: no PYTHON* variables, no user site, no script-dir/cwd on sys.path, so
    # nothing next to the command can be imported in place of the skill's code.
    argv = (sys.executable, "-I", "-X", "utf8", str(packaged), *args)
    base: dict[str, Any] = {"skill": skill, "entry": script, "argv": argv, "key_env": key_env}
    if script in ("lp_write.py", "pools_write.py") and parsed.get("broadcast"):
        recipient = parsed.get("recipient") or parsed.get("fee-recipient")
        to = f" Payout recipient: {recipient}." if isinstance(recipient, str) else ""
        return SigningInvocation(
            **base,
            needs_approval=True,
            action=f"{label}: signs with {key_env} and broadcasts a transaction.{to}",
        )
    if script == "ratchet.py":
        if sub == "arm" and parsed.get("confirm"):
            confirm = str(parsed["confirm"])
            return SigningInvocation(
                **base,
                needs_approval=True,
                action=(
                    f"{label}: arms ratchet mandate {confirm}, which lets the unattended "
                    f"tick withdraw and redeploy that position with {key_env}."
                ),
                mandate_to_record=confirm if _MANDATE_ID_RE.match(confirm) else None,
            )
        if sub == "clear-attention":
            return SigningInvocation(
                **base,
                needs_approval=True,
                action=f"{label}: re-arms a halted ratchet mandate (operator step).",
            )
        if sub == "tick" and parsed.get("broadcast"):
            if set(parsed) - _UNATTENDED_TICK_FLAGS or len(parsed.get("_") or []) != 1:
                return SigningInvocation(
                    **base,
                    needs_approval=True,
                    action=f"{label}: an unattended fire with non-default flags.",
                )
            return SigningInvocation(
                **base,
                needs_approval=False,
                action=f"{label}: fires operator-approved mandates only.",
                unattended_fire=True,
                cwd=str(packaged.parent),
            )
    return SigningInvocation(
        **base, needs_approval=False, action=f"{label}: dry run / read, signs nothing."
    )


def _classify_gmgn(command: str, args: list[str]) -> SigningInvocation | None:
    path: list[str] = []
    for arg in args:
        if arg.startswith("-"):
            break
        path.append(arg)
    words = tuple(path)
    moves = any(words[: len(p)] == p for p in _GMGN_MOVES_FUNDS)
    reads = any(words[: len(p)] == p for p in _GMGN_READS)
    if not moves and not reads:
        return None  # needs no key; runs as an ordinary command
    node, index_js = _resolve_gmgn()
    return SigningInvocation(
        skill="gmgn",
        entry="gmgn-cli",
        argv=(node, index_js, *args),
        key_env="GMGN_PRIVATE_KEY",
        needs_approval=moves,
        action=(
            f"gmgn-cli {' '.join(words)}: signs with GMGN_PRIVATE_KEY and "
            + ("submits a real trade or order." if moves else "reads order state.")
        ),
    )


def _resolve_gmgn() -> tuple[str, str]:
    """``(node, dist/index.js)`` of the installed ``gmgn-cli``, if it is the pinned one."""
    search = build_subprocess_env().get("PATH")
    exe = shutil.which("gmgn-cli", path=search)
    node = shutil.which("node", path=search)
    if exe is None or node is None:
        raise SigningRefusedError(
            "gmgn-cli (and node) must be installed for this command: "
            f"npm install -g --ignore-scripts gmgn-cli@{GMGN_CLI_VERSION}"
        )
    real = Path(os.path.realpath(exe))
    candidates = [
        real.parent.parent / "package.json",  # bin symlink -> <pkg>/dist/index.js
        Path(exe).parent / "node_modules" / "gmgn-cli" / "package.json",  # Windows shim
        Path(exe).parent.parent / "lib" / "node_modules" / "gmgn-cli" / "package.json",
    ]
    for manifest in candidates:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("name") != "gmgn-cli":
            continue
        version = str(data.get("version") or "")
        if version != GMGN_CLI_VERSION:
            raise SigningRefusedError(
                f"gmgn-cli {version or '(unknown version)'} is installed; AgentOS only hands "
                f"GMGN_PRIVATE_KEY to the reviewed {GMGN_CLI_VERSION}. Reinstall it: "
                f"npm install -g --ignore-scripts gmgn-cli@{GMGN_CLI_VERSION}"
            )
        index_js = manifest.parent / "dist" / "index.js"
        if index_js.is_file():
            return node, str(index_js)
    raise SigningRefusedError(f"could not locate the gmgn-cli package behind {exe}")


# --- the environment and the mandate store -----------------------------------


def signing_store_dir() -> Path:
    """Where operator-approved mandates live. On the sensitive-path floor."""
    from agentos.paths import default_agentos_home

    return default_agentos_home() / "skill-signing"


def _mandates_file() -> Path:
    return signing_store_dir() / "unilp-mandates.json"


def approved_mandates() -> dict[str, str]:
    try:
        data = json.loads(_mandates_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if _MANDATE_ID_RE.match(str(k))}


def record_mandate(ident: str, digest: str) -> None:
    mandates = approved_mandates()
    mandates[ident] = digest
    target = _mandates_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}")
    tmp.write_text(json.dumps(mandates, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target)


def mandate_digest(mandate: Mapping[str, Any]) -> str:
    """What ratchet.py prints in its arm receipt and re-derives on every tick."""
    body = {"immutable": mandate.get("immutable"), "bounds": mandate.get("bounds")}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def invocation_env(invocation: SigningInvocation, base: Mapping[str, str]) -> dict[str, str]:
    """``base`` (already stripped of every signing key) plus this invocation's key."""
    env = dict(base)
    value = os.environ.get(invocation.key_env)
    if value:
        env[invocation.key_env] = value
    env.pop(KEYS_HELD_ENV, None)
    env[GATED_ENV] = "1"
    if invocation.entry == "ratchet.py":
        # Always present for a gated ratchet run, so a tick can never fall back
        # to "every mandate on disk".
        env[OPERATOR_MANDATES_ENV] = json.dumps(approved_mandates(), sort_keys=True)
    if invocation.mandate_to_record:
        store = signing_store_dir()
        store.mkdir(parents=True, exist_ok=True)
        env[ARM_RECEIPT_ENV] = str(store / f".arm-{secrets.token_hex(16)}.json")
    env.update(invocation.extra_env)
    return env


def finish_invocation(
    invocation: SigningInvocation, env: Mapping[str, str], returncode: int
) -> None:
    """After the process exits: record an approved arm from its receipt."""
    receipt_path = env.get(ARM_RECEIPT_ENV)
    if not receipt_path or not invocation.mandate_to_record:
        return
    receipt = Path(receipt_path)
    try:
        data = json.loads(receipt.read_text(encoding="utf-8")) if returncode == 0 else None
    except (OSError, ValueError):
        data = None
    finally:
        try:
            receipt.unlink()
        except OSError:
            pass
    if not isinstance(data, dict) or data.get("id") != invocation.mandate_to_record:
        return
    digest = data.get("digest")
    if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
        record_mandate(invocation.mandate_to_record, digest)


#: A cron script longer than this is not a one-line wrapper.
_MAX_SCRIPT_BYTES = 8192


def script_signing_invocation(path: Path) -> SigningInvocation | None:
    """The invocation a cron shell script consists of, if that is all it is.

    The ratchet's documented ``tick.sh`` is a shebang plus one ``exec python3
    .../ratchet.py tick ...`` line. Run as a file, its key would have to be in
    the script's environment -- where any script the agent writes would get
    it too. So a script that is exactly one recognised invocation (comments,
    blank lines and ``\\`` continuations aside) is run through this module
    instead, and anything else runs as written, without a key.
    """
    if path.suffix.lower() not in (".sh", ".bash"):
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > _MAX_SCRIPT_BYTES:
        return None
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\\\n", " ")
    lines = [
        line.strip()
        for line in text.split("\n")
        if line.strip() and not line.strip().startswith("#")
    ]
    if len(lines) != 1:
        return None
    return classify_command(lines[0], str(path.parent))
