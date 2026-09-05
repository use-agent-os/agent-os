"""Code execution tool — sandboxed Python execution via subprocess."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from agentos.sandbox.integration import (
    escalate_backend_denial,
    gate_action,
    get_runtime,
    run_under_backend,
)
from agentos.sandbox.types import DenialResult, SandboxRequest
from agentos.tools.registry import tool
from agentos.tools.types import ToolError, current_tool_context

# Destructive Python patterns that must go through the same approval flow as
# shell warnlist hits. Catches the "agent pivots from `rm` to `os.remove()`"
# bypass. We scan using shallow regex (fast-path) plus AST analysis to catch
# dynamic evasion (getattr, __import__, importlib, exec/eval, and wildcard imports).
_DESTRUCTIVE_PY_PATTERNS: list[tuple[str, str]] = [
    (r"\bos\.remove\s*\(", "os.remove()"),
    (r"\bos\.unlink\s*\(", "os.unlink()"),
    (r"\bos\.rmdir\s*\(", "os.rmdir()"),
    (r"\bos\.removedirs\s*\(", "os.removedirs()"),
    (r"\bshutil\.rmtree\s*\(", "shutil.rmtree()"),
    (r"\.unlink\s*\(", "Path.unlink()"),
    (r"\.rmdir\s*\(", "Path.rmdir()"),
    (r"\bos\.system\s*\([^)]*\brm\b", "os.system with rm"),
    (
        r"\bsubprocess\.(run|call|Popen|check_output|check_call)[^\n;]{0,200}\brm\b",
        "subprocess invoking rm",
    ),
    (
        r"\bsubprocess\.(run|call|Popen|check_output|check_call)[^\n;]{0,200}\brmdir\b",
        "subprocess invoking rmdir",
    ),
]

_OS_DESTRUCTIVE_ATTRS: frozenset[str] = frozenset({"remove", "unlink", "rmdir", "removedirs"})
_SHUTIL_DESTRUCTIVE_ATTRS: frozenset[str] = frozenset({"rmtree", "rmdir"})
_PATH_DESTRUCTIVE_ATTRS: frozenset[str] = frozenset({"unlink", "rmdir"})
_ALL_DESTRUCTIVE_NAMES: frozenset[str] = frozenset(
    {"remove", "unlink", "rmdir", "removedirs", "rmtree"}
)
_SUBPROCESS_CALL_NAMES: frozenset[str] = frozenset(
    {"run", "call", "Popen", "check_output", "check_call"}
)


def _eval_const_str(node: ast.AST, compile_aliases: frozenset[str] | None = None) -> str | None:
    """Evaluate a statically resolvable string expression (literals, concat, f-strings).

    ``compile(...)`` calls resolve to their source argument: ``compile`` is a
    code *carrier*, so ``exec(compile("os.remove('/etc')", "", "exec"))`` must
    hand the same inner source to the destructive scan that
    ``exec("os.remove('/etc')")`` does. *compile_aliases* carries any local name
    bound to the builtin (``c = compile``) and always includes ``compile``.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.FormattedValue):
        return _eval_const_str(node.value, compile_aliases)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _eval_const_str(node.left, compile_aliases)
        right = _eval_const_str(node.right, compile_aliases)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for val in node.values:
            part = _eval_const_str(val, compile_aliases)
            if part is None:
                return None
            parts.append(part)
        return "".join(parts)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        names = compile_aliases if compile_aliases is not None else frozenset({"compile"})
        if node.func.id in names:
            if node.args:
                return _eval_const_str(node.args[0], compile_aliases)
            for keyword in node.keywords:
                if keyword.arg == "source":
                    return _eval_const_str(keyword.value, compile_aliases)
    return None


def _resolve_module_from_node(node: ast.AST, aliases: dict[str, str]) -> str | None:
    """Return the canonical module name ('os', 'shutil', etc.) if node resolves to one."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Call):
        # __import__("os")
        if isinstance(node.func, ast.Name) and node.func.id == "__import__" and node.args:
            mod = _eval_const_str(node.args[0])
            if mod:
                return aliases.get(mod, mod)
        if isinstance(node.func, ast.Attribute):
            # importlib.import_module("os")
            if node.func.attr == "import_module":
                mod_val = _resolve_module_from_node(node.func.value, aliases)
                if mod_val == "importlib" and node.args:
                    mod = _eval_const_str(node.args[0])
                    if mod:
                        return aliases.get(mod, mod)
            # builtins.__import__("os") — same import, spelled through the module.
            if node.func.attr == "__import__" and node.args:
                mod = _eval_const_str(node.args[0])
                if mod:
                    return aliases.get(mod, mod)
        # getattr(builtins, "__import__")("os") — the importer itself fetched
        # dynamically, so the callee is a Call rather than a Name/Attribute.
        if isinstance(node.func, ast.Call) and node.args:
            _target_mod, target_attr = _resolve_getattr_target(node.func, aliases)
            if target_attr == "__import__":
                mod = _eval_const_str(node.args[0])
                if mod:
                    return aliases.get(mod, mod)
    return None


def _resolve_getattr_target(
    node: ast.AST, aliases: dict[str, str]
) -> tuple[str | None, str | None]:
    """Return ``(module, attr)`` when *node* is a ``getattr(<module>, "attr")`` call.

    Both halves are resolved statically, so ``getattr(__import__("os"), "sys" +
    "tem")`` reports ``("os", "system")``. ``(None, None)`` means *node* is not a
    statically resolvable ``getattr`` call.
    """
    if not isinstance(node, ast.Call):
        return None, None
    if not (isinstance(node.func, ast.Name) and node.func.id == "getattr"):
        return None, None
    if len(node.args) < 2:
        return None, None
    return _resolve_module_from_node(node.args[0], aliases), _eval_const_str(node.args[1])


class _DestructiveCodeVisitor(ast.NodeVisitor):
    """AST visitor detecting obfuscated and dynamic destructive operations."""

    def __init__(self) -> None:
        self.warning: str | None = None
        self.module_aliases: dict[str, str] = {
            "os": "os",
            "shutil": "shutil",
            "pathlib": "pathlib",
            "subprocess": "subprocess",
            "importlib": "importlib",
        }
        self.destructive_funcs: dict[str, str] = {}
        #: Local names bound to the ``compile`` builtin (``c = compile``), so a
        #: renamed carrier resolves the same as the builtin spelling.
        self.compile_aliases: set[str] = {"compile"}

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Name) and node.value.id in self.compile_aliases:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.compile_aliases.add(target.id)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            target = alias.asname or alias.name
            if alias.name in ("os", "shutil", "pathlib", "subprocess", "importlib"):
                self.module_aliases[target] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        canonical = self.module_aliases.get(mod, mod)
        if canonical == "os":
            for alias in node.names:
                name = alias.name
                target = alias.asname or name
                if name == "*":
                    for fn in _OS_DESTRUCTIVE_ATTRS:
                        self.destructive_funcs[fn] = f"os.{fn}()"
                elif name in _OS_DESTRUCTIVE_ATTRS:
                    self.destructive_funcs[target] = f"os.{name}()"
        elif canonical == "shutil":
            for alias in node.names:
                name = alias.name
                target = alias.asname or name
                if name == "*":
                    for fn in _SHUTIL_DESTRUCTIVE_ATTRS:
                        self.destructive_funcs[fn] = f"shutil.{fn}()"
                elif name in _SHUTIL_DESTRUCTIVE_ATTRS:
                    self.destructive_funcs[target] = f"shutil.{name}()"
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self.warning is not None:
            return

        # 1. Direct function call: remove(), r(), etc.
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
            if func_name in self.destructive_funcs:
                self.warning = (
                    f"destructive Python operation detected: {self.destructive_funcs[func_name]}"
                )
                return

            # 2. Dynamic getattr: getattr(os, "rem"+"ove"), getattr(Path, "unlink"), etc.
            if func_name == "getattr" and len(node.args) >= 2:
                attr_name = _eval_const_str(node.args[1], frozenset(self.compile_aliases))
                if attr_name in _ALL_DESTRUCTIVE_NAMES:
                    mod = _resolve_module_from_node(node.args[0], self.module_aliases)
                    if mod == "os" and attr_name in _OS_DESTRUCTIVE_ATTRS:
                        self.warning = (
                            f"destructive Python operation detected: os.{attr_name}() via getattr"
                        )
                        return
                    if mod == "shutil" and attr_name in _SHUTIL_DESTRUCTIVE_ATTRS:
                        self.warning = (
                            f"destructive Python operation detected: "
                            f"shutil.{attr_name}() via getattr"
                        )
                        return
                    if attr_name in _PATH_DESTRUCTIVE_ATTRS:
                        self.warning = (
                            f"destructive Python operation detected: Path.{attr_name}() via getattr"
                        )
                        return
                    self.warning = (
                        f"destructive Python operation detected: {attr_name}() via getattr"
                    )
                    return

            # 3. eval() or exec() with embedded destructive code. `compile(...)`
            #    resolves to its source argument, so a compiled carrier is scanned
            #    exactly like a string literal.
            if func_name in ("eval", "exec") and node.args:
                inner_code = _eval_const_str(node.args[0], frozenset(self.compile_aliases))
                if inner_code:
                    inner_warning = _check_code_destructive(inner_code)
                    if inner_warning:
                        self.warning = (
                            f"destructive Python operation detected: "
                            f"{func_name}() with {inner_warning}"
                        )
                        return

        # 4. Shell-exec and destructive attrs reached through getattr:
        #    `getattr(os, "sys"+"tem")("rm -rf /")`. The callee is a Call, so the
        #    ast.Attribute branch below never sees it.
        target_mod, target_attr = _resolve_getattr_target(node.func, self.module_aliases)
        if target_attr is not None:
            reason = self._indirect_call_reason(target_mod, target_attr, node)
            if reason is not None:
                self.warning = reason
                return

        # 5. Method call on an attribute: obj.method()
        if isinstance(node.func, ast.Attribute):
            attr_name = node.func.attr
            mod = _resolve_module_from_node(node.func.value, self.module_aliases)

            if mod == "os" and attr_name in _OS_DESTRUCTIVE_ATTRS:
                self.warning = f"destructive Python operation detected: os.{attr_name}()"
                return
            if mod == "shutil" and attr_name in _SHUTIL_DESTRUCTIVE_ATTRS:
                self.warning = f"destructive Python operation detected: shutil.{attr_name}()"
                return
            if attr_name in _PATH_DESTRUCTIVE_ATTRS:
                self.warning = f"destructive Python operation detected: Path.{attr_name}()"
                return

            if mod == "os" and attr_name in ("system", "popen") and node.args:
                cmd_str = _eval_const_str(node.args[0], frozenset(self.compile_aliases))
                if cmd_str and re.search(r"\b(rm|rmdir)\b", cmd_str):
                    self.warning = f"destructive Python operation detected: os.{attr_name} with rm"
                    return

            if mod == "subprocess" and attr_name in _SUBPROCESS_CALL_NAMES and node.args:
                if self._subprocess_argv_removes(node.args[0]):
                    self.warning = "destructive Python operation detected: subprocess invoking rm"
                    return

        self.generic_visit(node)

    def _indirect_call_reason(self, module: str | None, attr: str, node: ast.Call) -> str | None:
        """Reason when a ``getattr``-resolved callee is a destructive operation."""
        if module == "os" and attr in _OS_DESTRUCTIVE_ATTRS:
            return f"destructive Python operation detected: os.{attr}() via getattr"
        if module == "shutil" and attr in _SHUTIL_DESTRUCTIVE_ATTRS:
            return f"destructive Python operation detected: shutil.{attr}() via getattr"
        if module == "os" and attr in ("system", "popen") and node.args:
            cmd_str = _eval_const_str(node.args[0], frozenset(self.compile_aliases))
            if cmd_str and re.search(r"\b(rm|rmdir)\b", cmd_str):
                return f"destructive Python operation detected: os.{attr} with rm via getattr"
        if module == "subprocess" and attr in _SUBPROCESS_CALL_NAMES and node.args:
            if self._subprocess_argv_removes(node.args[0]):
                return "destructive Python operation detected: subprocess invoking rm via getattr"
        return None

    def _subprocess_argv_removes(self, first_arg: ast.expr) -> bool:
        """True when a subprocess argv (list or string form) invokes rm/rmdir."""
        aliases = frozenset(self.compile_aliases)
        if isinstance(first_arg, ast.List):
            parts = [_eval_const_str(elt, aliases) for elt in first_arg.elts]
            return any(part in ("rm", "rmdir") for part in parts if part is not None)
        cmd_str = _eval_const_str(first_arg, aliases)
        return bool(cmd_str and re.search(r"\b(rm|rmdir)\b", cmd_str))


def _check_code_destructive(code: str) -> str | None:
    """Return a human-readable warning if *code* triggers a destructive pattern, else None."""
    for pattern, label in _DESTRUCTIVE_PY_PATTERNS:
        if re.search(pattern, code):
            return f"destructive Python operation detected: {label}"

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None

    visitor = _DestructiveCodeVisitor()
    visitor.visit(tree)
    return visitor.warning


_CODE_SENSITIVE_READ_TOKENS = (
    "open(",
    ".open(",
    ".read_text(",
    ".read_bytes(",
    "listdir(",
    "scandir(",
    "walk(",
    ".glob(",
    ".rglob(",
    "copyfile(",
    "copy2(",
    "copy(",
    "subprocess.",
    "os.system(",
    "os.popen(",
)
_CODE_NETWORK_TOKENS = (
    "httpx.",
    "requests.",
    "urllib.request",
    "http.client",
    "socket.",
    ".post(",
    ".put(",
    ".patch(",
)


def _iter_code_string_literals(code: str) -> list[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return re.findall(r"""["']([^"']{1,500})["']""", code)

    values: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            values.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
            if parts:
                values.append("".join(parts))
    return values


def _check_code_sensitive_access(code: str) -> tuple[str, str] | None:
    """Return (reason, marker) if Python code is trying to touch sensitive data."""
    lowered = code.lower()
    has_read_or_shell = any(token in lowered for token in _CODE_SENSITIVE_READ_TOKENS)

    ctx = current_tool_context.get()
    workspace = ctx.workspace_dir if ctx is not None else None

    from agentos.sandbox.sensitive_paths import sensitive_path_in_text, sensitive_path_marker

    for literal in _iter_code_string_literals(code):
        marker = sensitive_path_marker(literal, workspace=workspace) or sensitive_path_in_text(
            literal,
            workspace=workspace,
        )
        path_like_literal = literal.strip().startswith(("/", "~", "."))
        if marker is not None and (has_read_or_shell or path_like_literal):
            return "sensitive_path", marker

    from agentos.tools.builtin.web import _sensitive_body_marker, _sensitive_url_marker

    has_network = any(token in lowered for token in _CODE_NETWORK_TOKENS)
    if has_network:
        for literal in _iter_code_string_literals(code):
            marker = _sensitive_url_marker(literal)
            if marker is not None:
                return "sensitive_payload", marker
        marker = _sensitive_body_marker(code)
        if marker is not None:
            return "sensitive_payload", marker

    return None


_MAX_TIMEOUT = 120
_DEFAULT_TIMEOUT = 30
_MAX_OUTPUT_CHARS = 50_000
_SANDBOX_PYTHON_CANDIDATES: tuple[Path, ...] = (
    Path("/usr/bin/python3"),
    Path("/bin/python3"),
    Path("/usr/bin/python"),
    Path("/bin/python"),
)

# Only these env vars are forwarded to the sandbox subprocess.
# Secrets (API keys, tokens) are explicitly excluded.
_SAFE_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "USER",
        "SHELL",
        "TERM",
        "PYTHONPATH",
    }
)


def _build_safe_env() -> dict[str, str]:
    """Return the sandbox environment: the safe base plus skill declarations.

    The allowlist above is what any code can see. A skill that declares
    ``metadata.requires.env`` adds its own names on top for the session that
    loaded it — that is the supported way for a skill to reach a third-party
    API from sandboxed code, and it is why the guard on the way out no longer
    has to guess whether a payload is a credential. A skill AgentOS did not
    ship is refused AgentOS's own credentials at registration, so this cannot
    widen past them.
    """
    from agentos.tools.env_passthrough import is_env_passthrough

    return {
        key: value
        for key, value in os.environ.items()
        if key in _SAFE_ENV_KEYS or is_env_passthrough(key)
    }


def _execution_result_json(
    *,
    returncode: int,
    stdout: str,
    stderr: str,
    timed_out: bool,
    elapsed_ms: int,
) -> str:
    # Redact secrets from captured output before it reaches the model.
    # shell.py does this on every output surface; execute_code must match the
    # same egress policy (see redact.redact_terminal_output). A script that
    # prints os.environ / a credential file would otherwise leak it raw.
    #
    # Redact BEFORE truncating: a credential straddling the output cap would
    # otherwise be cut in half first, and the surviving prefix no longer
    # matches the shape pattern, leaking a partial key.
    #
    # code_file=False is a deliberate divergence from redact_terminal_output,
    # which passes code_file=not assignments (assignment pass only for env
    # dumps / credential-file reads). execute_code output is arbitrary script
    # output that routinely prints os.environ and credential files, so the
    # assignment pass must run unconditionally here. The cost is that
    # `api_key=*** in printed source becomes `api_key=*** — acceptable
    # for a code-execution surface where real secrets are the norm.
    from agentos.redact import redact_sensitive_text

    redacted_stdout = redact_sensitive_text(stdout, force=True, code_file=False)
    redacted_stderr = redact_sensitive_text(stderr, force=True, code_file=False)
    return json.dumps(
        {
            "exit_code": returncode,
            "stdout": (redacted_stdout if redacted_stdout is not None else stdout)[
                :_MAX_OUTPUT_CHARS
            ],
            "stderr": (redacted_stderr if redacted_stderr is not None else stderr)[
                :_MAX_OUTPUT_CHARS
            ],
            "timed_out": timed_out,
            "elapsed_ms": elapsed_ms,
        },
        ensure_ascii=False,
    )


def _append_code_exec_sandbox_network_hint(*, stdout: str, stderr: str) -> str:
    from agentos.tools.builtin.shell import (
        _SANDBOX_NETWORK_HINT,
        _append_sandbox_network_hint,
        _looks_like_sandbox_network_failure,
    )

    if not _looks_like_sandbox_network_failure(stdout + "\n" + stderr):
        return stderr
    if stderr:
        return _append_sandbox_network_hint(stderr, force=True)
    return _SANDBOX_NETWORK_HINT


def _resolve_python_bin(*, sandbox_enabled: bool) -> str:
    """Resolve a Python executable that is visible from the execution mode."""
    if sandbox_enabled:
        # The bubblewrap backend exposes host /usr and /bin inside the sandbox,
        # but not the caller's project venv. `uv run` commonly puts
        # .venv/bin/python3 first on PATH, which is invisible after isolation.
        for candidate in _SANDBOX_PYTHON_CANDIDATES:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    else:
        current_python = Path(sys.executable)
        if current_python.is_file():
            return str(current_python)

    python_bin = shutil.which("python3") or shutil.which("python")
    if python_bin is None:
        raise ToolError("Python interpreter not found on PATH")
    return python_bin


@tool(
    name="execute_code",
    description=(
        "Execute Python code in an isolated subprocess and return stdout/stderr. "
        "When an active workspace is configured, code runs with that workspace "
        "as cwd; otherwise each invocation runs in a fresh temporary directory. "
        "Use for calculations, data processing, and validation."
    ),
    params={
        "code": {
            "type": "string",
            "description": "Python code to execute.",
        },
        "timeout": {
            "type": "number",
            "description": (
                f"Execution timeout in seconds (1-{_MAX_TIMEOUT}, default {_DEFAULT_TIMEOUT})."
            ),
        },
        "approval_id": {
            "type": "string",
            "description": "Approval record to consume for destructive Python operations.",
        },
    },
    required=["code"],
)
async def execute_code(
    code: str,
    timeout: float = _DEFAULT_TIMEOUT,
    approval_id: str | None = None,
) -> str:
    if not code.strip():
        raise ToolError("Code must not be empty")

    from agentos.tools.builtin.shell import _context_elevated_mode

    sensitive_access = _check_code_sensitive_access(code)
    if sensitive_access is not None and _context_elevated_mode() != "full":
        reason, marker = sensitive_access
        if reason == "sensitive_payload":
            from agentos.tools.builtin.web import _sensitive_body_block

            return _sensitive_body_block("execute_code", marker)

        from agentos.sandbox.sensitive_paths import build_block_envelope

        return json.dumps(
            build_block_envelope(
                "execute_code <python>",
                marker,
                tool_name="execute_code",
            ),
            ensure_ascii=False,
        )

    # Destructive-Python gate — mirrors the shell warnlist approval flow.
    warning = _check_code_destructive(code)
    if warning is not None:
        from agentos.tools.builtin.shell import (
            _approval_elevation_state,
            _check_exec_approval,
            _restore_approval_elevation,
        )

        prior_elevation = _approval_elevation_state()
        approval_response: dict[str, object] | None = None
        approval_granted = False
        try:
            approval_response = await _check_exec_approval(
                tool_name="execute_code",
                command=code[:200],
                workdir=None,
                warning=warning,
                approval_id=approval_id,
                background=False,
            )
            approval_granted = approval_response is None and _approval_elevation_state()
        finally:
            if not approval_granted:
                _restore_approval_elevation(prior_elevation)
        if approval_response is not None:
            return json.dumps(approval_response)

    timeout = max(1.0, min(float(timeout), _MAX_TIMEOUT))

    ctx = current_tool_context.get()
    runtime = get_runtime()
    sandbox_enabled = bool(runtime is not None and runtime.effective.sandbox_enabled)
    python_bin = _resolve_python_bin(sandbox_enabled=sandbox_enabled)
    workspace = (
        Path(ctx.workspace_dir).expanduser().resolve() if ctx and ctx.workspace_dir else None
    )
    cleanup_dir: str | None = None
    if workspace is not None:
        workspace.mkdir(parents=True, exist_ok=True)
        workdir_path = workspace
    elif runtime is not None and runtime.effective.sandbox_enabled:
        workdir_path = runtime.workspace.expanduser().resolve()
        workdir_path.mkdir(parents=True, exist_ok=True)
    else:
        workdir = tempfile.mkdtemp(prefix="agentos_exec_")
        workdir_path = Path(workdir)
        cleanup_dir = workdir
    start_ns = time.monotonic_ns()

    safe_env = _build_safe_env()

    from agentos.tools.builtin.shell import _elevated_mode

    elevated_bypass = _elevated_mode() in ("on", "bypass", "full")
    if runtime is None or (runtime.effective.sandbox_enabled and not elevated_bypass):
        decision, _policy, request = await gate_action(
            action_kind="code.exec",
            argv=(python_bin, "-c", code),
            cwd=workdir_path,
            env=safe_env,
        )
        if isinstance(decision, DenialResult):
            return json.dumps(decision.to_dict())
        backend_request = SandboxRequest(
            argv=(python_bin, "-c", code),
            cwd=request.cwd,
            action_kind=request.action_kind,
            policy=request.policy,
            env=safe_env,
        )
        try:
            sandbox_result = await run_under_backend(backend_request, runtime=runtime)
        except Exception as exc:
            return _execution_result_json(
                returncode=-1,
                stdout="",
                stderr=f"Execution error: {exc}",
                timed_out=False,
                elapsed_ms=0,
            )
        if sandbox_result.backend_notes:
            escalation = await escalate_backend_denial(
                sandbox_result, request, _policy, runtime=runtime
            )
            if isinstance(escalation, DenialResult):
                return json.dumps(escalation.to_dict())
            try:
                proc = await asyncio.create_subprocess_exec(
                    python_bin,
                    "-c",
                    code,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(workdir_path),
                    env=safe_env,
                )
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        proc.communicate(), timeout=timeout
                    )
                except TimeoutError:
                    proc.kill()
                    await proc.communicate()
                    elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
                    return _execution_result_json(
                        returncode=-1,
                        stdout="",
                        stderr=f"Execution timed out after {timeout}s",
                        timed_out=True,
                        elapsed_ms=elapsed_ms,
                    )
                elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
                return _execution_result_json(
                    returncode=proc.returncode if proc.returncode is not None else -1,
                    stdout=stdout_bytes.decode("utf-8", errors="replace"),
                    stderr=stderr_bytes.decode("utf-8", errors="replace"),
                    timed_out=False,
                    elapsed_ms=elapsed_ms,
                )
            except Exception as exc:
                return _execution_result_json(
                    returncode=-1,
                    stdout="",
                    stderr=f"Execution error: {exc}",
                    timed_out=False,
                    elapsed_ms=0,
                )
        elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
        stdout = sandbox_result.stdout
        stderr = sandbox_result.stderr
        stderr = _append_code_exec_sandbox_network_hint(stdout=stdout, stderr=stderr)
        return _execution_result_json(
            returncode=sandbox_result.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=sandbox_result.timed_out,
            elapsed_ms=elapsed_ms,
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            python_bin,
            "-c",
            code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workdir_path),
            env=safe_env,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
            return _execution_result_json(
                returncode=-1,
                stdout="",
                stderr=f"Execution timed out after {timeout}s",
                timed_out=True,
                elapsed_ms=elapsed_ms,
            )

        elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        return _execution_result_json(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            timed_out=False,
            elapsed_ms=elapsed_ms,
        )
    except Exception as exc:
        return _execution_result_json(
            returncode=-1,
            stdout="",
            stderr=f"Execution error: {exc}",
            timed_out=False,
            elapsed_ms=0,
        )
    finally:
        if cleanup_dir:
            shutil.rmtree(cleanup_dir, ignore_errors=True)
