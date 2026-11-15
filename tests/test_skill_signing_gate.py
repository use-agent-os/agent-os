"""A skill's hot-wallet key moves funds only after the operator approves (WAL-10, WAL-04).

``senior-unilp-manager`` and ``poolsdotfun-token-launcher`` signed with a key
in the agent's own environment, gated only by a ``PLAN_HASH`` the same dry run
prints: the agent ran the command twice and the broadcast went out, to any
``--recipient`` it chose. The GMGN skills promised a code-level confirmation
the installed ``gmgn-cli`` (unpinned, 1.7.1) does not have.

Pinned here:

* the keys never reach a child the agent drives -- exec, ``execute_code``,
  cron scripts, MCP servers, coding-agent harnesses, lifecycle hooks -- nor a
  dotenv fallback, nor ``env.reveal`` from an agent connection;
* a direct invocation of a skill entry point gets its key, and one that moves
  funds only after an operator approval that ``bypass``, ``/elevated full``,
  auto-approve and an unattended run cannot supply;
* the ratchet's unattended tick fires only mandates the operator approved,
  with the bounds approved;
* gmgn-cli is pinned, and the key is refused to any other installed version.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.gateway.approval_queue import get_approval_queue, reset_approval_queue
from agentos.sandbox.integration import reset_runtime
from agentos.sandbox.intent_cache import reset_intent_cache
from agentos.tools import env_passthrough, skill_signing
from agentos.tools.builtin import shell
from agentos.tools.skill_signing import (
    GMGN_CLI_VERSION,
    SigningRefusedError,
    classify_command,
)
from agentos.tools.types import (
    CallerKind,
    InteractionMode,
    ToolContext,
    ToolError,
    UnsupportedSurfaceError,
    current_tool_context,
)

_REPO = Path(__file__).resolve().parents[1]
_BUNDLED = _REPO / "src" / "agentos" / "skills" / "bundled"
_UNILP_SCRIPTS = _BUNDLED / "senior-unilp-manager" / "scripts"

# Dummy, never a real key.
KEY = "0x" + "5" * 64
KEY_HASH = hashlib.sha256(KEY.encode()).hexdigest()
ATTACKER = "0x00000000000000000000000000000000000000ee"
MANDATE = "0123456789abcdef0123456789abcdef"


def _exec() -> Any:
    fn: Any = shell.exec_command
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


@pytest.fixture(autouse=True)
def _state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("UNIV4_LP_PRIVATE_KEY", KEY)
    reset_approval_queue()
    reset_intent_cache()
    reset_runtime()
    monkeypatch.setattr(shell, "_sandbox_effectively_off", lambda: True)
    elevate = shell._elevate_current_call.set(False)
    # The documented default: permissions in bypass mode.
    ctx = ToolContext(caller_kind=CallerKind.CLI, session_key="agent:main:test", elevated="bypass")
    token = current_tool_context.set(ctx)
    yield ctx
    current_tool_context.reset(token)
    shell._elevate_current_call.reset(elevate)
    reset_approval_queue()
    reset_intent_cache()


@pytest.fixture
def fake_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The real ``unilp`` helpers plus stand-in entry points that report their env.

    The real scripts would need a chain; these print what AgentOS handed them.
    """
    root = tmp_path / "bundled"
    scripts = root / "senior-unilp-manager" / "scripts"
    shutil.copytree(_UNILP_SCRIPTS / "unilp", scripts / "unilp")
    probe = (
        "import hashlib, json, os, sys\n"
        "env = os.environ\n"
        "key = env.get('UNIV4_LP_PRIVATE_KEY')\n"
        "out = {'key': key and hashlib.sha256(key.encode()).hexdigest(), 'gated': env.get('"
        + skill_signing.GATED_ENV
        + "'), 'mandates': env.get('UNILP_OPERATOR_MANDATES'), 'argv': sys.argv[1:],\n"
        "       'isolated': sys.flags.isolated, 'cwd': os.getcwd()}\n"
        "receipt = env.get('UNILP_ARM_RECEIPT')\n"
        "if receipt and '--confirm' in sys.argv:\n"
        "    ident = sys.argv[sys.argv.index('--confirm') + 1]\n"
        "    open(receipt, 'w').write(json.dumps({'id': ident, 'digest': 'a' * 64}))\n"
        "print(json.dumps(out))\n"
        "print('leak check', env.get('UNIV4_LP_PRIVATE_KEY'))\n"
    )
    for name in ("lp_write.py", "lp_read.py", "ratchet.py"):
        (scripts / name).write_text(probe, encoding="utf-8")
    monkeypatch.setattr(skill_signing, "_BUNDLED", root)
    monkeypatch.setattr(skill_signing, "_FMT_CACHE", {})
    return scripts


def _report(output: str) -> dict[str, Any]:
    line = next(line for line in output.splitlines() if line.startswith("{"))
    return json.loads(line)


def _cmd(script: str, *args: str, scripts: Path = _UNILP_SCRIPTS) -> str:
    return f'S="{scripts}"; "{sys.executable}" "$S/{script}" ' + " ".join(args)


# --- the PoC: dry run, scrape the hash, re-run with it ----------------------


async def test_a_self_confirmed_broadcast_waits_for_the_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audit run: ``burn --recipient <attacker> --broadcast --confirm <hash>``.

    On main the command ran in the shell with the key in its environment and
    signed. Now it is parked for the operator and nothing is spawned.
    """

    async def no_spawn(*_a: object, **_k: object) -> object:
        raise AssertionError("nothing may run before the operator approves")

    monkeypatch.setattr(shell.asyncio, "create_subprocess_exec", no_spawn)
    monkeypatch.setattr(shell.asyncio, "create_subprocess_shell", no_spawn)
    command = _cmd(
        "lp_write.py",
        "burn --token-id 7 --chain base",
        f"--recipient {ATTACKER} --broadcast --confirm c7e5ac80 --rpc http://127.0.0.1:9",
    )
    result = json.loads(await _exec()(command=command))
    assert result["status"] == "approval_required"
    pending = get_approval_queue().get(result["approval_id"])
    assert pending.params["command"] == command
    assert ATTACKER in pending.params["warning"]


@pytest.mark.parametrize("mode", ["bypass", "full", "on"])
async def test_no_elevated_mode_answers_it(_state: ToolContext, mode: str) -> None:
    _state.elevated = mode
    command = _cmd("lp_write.py", "collect --token-id 1 --broadcast --confirm aa")
    result = json.loads(await _exec()(command=command))
    assert result["status"] == "approval_required"


async def test_auto_approve_does_not_answer_it(_state: ToolContext) -> None:
    get_approval_queue().set_settings("auto-approve")
    _state.elevated = None
    command = _cmd("lp_write.py", "collect --token-id 1 --broadcast --confirm aa")
    assert json.loads(await _exec()(command=command))["status"] == "approval_required"


async def test_elevating_the_session_does_not_release_a_signing_prompt() -> None:
    """ "Stop Asking" on some other prompt approves the session's pending exec rows."""
    command = _cmd("lp_write.py", "collect --token-id 1 --broadcast --confirm aa")
    approval_id = json.loads(await _exec()(command=command))["approval_id"]
    queue = get_approval_queue()
    queue.resolve_pending_for_session("agent:main:test", approved=True, elevated_mode="bypass")
    assert not queue.get(approval_id).resolved


async def test_an_unattended_run_cannot_broadcast(_state: ToolContext) -> None:
    _state.interaction_mode = InteractionMode.UNATTENDED
    command = _cmd("lp_write.py", "collect --token-id 1 --broadcast --confirm aa")
    with pytest.raises(UnsupportedSurfaceError):
        await _exec()(command=command)


async def test_after_approval_the_packaged_script_runs_with_the_key(fake_bundle: Path) -> None:
    elsewhere = Path("/somewhere/else/senior-unilp-manager/scripts")
    command = _cmd("lp_write.py", "burn --token-id 7 --broadcast --confirm aa", scripts=elsewhere)
    pending = json.loads(await _exec()(command=command))
    approval_id = pending["approval_id"]
    get_approval_queue().resolve(approval_id, approved=True)

    out = await _exec()(command=command, approval_id=approval_id)
    report = _report(out)
    assert report["key"] == KEY_HASH
    assert report["gated"] == "1"
    assert report["isolated"] == 1
    assert report["argv"] == ["burn", "--token-id", "7", "--broadcast", "--confirm", "aa"]
    # Even a script that prints the key does not get it into the transcript.
    assert "leak check [REDACTED]" in out
    assert KEY[2:] not in out

    # Single use: the same approval does not run it twice.
    with pytest.raises(ToolError, match="consumed"):
        await _exec()(command=command, approval_id=approval_id)


async def test_a_denied_approval_never_runs(fake_bundle: Path) -> None:
    command = _cmd("lp_write.py", "burn --token-id 7 --broadcast --confirm aa")
    approval_id = json.loads(await _exec()(command=command))["approval_id"]
    get_approval_queue().resolve(approval_id, approved=False)
    result = json.loads(await _exec()(command=command, approval_id=approval_id))
    assert result["status"] == "approval_denied"


async def test_a_dry_run_gets_the_key_without_a_prompt(fake_bundle: Path) -> None:
    out = await _exec()(command=_cmd("lp_write.py", "burn --token-id 7"))
    assert _report(out)["key"] == KEY_HASH
    assert get_approval_queue().list_pending("exec") == []


async def test_env_overrides_are_refused_on_a_signing_command(fake_bundle: Path) -> None:
    out = await _exec()(command=_cmd("lp_read.py", "positions"), env={"PYTHONPATH": "/tmp"})
    assert json.loads(out)["status"] == "signing_refused"


@pytest.mark.skipif(os.name == "nt", reason="the shell form below is POSIX sh")
async def test_any_other_shape_runs_without_the_key(fake_bundle: Path) -> None:
    """A pipe (or any shape the gate does not accept) is an ordinary command: no key."""
    command = _cmd("lp_write.py", "burn --token-id 7", scripts=fake_bundle) + " | cat"
    out = await _exec()(command=command)
    assert not _report(out)["key"]
    assert get_approval_queue().list_pending("exec") == []


# --- the classifier ----------------------------------------------------------


@pytest.mark.parametrize(
    "tail",
    [
        "| tee log",
        "> out.txt",
        "; env",
        "&& cat ~/.agentos/.env",
        "--note $(cat ~/.agentos/.env)",
        "--note `id`",
        "--note *",
        "&",
    ],
)
def test_compound_or_expanding_commands_are_not_gated(tail: str) -> None:
    command = _cmd("lp_write.py", "burn --token-id 7 --broadcast --confirm aa", tail)
    assert classify_command(command, None) is None


def test_env_prefix_and_interpreter_flags_are_not_gated() -> None:
    script = _UNILP_SCRIPTS / "lp_write.py"
    assert classify_command(f'PYTHONPATH=/tmp python3 "{script}" burn', None) is None
    assert classify_command(f'python3 -c "x" "{script}" burn', None) is None
    assert classify_command(f'bash "{script}" burn', None) is None


def test_bidi_or_control_characters_are_not_gated() -> None:
    script = _UNILP_SCRIPTS / "lp_write.py"
    command = f'python3 "{script}" burn --recipient "0xab‮cd" --broadcast --confirm aa'
    assert classify_command(command, None) is None


def test_the_documented_shapes_are_gated() -> None:
    lp_write = classify_command(
        _cmd("lp_write.py", "mint --pool 0x1 --broadcast --confirm aa 2>&1"), None
    )
    assert lp_write is not None and lp_write.needs_approval
    assert lp_write.argv[1:4] == ("-I", "-X", "utf8")
    assert Path(lp_write.argv[4]) == _UNILP_SCRIPTS / "lp_write.py"
    tilde = classify_command(
        "exec python3 ~/.agentos/skills/senior-unilp-manager/scripts/ratchet.py "
        "tick --all --broadcast --json --alert-only",
        None,
    )
    assert tilde is not None and tilde.unattended_fire and not tilde.needs_approval
    assert Path(tilde.cwd or "") == _UNILP_SCRIPTS


def test_a_flag_that_is_turned_back_off_is_read_like_the_script_reads_it() -> None:
    invocation = classify_command(
        _cmd("lp_write.py", "burn --broadcast --broadcast= --confirm aa"), None
    )
    assert invocation is not None and not invocation.needs_approval


@pytest.mark.parametrize(
    ("args", "approval", "unattended"),
    [
        ("tick --all --broadcast --json --alert-only", False, True),
        ("tick --id " + MANDATE + " --broadcast --chain base", False, True),
        ("tick --all --broadcast --rpc http://evil.example", True, False),
        ("tick --all --broadcast --deadline-secs 99999", True, False),
        ("tick --all --json", False, False),
        (f"arm --token-id 5 --confirm {MANDATE}", True, False),
        ("arm --token-id 5", False, False),
        (f"clear-attention --id {MANDATE}", True, False),
        (f"disarm --id {MANDATE}", False, False),
        ("list", False, False),
    ],
)
def test_ratchet_commands(args: str, approval: bool, unattended: bool) -> None:
    invocation = classify_command(_cmd("ratchet.py", args), None)
    assert invocation is not None
    assert (invocation.needs_approval, invocation.unattended_fire) == (approval, unattended)


def test_pools_write_broadcast_needs_approval() -> None:
    scripts = _BUNDLED / "poolsdotfun-token-launcher" / "scripts"
    invocation = classify_command(
        f'S="{scripts}"; python3 "$S/pools_write.py" set-fee-recipient --token 0x1 '
        f"--recipient {ATTACKER} --broadcast --confirm 0x4f4aba59",
        None,
    )
    assert invocation is not None and invocation.needs_approval
    assert invocation.key_env == "POOLSFUN_PRIVATE_KEY"
    assert ATTACKER in invocation.action


# --- the ratchet mandate binding --------------------------------------------


async def test_an_approved_arm_is_recorded_and_handed_to_the_tick(fake_bundle: Path) -> None:
    arm = _cmd("ratchet.py", f"arm --token-id 5 --confirm {MANDATE}")
    approval_id = json.loads(await _exec()(command=arm))["approval_id"]
    get_approval_queue().resolve(approval_id, approved=True)
    await _exec()(command=arm, approval_id=approval_id)
    assert skill_signing.approved_mandates() == {MANDATE: "a" * 64}

    tick = await _exec()(command=_cmd("ratchet.py", "tick --all --broadcast --json --alert-only"))
    report = _report(tick)
    assert json.loads(report["mandates"]) == {MANDATE: "a" * 64}
    assert report["key"] == KEY_HASH
    assert Path(report["cwd"]) == fake_bundle


def _ratchet() -> Any:
    sys.path.insert(0, str(_UNILP_SCRIPTS))
    try:
        import ratchet  # type: ignore[import-not-found]
    finally:
        sys.path.remove(str(_UNILP_SCRIPTS))
    return ratchet


@pytest.mark.skipif(os.name == "nt", reason="ratchet's journal needs fcntl")
def test_a_gated_tick_refuses_unapproved_or_edited_mandates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ratchet = _ratchet()
    mandate = {"immutable": {"poolId": "0x1"}, "bounds": {"maxSlippageBps": 50}}
    digest = ratchet.mandate_binding_digest(mandate)
    assert digest == skill_signing.mandate_digest(mandate)

    monkeypatch.delenv("UNILP_OPERATOR_MANDATES", raising=False)
    assert ratchet.operator_refusal(MANDATE, mandate) is None  # standalone use

    monkeypatch.setenv("UNILP_OPERATOR_MANDATES", "{}")
    assert "not armed through an operator approval" in ratchet.operator_refusal(MANDATE, mandate)

    monkeypatch.setenv("UNILP_OPERATOR_MANDATES", json.dumps({MANDATE: digest}))
    assert ratchet.operator_refusal(MANDATE, mandate) is None

    edited = {**mandate, "bounds": {"maxSlippageBps": 10_000}}
    assert "bounds differ" in ratchet.operator_refusal(MANDATE, edited)


# --- cron script jobs ---------------------------------------------------------


async def test_a_cron_tick_script_runs_gated(
    fake_bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentos.scheduler.scripts import run_job_script

    script_dir = tmp_path / "state" / "scripts" / "senior-unilp-manager" / "job1"
    script_dir.mkdir(parents=True)
    (script_dir / "tick.sh").write_text(
        "#!/bin/sh\n# take-profit\nexec python3 "
        f"{fake_bundle}/ratchet.py \\\n  tick --all --broadcast --json --alert-only\n",
        encoding="utf-8",
    )
    ok, output = await run_job_script("senior-unilp-manager/job1/tick.sh", timeout=60)
    assert ok, output
    assert _report(output)["key"] == KEY_HASH

    (script_dir / "arm.sh").write_text(
        f"exec python3 {fake_bundle}/ratchet.py arm --token-id 5 --confirm {MANDATE}\n",
        encoding="utf-8",
    )
    ok, output = await run_job_script("senior-unilp-manager/job1/arm.sh", timeout=60)
    assert not ok and "approval" in output


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
async def test_any_other_cron_script_gets_no_key(fake_bundle: Path, tmp_path: Path) -> None:
    from agentos.scheduler.scripts import run_job_script

    script_dir = tmp_path / "state" / "scripts" / "x"
    script_dir.mkdir(parents=True)
    (script_dir / "tick.sh").write_text(
        f"echo hi\nexec python3 {fake_bundle}/ratchet.py tick --all --broadcast\n",
        encoding="utf-8",
    )
    ok, output = await run_job_script("x/tick.sh", timeout=60)
    assert ok, output
    assert not _report(output)["key"]


# --- custody: every other way to reach the key ------------------------------


def test_no_child_environment_carries_a_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentos.acp.process import scrub_env
    from agentos.gateway import lifecycle_hooks

    base = {
        "PATH": "/usr/bin",
        "UNIV4_LP_PRIVATE_KEY": KEY,
        "POOLSFUN_PRIVATE_KEY": KEY,
        "GMGN_PRIVATE_KEY": "pem",
    }
    env = env_passthrough.build_subprocess_env(base, {"POOLSFUN_PRIVATE_KEY": KEY})
    assert not set(env) & {"UNIV4_LP_PRIVATE_KEY", "POOLSFUN_PRIVATE_KEY", "GMGN_PRIVATE_KEY"}
    for name, value in base.items():
        monkeypatch.setenv(name, value)
    env = env_passthrough.build_subprocess_env(extra={"POOLSFUN_PRIVATE_KEY": KEY})
    assert "UNIV4_LP_PRIVATE_KEY" not in env and "POOLSFUN_PRIVATE_KEY" not in env
    # Present-but-empty, so gmgn-cli's own dotenv cannot fill it in.
    assert env["GMGN_PRIVATE_KEY"] == ""
    assert env[env_passthrough.SIGNING_KEYS_HELD_ENV] == "1"

    harness = scrub_env(base, keep={"UNIV4_LP_PRIVATE_KEY"})
    assert not set(harness) & {"UNIV4_LP_PRIVATE_KEY", "POOLSFUN_PRIVATE_KEY", "GMGN_PRIVATE_KEY"}

    event = SimpleNamespace(name="gateway_start", reason="")
    hook_env = lifecycle_hooks._command_env(event)  # type: ignore[arg-type]
    assert not set(hook_env) & {"UNIV4_LP_PRIVATE_KEY", "POOLSFUN_PRIVATE_KEY", "GMGN_PRIVATE_KEY"}


async def test_an_mcp_server_does_not_inherit_a_signing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.mcp import stdio
    from agentos.mcp.types import MCPServerConfig

    seen: dict[str, Any] = {}

    async def spawn(*_argv: object, env: dict[str, str] | None = None, **_k: object) -> object:
        seen["env"] = env
        raise RuntimeError("stop here")

    monkeypatch.setattr(stdio.asyncio, "create_subprocess_exec", spawn)
    client = stdio.MCPStdioClient(MCPServerConfig(name="x", transport="stdio", command="x"))
    with pytest.raises(RuntimeError, match="stop here"):
        await client.connect()
    assert seen["env"] is not None and "UNIV4_LP_PRIVATE_KEY" not in seen["env"]


def test_no_skill_may_tunnel_a_signing_key_into_execute_code() -> None:
    env_passthrough.clear_env_passthrough(all_sessions=True)
    refused = env_passthrough.register_env_passthrough(["UNIV4_LP_PRIVATE_KEY"], trusted=True)
    assert refused == ["UNIV4_LP_PRIVATE_KEY"]
    assert not env_passthrough.is_env_passthrough("UNIV4_LP_PRIVATE_KEY")


def test_the_skill_never_reads_its_key_back_from_a_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bypass of the env strip: ``load_env`` used to read ``~/.agentos/.env``."""
    home = tmp_path / "home"
    (home / ".agentos").mkdir(parents=True)
    (home / ".agentos" / ".env").write_text(
        f"UNIV4_LP_PRIVATE_KEY={KEY}\nRPC_BASE_URL=http://127.0.0.1:9\n", encoding="utf-8"
    )
    monkeypatch.delenv("UNIV4_LP_PRIVATE_KEY")
    env = env_passthrough.build_subprocess_env()
    env["HOME"] = env["USERPROFILE"] = str(home)
    env.pop("UNILP_ENV", None)
    env.pop("AGENTOS_HOME", None)
    import subprocess

    result = subprocess.run(
        [sys.executable, str(_UNILP_SCRIPTS / "lp_write.py"), "address"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(tmp_path),
        timeout=60,
    )
    assert result.returncode != 0
    assert "held back from the agent's shell" in result.stderr
    assert KEY[2:18] not in result.stdout + result.stderr


async def test_an_agent_connection_cannot_reveal_a_signing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.gateway import rpc_approvals, rpc_env
    from agentos.gateway.rpc import RpcHandlerError

    class _Binding:
        def to_dict(self) -> dict[str, str]:
            return {"kind": "agent"}

    monkeypatch.setattr(rpc_approvals, "recognized_agent", lambda _ctx: _Binding())
    rpc_env._reset_reveal_budget()
    with pytest.raises(RpcHandlerError) as caught:
        await rpc_env._handle_env_reveal({"name": "UNIV4_LP_PRIVATE_KEY"}, object())  # type: ignore[arg-type]
    assert caught.value.code == "approvals.operator_required"


def test_the_approval_rows_and_the_mandate_store_are_sensitive(tmp_path: Path) -> None:
    from agentos.paths import default_agentos_home
    from agentos.sandbox.sensitive_paths import is_sensitive_path

    home = default_agentos_home()
    for path in (
        home / "state" / "approval_queue.sqlite",
        home / "state" / "approval_queue.sqlite-wal",
        home / "skill-signing" / "unilp-mandates.json",
        Path.home() / ".config" / "gmgn" / ".env",
        Path.home() / ".config" / "gmgn" / "keypair.pem",
    ):
        assert is_sensitive_path(str(path)) is not None, path


# --- gmgn ---------------------------------------------------------------------


def _fake_gmgn(tmp_path: Path, version: str, monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = tmp_path / "npm"
    pkg = prefix / "node_modules" / "gmgn-cli"
    (pkg / "dist").mkdir(parents=True)
    (pkg / "dist" / "index.js").write_text("// fake\n", encoding="utf-8")
    (pkg / "package.json").write_text(
        json.dumps({"name": "gmgn-cli", "version": version}), encoding="utf-8"
    )
    suffix = ".cmd" if os.name == "nt" else ""
    for name in ("gmgn-cli", "node"):
        exe = prefix / f"{name}{suffix}"
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(prefix))
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".CMD;.EXE")


@pytest.mark.parametrize(
    ("command", "approval"),
    [
        ("gmgn-cli swap --chain sol --from w --input-token a --output-token b --amount 1", True),
        ("gmgn-cli multi-swap --chain sol --wallets a,b", True),
        ("gmgn-cli order strategy create --chain sol --order-type limit_order", True),
        ("gmgn-cli order strategy cancel --chain sol --order-id 1", True),
        ("gmgn-cli cooking create --chain sol --name X", True),
        ("gmgn-cli order quote --chain sol", False),
        ("gmgn-cli order strategy list --chain sol", False),
    ],
)
def test_gmgn_commands_that_need_the_key(
    command: str, approval: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gmgn(tmp_path, GMGN_CLI_VERSION, monkeypatch)
    invocation = classify_command(command, None)
    assert invocation is not None
    assert invocation.needs_approval is approval
    assert invocation.key_env == "GMGN_PRIVATE_KEY"
    assert Path(invocation.argv[1]).name == "index.js"


def test_gmgn_reads_without_the_key_are_ordinary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gmgn(tmp_path, GMGN_CLI_VERSION, monkeypatch)
    assert classify_command("gmgn-cli market trending --chain sol", None) is None
    assert classify_command("gmgn-cli swap --chain sol | tee x", None) is None


def test_gmgn_key_is_refused_to_an_unreviewed_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gmgn(tmp_path, "1.8.0", monkeypatch)
    with pytest.raises(SigningRefusedError, match=GMGN_CLI_VERSION):
        classify_command("gmgn-cli swap --chain sol --amount 1", None)


def test_every_gmgn_skill_installs_the_pinned_cli_and_claims_no_cli_gate() -> None:
    for skill_md in sorted(_BUNDLED.glob("gmgn-*/SKILL.md")):
        text = skill_md.read_text(encoding="utf-8")
        if "kind: npm" in text:
            assert f"package: gmgn-cli@{GMGN_CLI_VERSION}" in text, skill_md
        assert "npm install -g gmgn-cli`" not in text, skill_md
        # The 1.5.x-era claim the audit found unbacked (WAL-04).
        assert "GMGN_ALLOW_AUTOMATED_TRADES" not in text, skill_md
        assert "cannot be bypassed by the agent" not in text, skill_md
